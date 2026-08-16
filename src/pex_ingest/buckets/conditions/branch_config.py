"""Load and validate `config/branch_config.yaml`.

The validation is the point of this module, not the loading.

  "Validate every configured branch against the year's actual tree at parse time,
   and **fail loudly** if one is missing. MeSH reorganizes its tree between annual
   releases, and a branch number that quietly stopped existing produces zero
   candidate conditions for that specialty — which looks identical to 'this
   specialty has no conditions' and can survive a long way into a release before
   anyone notices."
   — docs/prerequisites/05-condition-vocabulary.md

So a stale branch number is a hard failure, never a warning.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ...config import REPO_ROOT

log = logging.getLogger("pex.conditions.config")

DEFAULT_PATH = REPO_ROOT / "config" / "branch_config.yaml"


class BranchConfigError(RuntimeError):
    pass


@dataclass
class BranchConfig:
    condition_branches: list[str]
    procedure_branches: list[str]
    excluded_branches: list[str]
    excluded_descriptors: set[str] = field(default_factory=set)
    prune: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)
    specialty_mapping: dict = field(default_factory=dict)

    @property
    def all_branches(self) -> list[str]:
        return list(self.condition_branches) + list(self.procedure_branches)

    def _under(self, tree: str, branch: str) -> bool:
        """Is this tree number inside this branch?

        Matching is on dot boundaries — `C12.294` is under `C12`, `C120` is not —
        **except** for a top-level category letter, which is followed directly by
        digits rather than a dot: the category `C` contains `C12.294.565.500`.
        Requiring a dot everywhere would silently match nothing at all for every
        single-letter branch, which is the entire `C`, `D` and `F` configuration.
        """
        if tree == branch or tree.startswith(branch + "."):
            return True
        if len(branch) == 1 and branch.isalpha():
            return tree[:1] == branch and tree[1:2].isdigit()
        return False

    def is_excluded(self, trees: list[str]) -> bool:
        """A descriptor is excluded if *any* of its tree numbers sits in an
        excluded branch.

        Deliberately "any", not "all". A descriptor that appears both under a
        disease branch and under Psychological Phenomena is exactly the ambiguous
        case the exclusion list exists to keep out, and letting one acceptable
        placement rescue it would defeat the filter.
        """
        return any(
            self._under(tree, branch)
            for tree in trees
            for branch in self.excluded_branches
        )

    def classify(self, trees: list[str]) -> str | None:
        """'condition', 'procedure', or None if the descriptor is out of scope.

        Conditions win ties: a descriptor sitting under both `C` and `E04` is far
        more useful to a patient as a condition to search than as a procedure.
        """
        if not trees or self.is_excluded(trees):
            return None
        for branch in self.condition_branches:
            if any(self._under(tree, branch) for tree in trees):
                return "condition"
        for branch in self.procedure_branches:
            if any(self._under(tree, branch) for tree in trees):
                return "procedure"
        return None

    def validate_against(self, all_trees: list[tuple[str, list[str]]]) -> None:
        """Fail the run if a configured branch matches nothing in this year's tree."""
        every_tree = [tree for _mesh_id, trees in all_trees for tree in trees]
        empty = [
            branch for branch in self.all_branches
            if not any(self._under(tree, branch) for tree in every_tree)
        ]
        if empty:
            raise BranchConfigError(
                f"Configured MeSH branches match no descriptor in this release: {empty}.\n"
                "MeSH reorganizes between annual releases — verify each branch against "
                "the descriptor file before re-running. This is a hard failure because "
                "a stale branch silently produces zero conditions."
            )
        log.info("all %d configured branches validated against the release", len(self.all_branches))


def load(path: Path | None = None) -> BranchConfig:
    import yaml

    path = Path(path) if path else DEFAULT_PATH
    if not path.exists():
        raise BranchConfigError(
            f"{path} not found. It is the one hand-written input this pipeline has; "
            "see docs/prerequisites/05-condition-vocabulary.md#the-hand-written-input"
        )

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    include = data.get("include") or {}
    exclude = data.get("exclude") or {}

    config = BranchConfig(
        condition_branches=list(include.get("condition_branches") or []),
        procedure_branches=list(include.get("procedure_branches") or []),
        excluded_branches=list(exclude.get("branches") or []),
        excluded_descriptors=set(exclude.get("descriptors") or []),
        prune=data.get("prune") or {},
        llm=data.get("llm") or {},
        specialty_mapping=data.get("specialty_mapping") or {},
    )
    if not config.all_branches:
        raise BranchConfigError(f"{path} configures no branches at all — nothing would be built.")

    # A branch that is both included and excluded is a configuration mistake with
    # a silent, confusing outcome (the exclusion wins and the branch vanishes).
    overlap = [
        branch for branch in config.all_branches
        if any(branch == ex or branch.startswith(ex + ".") for ex in config.excluded_branches)
    ]
    if overlap:
        raise BranchConfigError(
            f"These branches are both included and excluded: {overlap}. "
            "The exclusion would win and they would produce nothing."
        )

    log.info(
        "branch config: conditions=%s procedures=%s excluded=%s",
        config.condition_branches, config.procedure_branches, config.excluded_branches,
    )
    return config
