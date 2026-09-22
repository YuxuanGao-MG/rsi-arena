"""What a harness already scored on a window, so it is never bought twice.

Nearly a third of a generation's bill was the incumbent being re-scored on
windows it had already been scored on. Not approximately the same windows — the
same ones: the incumbent has been byte-identical for five generations because
nothing has ever been promoted, the question set is built once and committed,
and a window's realised price was fixed the moment the candle printed. The
answer is a pure function of (harness, window), and it was being re-bought at
three and a half cents a time, four hundred and eighty times a generation.

There was already a cache one layer down — the model client memoises on the
request body — but it kept missing, twelve per cent on the last real run, and
it will keep missing: it lives in the Actions cache, which is evicted by size
across the whole repository, and a rebuilt prompt changes the key even when the
answer cannot have changed. This memoises the thing that actually matters, the
*outcome*, and keeps it in the repository beside the runs it came from.

Deliberately not a general cache. It stores only what the gate and the archive
read back — the score, and what it cost when it was genuinely paid for. Traces
are two orders larger and are what ``runs/*/rollouts/`` is for.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .settings import Settings
from .task import Instance, Outcome, Rollout

#: Where it lives, relative to the runs directory.
SCOREBOARD = "scoreboard.json"


def scoreboard_path(runs_root: str | Path, topic: str = "") -> Path:
    """``runs/scoreboard.json`` for the first topic, ``runs/scoreboard.<topic>.json`` after.

    Flat names in one directory, and the first topic keeps the name it has
    always had: its scoreboard is committed history that a rename would
    orphan, and the workflow reads it by that name.
    """
    root = Path(runs_root)
    # A topic's own runs directory (runs/<topic>/) keeps the flat file beside
    # the first topic's, in the parent: the web image copies runs/archive*.json
    # and .railwayignore cannot re-include a file inside an excluded directory.
    if root.name == topic:
        root = root.parent
    if not topic or topic == Settings.topic:
        return root / SCOREBOARD
    return root / f"scoreboard.{topic}.json"


def key(fingerprint: str, instance: Instance) -> str:
    """Identity of one harness's answer to one question.

    The instance id alone would be enough today and would be a trap tomorrow:
    ids are ``ticker@instant``, and a question set rebuilt against revised
    candle history could keep an id while changing the answer underneath it. The
    numbers that define the question go into the key, so a rebuilt window is a
    different question rather than a stale hit.
    """
    payload = {"h": fingerprint, "i": getattr(instance, "id", ""),
               "m": getattr(instance, "mid_now", None),
               "r": getattr(instance, "realised", None)}
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:20]


class Scoreboard:
    """Outcomes already paid for, by harness and question."""

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self.entries: dict[str, dict[str, Any]] = dict(entries or {})
        self.hits = 0
        self.added = 0

    # -- persistence --

    @classmethod
    def load(cls, path: str | Path) -> "Scoreboard":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls(json.loads(p.read_text()).get("entries", {}))
        except Exception:
            # Same reasoning as the archive: this is memory, not evidence. Losing
            # it costs money; refusing to run costs the generation.
            return cls()

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"entries": self.entries}, sort_keys=True))

    path_for = staticmethod(scoreboard_path)

    def __len__(self) -> int:
        return len(self.entries)

    # -- use --

    def get(self, fingerprint: str, instance: Instance) -> Outcome | None:
        row = self.entries.get(key(fingerprint, instance))
        if row is None:
            return None
        self.hits += 1
        return Outcome(value=row["value"], feedback=row.get("feedback", ""),
                       objectives=row.get("objectives", {}), details=row.get("details", {}))

    def put(self, fingerprint: str, instance: Instance, outcome: Outcome,
            cost_usd: float = 0.0) -> None:
        k = key(fingerprint, instance)
        if k in self.entries:
            return
        self.added += 1
        self.entries[k] = {"value": outcome.value, "feedback": outcome.feedback,
                           "objectives": outcome.objectives, "details": outcome.details,
                           "cost_usd": cost_usd}

    def cost_of(self, fingerprint: str, instance: Instance) -> float:
        """What this answer cost the first time.

        Kept because the gate rejects a candidate that costs more than twice the
        incumbent per instance, and a memoised incumbent that reports zero would
        silently disable that check — ``inc_cost > 0`` guards it. A remembered
        answer is free to fetch and was not free to produce, and the second
        number is the one the comparison means.
        """
        return float((self.entries.get(key(fingerprint, instance)) or {}).get("cost_usd", 0.0))

    def absorb(self, fingerprint: str, rollouts: list[Rollout]) -> int:
        """Remember a finished evaluation. Returns how many were new."""
        before = self.added
        for r in rollouts:
            # A run that failed for want of money is not an answer. Remembering
            # it would make the shortage permanent.
            if r.run is not None and (r.run.error_kind in ("budget", "provider")):
                continue
            if r.run is None and not r.outcome.details.get("scored"):
                continue
            self.put(fingerprint, r.instance, r.outcome,
                     cost_usd=r.cost_usd if r.run is not None else 0.0)
        return self.added - before

    def summary(self) -> dict[str, Any]:
        return {"remembered": len(self.entries), "hits": self.hits, "added": self.added}
