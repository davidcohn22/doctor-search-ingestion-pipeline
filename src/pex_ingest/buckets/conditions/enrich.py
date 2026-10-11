"""LLM enrichment: patient-friendly names and the phrases people actually type.

MeSH names concepts the way clinicians write them ("Prostatic Hyperplasia").
Patients type "enlarged prostate", "prostate problems", or just "prostate
doctor". MedlinePlus covers ~1,000 descriptors with human-authored consumer
names; this fills the rest, and adds colloquial and body-part phrasings to all of
them.

**Run it through the Message Batches API**, per
docs/prerequisites/05-condition-vocabulary.md:

  * 50% of standard token cost — the whole national job is ~$8 at Sonnet 5.5,
  * no rate-limit management: submit the job, poll, collect,
  * up to 100,000 requests per batch, results retrievable for 29 days.

**The one mistake that silently corrupts the output** is joining results back by
position. Batch results return in arbitrary order, so every request carries the
MeSH descriptor UI as its `custom_id` and results are keyed on that. Pair
descriptor #400's synonyms onto descriptor #12 and you get a plausible-looking
condition list that is quietly wrong in ways spot-checking may never catch.

Also handled: a batch result is `succeeded`, `errored`, `canceled` **or**
`expired`. Treating all four as success means a descriptor silently gets no
synonyms.
"""

from __future__ import annotations

import json
import logging
import re
import time

log = logging.getLogger("pex.conditions.enrich")

PROMPT = """MeSH descriptor: "{name}" (type: {kind}).

You are naming a medical condition for a physician-search product used by
patients researching their own care.

Return JSON only, no prose:
{{"display_name": "<short patient-friendly name; include the clinical term in \
parentheses when patients would recognize it, e.g. 'Enlarged prostate (BPH)'>",
 "kind": "condition" | "procedure",
 "description": "<one plain-English sentence a patient can understand>",
 "colloquial_synonyms": ["<3-8 phrases patients actually type, e.g. 'prostate problems'>"],
 "body_part_synonyms": ["<0-4 body-part phrasings, e.g. 'prostate doctor'>"]}}

Rules:
- Every synonym must be lowercase.
- Do not invent alternative clinical names; MeSH's own entry terms already cover those.
- If the descriptor is not something a patient would ever search for when looking
  for a doctor, return "display_name": null and empty synonym lists."""

# Batch jobs are large enough that a poll loop needs a real ceiling. Most complete
# within an hour; the API's own maximum is 24 hours.
POLL_INTERVAL = 30
POLL_TIMEOUT = 24 * 3600

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)

DEFAULT_MODEL = "claude-sonnet-5-5"


def model_params(config) -> dict:
    """The model settings every call shares, from branch_config.yaml's `llm` block.

    `thinking: between_tools` is how Sonnet 5.5 turns thinking off; it is only
    accepted at effort `high` or below. Thinking adds nothing to a fixed-schema
    reply this short, and at max_tokens 400 it can truncate the JSON.
    """
    params = {
        "model": config.llm.get("model", DEFAULT_MODEL),
        "max_tokens": int(config.llm.get("max_tokens", 400)),
    }
    if config.llm.get("thinking"):
        params["thinking"] = {"type": config.llm["thinking"]}
    if config.llm.get("effort"):
        params["output_config"] = {"effort": config.llm["effort"]}
    return params


def build_requests(descriptors: list[dict], config) -> list[dict]:
    """One request per descriptor, keyed by its UI.

    Batching ~20 descriptors per request would cut the bill by another ~$1.50 but
    adds a join-back-by-index failure mode. Not worth it at these amounts — one
    descriptor per request with the UI as `custom_id` has nothing to misalign.
    """
    params = model_params(config)
    return [
        {
            "custom_id": descriptor["mesh_id"],
            "params": {
                **params,
                "messages": [
                    {
                        "role": "user",
                        "content": PROMPT.format(
                            name=descriptor["name"], kind=descriptor.get("kind", "condition")
                        ),
                    }
                ],
            },
        }
        for descriptor in descriptors
    ]


def parse_reply(text: str) -> dict | None:
    """Extract the JSON object from a model reply, tolerantly.

    Asking for "JSON only" usually gets JSON only, but a stray sentence around it
    should not cost a descriptor its synonyms.
    """
    if not text:
        return None
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    def clean_list(values) -> list[str]:
        out = []
        for value in values or []:
            if isinstance(value, str) and value.strip():
                # condition_synonym has a CHECK that synonyms are lowercase, so
                # normalizing here is what keeps the load from failing row by row.
                out.append(value.strip().lower())
        return sorted(set(out))

    return {
        "display_name": (data.get("display_name") or None),
        "kind": data.get("kind") if data.get("kind") in ("condition", "procedure") else None,
        "description": (data.get("description") or None),
        "colloquial_synonyms": clean_list(data.get("colloquial_synonyms")),
        "body_part_synonyms": clean_list(data.get("body_part_synonyms")),
    }


