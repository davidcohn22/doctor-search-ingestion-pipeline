"""Run the two generation passes against the Claude API and save every reply.

    pass 1  list concepts per specialty group      Sonnet 5.5, one call per group
    pass 2  write a card per merged concept        Haiku 5.5, ~10 concepts per call

Two transports, one result format:

  * **live** — ordinary streaming calls, a few at a time. Used to tune the prompts
    on urology, where waiting for a batch would slow every iteration.
  * **batch** — the Message Batches API at half price, for the full run.

Every reply is written to the run directory before anything parses it
(`listing/<group>.json`, `cards/<chunk>.json`), and a pass skips work whose file
already exists. Re-running a step after a crash or a prompt fix costs only what
is missing, and every generated card can be traced to the exact reply it came
from. Results are keyed by `custom_id`, never by position: batch results come
back in any order.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import prompts

log = logging.getLogger("pex.vocabulary.generate")

LISTING_MODEL = "claude-sonnet-5-5"
CARD_MODEL = "claude-haiku-5-5"
CARDS_PER_CALL = 10
POLL_INTERVAL = 30


def listing_params(group_name: str) -> dict:
    return {
        "model": LISTING_MODEL,
        # A broad specialty lists ~300 concepts with aliases; thinking shares the
        # budget. Generous, because a truncated list silently loses its tail.
        "max_tokens": 48_000,
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": prompts.LISTING_SCHEMA},
        },
        **prompts.listing_messages(group_name),
    }


def card_params(concepts: list[dict], neighbors: list[str], groups: list[str]) -> dict:
    return {
        "model": CARD_MODEL,
        "max_tokens": 24_000,
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": prompts.CARD_SCHEMA},
        },
        **prompts.card_messages(concepts, neighbors, groups),
    }


def _client():
    import anthropic

    return anthropic.Anthropic(max_retries=4)


def _message_record(message) -> dict:
    """What we keep of a reply: its text, why it stopped, and what it cost."""
    text = "".join(b.text for b in message.content if b.type == "text")
    usage = message.usage
    return {
        "stop_reason": message.stop_reason,
        "text": text,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
        "model": message.model,
    }


def _live_call(client, params: dict) -> dict:
    # Streaming: long outputs would otherwise risk an HTTP timeout.
    with client.messages.stream(**params) as stream:
        message = stream.get_final_message()
    return _message_record(message)


def run_live(jobs: dict[str, dict], out_dir: Path, workers: int = 4) -> dict:
    """Run {custom_id: params} as live calls; write out_dir/<custom_id>.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = {k: v for k, v in jobs.items() if not (out_dir / f"{k}.json").exists()}
    log.info("live: %d calls to make (%d already saved)", len(todo), len(jobs) - len(todo))
    client = _client()

    def one(item):
        custom_id, params = item
        try:
            record = _live_call(client, params)
        except Exception as exc:  # noqa: BLE001 — recorded, retried on the next run
            log.warning("%s failed: %s", custom_id, exc)
            return custom_id, None
        (out_dir / f"{custom_id}.json").write_text(json.dumps(record), encoding="utf-8")
        return custom_id, record

    stats = {"ok": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for custom_id, record in pool.map(one, todo.items()):
            if record is None:
                stats["failed"] += 1
            else:
                stats["ok"] += 1
                log.info("  %s: %s, %s output tokens", custom_id, record["stop_reason"],
                         f"{record['usage']['output_tokens']:,}")
    return stats


def run_batch(jobs: dict[str, dict], out_dir: Path, state_file: Path) -> dict:
    """Run {custom_id: params} through the Batches API; write out_dir/<custom_id>.json.

    The batch id is saved to `state_file` as soon as it exists, so an interrupted
    poll resumes the same batch instead of paying for a second one.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    client = _client()
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    batch_id = state.get("batch_id")

    if not batch_id:
        todo = {k: v for k, v in jobs.items() if not (out_dir / f"{k}.json").exists()}
        if not todo:
            log.info("batch: nothing to do")
            return {"ok": 0}
        batch = client.messages.batches.create(
            requests=[{"custom_id": k, "params": v} for k, v in todo.items()]
        )
        batch_id = batch.id
        state_file.write_text(json.dumps({"batch_id": batch_id, "submitted": len(todo)}))
        log.info("batch %s submitted with %d requests", batch_id, len(todo))

    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        if batch.processing_status == "ended":
            break
        log.info("  batch %s: %s processing, %s succeeded, %s errored", batch_id,
                 counts.processing, counts.succeeded, counts.errored)
        time.sleep(POLL_INTERVAL)

    stats = {"ok": 0, "errored": 0, "canceled": 0, "expired": 0}
    for entry in client.messages.batches.results(batch_id):
        kind = entry.result.type
        if kind != "succeeded":
            stats[kind] = stats.get(kind, 0) + 1
            log.warning("%s: batch result %s", entry.custom_id, kind)
            continue
        record = _message_record(entry.result.message)
        (out_dir / f"{entry.custom_id}.json").write_text(json.dumps(record), encoding="utf-8")
        stats["ok"] += 1
    state_file.unlink(missing_ok=True)   # finished: a re-run submits only what failed
    log.info("batch %s done: %s", batch_id, stats)
    return stats


def read_reply(path: Path) -> tuple[dict | None, str]:
    """(parsed JSON, problem) for one saved reply."""
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["stop_reason"] == "refusal":
        return None, "refusal"
    if record["stop_reason"] == "max_tokens":
        return None, "truncated"
    try:
        return json.loads(record["text"]), ""
    except json.JSONDecodeError:
        return None, "unparsed"


def usage_cost(out_dir: Path, model: str, batch: bool) -> float:
    """Dollars spent on the replies in a directory, at list price."""
    prices = {LISTING_MODEL: (2.0, 10.0), CARD_MODEL: (0.10, 0.50)}
    price_in, price_out = prices[model]
    total = 0.0
    for path in out_dir.glob("*.json"):
        usage = json.loads(path.read_text())["usage"]
        total += usage["input_tokens"] * price_in / 1e6 + usage["output_tokens"] * price_out / 1e6
    return total * (0.5 if batch else 1.0)
