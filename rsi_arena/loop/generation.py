"""One turn of the loop, recorded so the next can start from it.

A run directory holds ``manifest.json`` (this record), ``best.json`` (the
harness GEPA chose), ``rollouts/`` (every scored instance for baseline and
candidate, both splits), and GEPA's own state under ``gepa/``. A run can be
started from a previous run directory; its ``parent`` field is how lineage is
walked back to the seed harness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..harness import Harness

MANIFEST = "manifest.json"
BEST = "best.json"


def fingerprint(harness: Harness) -> str:
    """Identity of what the loop can change, and nothing else.

    Over the components and the model, not over ``to_dict()``. The loop renames
    every candidate ``<name>+genN``, so a fingerprint that included the name
    differed every generation whether or not anything had been rewritten — and
    a run where GEPA returned the incumbent unchanged still recorded a new
    fingerprint, reading as "a new harness tied" when the truth was "the search
    found nothing". Telling those two apart is the entire job.

    The model is in because the same components on a different model are a
    different harness; the description is out because prose about a harness is
    not the harness.
    """
    canon = json.dumps({**harness.to_components(), "model": harness.config.model},
                       sort_keys=True, default=str).encode()
    return hashlib.sha256(canon).hexdigest()[:12]


@dataclass
class Generation:
    run_dir: str
    topic: str
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    parent: str | None = None                  # a run directory, or None for the first generation
    incumbent: str = ""                        # path of the harness this generation started from
    incumbent_fingerprint: str = ""
    candidate_fingerprint: str = ""
    settings: dict[str, Any] = field(default_factory=dict)
    split: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)     # {"train": summary, "holdout": summary}
    candidate: dict[str, Any] = field(default_factory=dict)
    decision: dict[str, Any] = field(default_factory=dict)
    search: dict[str, Any] = field(default_factory=dict)       # what GEPA did
    llm: dict[str, Any] = field(default_factory=dict)          # calls, cache hits, spend

    @property
    def accepted(self) -> bool:
        return bool(self.decision.get("accepted"))

    @property
    def path(self) -> Path:
        return Path(self.run_dir)

    def promoted(self) -> str:
        """The harness the next generation starts from: the candidate if accepted, else the incumbent."""
        return str(self.path / BEST) if self.accepted else self.incumbent

    def save(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        out = self.path / MANIFEST
        out.write_text(json.dumps(asdict(self), indent=2, default=str))
        return out

    @classmethod
    def load(cls, run_dir: str | Path) -> "Generation":
        data = json.loads((Path(run_dir) / MANIFEST).read_text())
        return cls(**data)

    @staticmethod
    def is_run_dir(path: str | Path) -> bool:
        return (Path(path) / MANIFEST).exists()


def resolve_harness(path: str | Path) -> tuple[Harness, str, str | None]:
    """A harness file, or a run directory to continue from.

    Returns the harness, the path it was read from, and the parent run directory
    if there was one.
    """
    if Generation.is_run_dir(path):
        gen = Generation.load(path)
        source = gen.promoted()
        return Harness.load(source), source, str(path)
    return Harness.load(path), str(path), None


def lineage(run_dir: str | Path) -> list[Generation]:
    """Every generation from the first to this one."""
    chain: list[Generation] = []
    cursor: str | None = str(run_dir)
    seen: set[str] = set()
    while cursor and Generation.is_run_dir(cursor) and cursor not in seen:
        seen.add(cursor)
        gen = Generation.load(cursor)
        chain.append(gen)
        cursor = gen.parent
    return list(reversed(chain))


def render_lineage(chain: list[Generation]) -> str:
    if not chain:
        return "no generations"
    head = f"{'generation':28} {'accepted':8} {'train':>8} {'held-out':>9} {'$/inst':>8} {'cands':>6}  reason"
    rows = [head, "-" * len(head)]
    for g in chain:
        cand_h, cand_t = g.candidate.get("holdout", {}), g.candidate.get("train", {})
        rows.append(f"{Path(g.run_dir).name:28} {'yes' if g.accepted else 'no':8} "
                    f"{cand_t.get('statistic', 0.0):+8.3f} {cand_h.get('statistic', 0.0):+9.3f} "
                    f"{cand_h.get('cost_per_instance', 0.0):8.4f} {g.search.get('candidates', 0):6}  "
                    f"{(g.decision.get('reasons') or [''])[0]}")
    base = chain[0].baseline.get("holdout", {})
    rows.append(f"{'(seed baseline)':28} {'':8} {chain[0].baseline.get('train', {}).get('statistic', 0.0):+8.3f} "
                f"{base.get('statistic', 0.0):+9.3f} {base.get('cost_per_instance', 0.0):8.4f}")
    return "\n".join(rows)