def run_batch(descriptors: list[dict], config, settings) -> dict[str, dict]:
    """Submit, poll, and collect. Returns {mesh_id: enrichment}."""
    import anthropic

    settings.require("anthropic_api_key")
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    requests = build_requests(descriptors, config)
    log.info(
        "submitting %s descriptors to the Batches API (model %s)",
        f"{len(requests):,}", config.llm.get("model", DEFAULT_MODEL),
    )

    batch = client.messages.batches.create(requests=requests)
    log.info("batch %s submitted; polling every %ds", batch.id, POLL_INTERVAL)

    started = time.monotonic()
    while batch.processing_status != "ended":
        if time.monotonic() - started > POLL_TIMEOUT:
            raise RuntimeError(f"batch {batch.id} did not finish within 24 hours")
        time.sleep(POLL_INTERVAL)
        batch = client.messages.batches.retrieve(batch.id)
        counts = getattr(batch, "request_counts", None)
        if counts:
            log.info(
                "  … %s succeeded, %s errored, %s processing",
                getattr(counts, "succeeded", 0), getattr(counts, "errored", 0),
                getattr(counts, "processing", 0),
            )

    results: dict[str, dict] = {}
    outcomes = {
        "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0,
        "refused": 0, "truncated": 0, "unparsed": 0,
    }

    for entry in client.messages.batches.results(batch.id):
        mesh_id = entry.custom_id           # never positional — see the module docstring
        result_type = entry.result.type
        outcomes[result_type] = outcomes.get(result_type, 0) + 1
        if result_type != "succeeded":
            log.warning("descriptor %s: batch result %s", mesh_id, result_type)
            continue

        message = entry.result.message
        # A batch request that "succeeded" can still have been declined by a
        # safety classifier or cut off at max_tokens; neither is usable output.
        if message.stop_reason == "refusal":
            outcomes["refused"] += 1
            log.warning("descriptor %s: model declined", mesh_id)
            continue
        if message.stop_reason == "max_tokens":
            outcomes["truncated"] += 1
            log.warning("descriptor %s: reply hit max_tokens", mesh_id)
            continue
        text = "".join(block.text for block in message.content if block.type == "text")
        parsed = parse_reply(text)
        if parsed is None:
            outcomes["unparsed"] += 1
            log.warning("descriptor %s: could not parse the reply as JSON", mesh_id)
            continue
        results[mesh_id] = parsed

    log.info("batch %s complete: %s", batch.id, outcomes)
    missing = sum(
        outcomes.get(k, 0) for k in ("errored", "expired", "refused", "truncated", "unparsed")
    )
    if missing:
        log.warning(
            "%d descriptors got no enrichment — re-run to fill them; the loader "
            "keeps whatever it already has",
            missing,
        )
    return results


def run_live(descriptors: list[dict], config, settings) -> dict[str, dict]:
    """One live call per descriptor. For samples and model comparison only.

    At real volume this is the wrong tool — it is twice the price and requires
    pacing logic the Batches API makes unnecessary.
    """
    import anthropic

    settings.require("anthropic_api_key")
    client = anthropic.Anthropic()
    params = model_params(config)

    results = {}
    for descriptor in descriptors:
        message = client.messages.create(
            **params,
            messages=[{
                "role": "user",
                "content": PROMPT.format(
                    name=descriptor["name"], kind=descriptor.get("kind", "condition")
                ),
            }],
        )
        if message.stop_reason in ("refusal", "max_tokens"):
            log.warning("descriptor %s: stop_reason %s", descriptor["mesh_id"], message.stop_reason)
            continue
        text = "".join(block.text for block in message.content if block.type == "text")
        parsed = parse_reply(text)
        if parsed:
            results[descriptor["mesh_id"]] = parsed
    return results


def estimate_cost(descriptors: list[dict], config, settings) -> dict:
    """Token-count the real prompt before spending anything.

    The prerequisites doc's figures are estimates, not tokenizer-measured, and
    says so — this is the check it asks for.
    """
    import anthropic

    settings.require("anthropic_api_key")
    client = anthropic.Anthropic()
    model = config.llm.get("model", DEFAULT_MODEL)

    sample = descriptors[: min(10, len(descriptors))]
    total_input = 0
    for descriptor in sample:
        counted = client.messages.count_tokens(
            model=model,
            messages=[{
                "role": "user",
                "content": PROMPT.format(
                    name=descriptor["name"], kind=descriptor.get("kind", "condition")
                ),
            }],
        )
        total_input += counted.input_tokens

    mean_input = total_input / max(len(sample), 1)
    log.info(
        "measured ~%.0f input tokens per descriptor over %d samples; "
        "%s descriptors → ~%s input tokens (batch pricing halves the bill)",
        mean_input, len(sample), f"{len(descriptors):,}",
        f"{int(mean_input * len(descriptors)):,}",
    )
    return {"mean_input_tokens": mean_input, "descriptors": len(descriptors)}
