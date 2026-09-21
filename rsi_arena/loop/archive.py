"""Everything the search has ever found, and how to pick what to try next from it.

A generation used to keep one harness and throw away the rest. GEPA proposed
seven candidates, the best mean scorer was gated, and the other six — each of
which had been paid for, and some of which were the best thing anyone had found
on some particular match — were left in ``runs/*/gepa/`` and never read again.
Three generations have run that way and none was accepted, so the loop's entire
memory of three days of search is a single JSON file identical to the one it
started from.

That is the configuration GEPA's own ablation measures as roughly half-strength.
Selecting the best mean candidate is worth +6.05% over the seed in their table;
selecting from a Pareto frontier is worth +12.44%. The difference is not a
better mutation — it is refusing to discard a candidate that lost on average
while being the only thing that ever worked on some instance. The Darwin Gödel
Machine says the same in the negative: keeping only the most recent agent means
"a poorly performing self-modification makes subsequent improvements harder to
achieve", because the stepping stone back to solid ground has been deleted.

So: keep everything, remember what each candidate was uniquely good at, and
sample the next parent in proportion to how much of the instance space it uniquely
owns — discounted by how often it has already been mined, so one strong ancestor
cannot monopolise the search.

The archive is deliberately separate from the lineage. ``generation.py`` records
what was *promoted*, which is a claim about held-out evidence and a thing the
gate alone may change. This records what was *found*, which is cheaper, larger,
and carries no claim at all. Conflating them is how an archive turns into a
leaderboard and stops preserving the losers that make it worth having.
"""

from __future__ import annotations

import json
import pickle
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .settings import Settings

#: Where the archive lives, relative to the runs directory.
ARCHIVE = "archive.json"


def archive_path(runs_root: str | Path, topic: str = "") -> Path:
    """``runs/archive.json`` for the first topic, ``runs/archive.<topic>.json`` after.

    The first topic keeps its name: sixteen candidates were back-filled into
    that file from rejected runs, and the workflow commits it by that name.
    """
    root = Path(runs_root)
    if not topic or topic == Settings.topic:
        return root / ARCHIVE
    return root / f"archive.{topic}.json"

#: How hard to discount a parent that has already been mined.
#:
#: The Darwin Gödel Machine's sampling weight is ``1/(1+children)``. Without it,
#: the first candidate to own a large slice of the instance space is sampled
#: nearly every generation forever, and the search becomes the deep single
#: lineage it already was — with extra bookkeeping.
MINED_DISCOUNT = 1.0


@dataclass
class Entry:
    """One candidate anyone has ever evaluated, with what it scored on what.

    ``scores`` is keyed by instance id rather than position because the instance
    set moves: the holdout rotates, the benchmark grows, a fixture's windows get
    rebuilt. Two candidates from different generations are comparable exactly on
    the instances they have both seen and nowhere else, and keying by id is what
    makes that fall out rather than needing to be remembered.
    """

    id: str                                    # fingerprint of components + model
    components: dict[str, str]
    generation: str                            # the run directory that found it
    parent: str | None = None                  # another entry's id
    scores: dict[str, float] = field(default_factory=dict)
    cost_usd: float = 0.0
    discovered_after_calls: int = 0
    children: int = 0                          # times sampled as a parent
    promoted: bool = False                     # did the gate ever accept it
    note: str = ""

    @property
    def flat(self) -> bool:
        """Every score the same, so it separates nothing.

        Silence across a whole valset looks like this, and so does a harness that
        failed identically everywhere. Either way there is no instance it is
        better at than any other.
        """
        if len(self.scores) < 4:
            return False
        return len(set(round(v, 9) for v in self.scores.values())) == 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class Archive:
    """Every candidate, and the frontier over them."""

    def __init__(self, entries: list[Entry] | None = None) -> None:
        self.entries: list[Entry] = list(entries or [])

    # -- persistence --

    @classmethod
    def load(cls, path: str | Path) -> "Archive":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = json.loads(p.read_text())
        except Exception:
            # An archive that cannot be read must not stop a generation. It is
            # memory, not evidence: losing it costs search efficiency, and
            # refusing to run costs the whole generation.
            return cls()
        return cls([Entry.from_dict(e) for e in raw.get("entries", [])])

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"entries": [e.to_dict() for e in self.entries]}, indent=1, sort_keys=True))

    path_for = staticmethod(archive_path)

    # -- contents --

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, entry_id: str) -> Entry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def add(self, entry: Entry) -> Entry:
        """Add, or merge into what is already there.

        The same components can be found twice — a rewrite that reverts, two
        generations that converge — and the second finding is more evidence about
        one candidate, not a second candidate.
        """
        existing = self.get(entry.id)
        if existing is None:
            self.entries.append(entry)
            return entry
        existing.scores.update(entry.scores)
        existing.cost_usd = entry.cost_usd or existing.cost_usd
        existing.promoted = existing.promoted or entry.promoted
        return existing

    # -- the frontier --

    def contested(self) -> set[str]:
        """Instances more than one candidate has actually been scored on.

        An instance only one candidate has seen cannot rank candidates — there is
        nobody to be better *than*. Counting it anyway is how gen5's refused
        harness came to own the archive: it was scored an identical 0.5 on all
        2,600 windows of its own split, no earlier candidate had seen any of
        them, and so it was trivially instance-best on 2,586 — a sampling weight
        of sixty to one over every candidate that had actually forecast
        something, earned entirely by being refused.

        Comparing only where a comparison exists is also what makes the frontier
        mean anything when the held-out set rotates.
        """
        seen: dict[str, int] = {}
        for e in self.entries:
            for instance in e.scores:
                seen[instance] = seen.get(instance, 0) + 1
        return {i for i, n in seen.items() if n > 1}

    def instance_best(self) -> dict[str, float]:
        """``s*[i]``: the best anything has scored on each contested instance."""
        best: dict[str, float] = {}
        contested = self.contested()
        for e in self.entries:
            for instance, score in e.scores.items():
                if instance in contested and score > best.get(instance, float("-inf")):
                    best[instance] = score
        return best

    def wins(self) -> dict[str, list[str]]:
        """``f[Φ]`` by entry id: the instances on which it is tied for best.

        Ties count for everyone tied, which is the published algorithm and also
        the right reading — two candidates that both solve an instance perfectly
        are both evidence that the instance is solvable that way.
        """
        best = self.instance_best()
        out: dict[str, list[str]] = {e.id: [] for e in self.entries}
        for e in self.entries:
            for instance, score in e.scores.items():
                if instance in best and score >= best[instance]:
                    out[e.id].append(instance)
        return out

    def frontier(self) -> list[Entry]:
        """Candidates that are best at something and dominated by nothing.

        Domination is judged only on instances both candidates have actually
        been scored on. A candidate evaluated on forty windows cannot dominate
        one evaluated on four hundred just by having been asked fewer questions,
        and with fewer than a handful of shared instances the comparison is not
        worth making at all.
        """
        wins = self.wins()
        contested = self.contested()
        # Never compared is not the same as compared and beaten.
        #
        # A candidate from a rotated held-out set may share no instance with
        # anything already in the archive. It has won nothing, but nothing has
        # beaten it either, and dropping it would mean the frontier empties every
        # time the split moves. It stays, and `sample_parents` gives it the floor
        # weight of one — reachable, never dominant. A candidate that *was*
        # compared and lost every comparison is a different case and does drop.
        contenders = [e for e in self.entries
                      if wins[e.id] or not (set(e.scores) & contested)]
        keep: list[Entry] = []
        for e in contenders:
            if not any(_dominates(other, e) for other in contenders if other.id != e.id):
                keep.append(e)
        return keep

    def sample_parents(self, n: int, *, seed: int = 0,
                       include: list[Entry] | None = None) -> list[Entry]:
        """``n`` parents drawn from the frontier, in proportion to what they own.

        Weight is the count of instances the candidate is instance-best on,
        divided by one plus the number of times it has already been sampled.
        The numerator is GEPA's; the denominator is the Darwin Gödel Machine's,
        and without it the first broad candidate is sampled every generation
        until the end of time.

        Drawn without replacement, because the point of asking for several
        parents is to mutate several different things.
        """
        pool = list(self.frontier())
        for e in include or []:
            if not any(x.id == e.id for x in pool):
                pool.append(e)
        if not pool:
            return []
        wins = self.wins()
        rng = random.Random(seed)
        picked: list[Entry] = []
        weights = {e.id: max(1.0, len(wins.get(e.id, []))) / (1.0 + MINED_DISCOUNT * e.children)
                   for e in pool}
        while pool and len(picked) < n:
            total = sum(weights[e.id] for e in pool)
            draw, cursor = rng.random() * total, 0.0
            for i, e in enumerate(pool):
                cursor += weights[e.id]
                if cursor >= draw:
                    picked.append(pool.pop(i))
                    break
            else:                                  # floating point ran off the end
                picked.append(pool.pop())
        for e in picked:
            e.children += 1
        return picked

    def summary(self) -> dict[str, Any]:
        wins = self.wins()
        front = self.frontier()
        return {"candidates": len(self.entries),
                "flat": sum(1 for e in self.entries if e.flat),
                "contested": len(self.contested()),
                "frontier": len(front),
                "instances": len(self.instance_best()),
                "generations": len({e.generation for e in self.entries}),
                "promoted": sum(1 for e in self.entries if e.promoted),
                "widest": max(((len(v), k) for k, v in wins.items()), default=(0, ""))[0]}


def _dominates(a: Entry, b: Entry) -> bool:
    """``a`` is at least as good as ``b`` everywhere they overlap, and better somewhere."""
    shared = set(a.scores) & set(b.scores)
    if len(shared) < 4:
        return False
    better = False
    for instance in shared:
        if a.scores[instance] < b.scores[instance]:
            return False
        if a.scores[instance] > b.scores[instance]:
            better = True
    return better


def from_gepa_state(run_dir: str | Path, generation: str, instance_ids: list[str],
                    fingerprint_of, *, promoted_id: str | None = None) -> list[Entry]:
    """Every candidate a finished GEPA search proposed, with its per-instance scores.

    All of this was already on disk and none of it was ever read. ``gepa_state.bin``
    holds ``program_candidates`` beside ``prog_candidate_val_subscores`` — a
    candidates-by-instances matrix — and ``parent_program_for_candidate``, which
    is the ancestry within the search. Three rejected generations' worth of it is
    sitting in ``runs/`` right now and can be back-filled without paying again.

    ``instance_ids`` must be the valset in the order it was handed to GEPA;
    the subscore lists are positional against it.
    """
    state_path = Path(run_dir) / "gepa" / "gepa_state.bin"
    if not state_path.exists():
        return []
    try:
        state = pickle.loads(state_path.read_bytes())
    except Exception:
        return []

    candidates = state.get("program_candidates") or []
    subscores = state.get("prog_candidate_val_subscores") or []
    parents = state.get("parent_program_for_candidate") or []
    found_at = state.get("num_metric_calls_by_discovery") or []

    out: list[Entry] = []
    ids: list[str] = []
    for k, components in enumerate(candidates):
        entry_id = fingerprint_of(components)
        ids.append(entry_id)
        # A row is a dict keyed by the instance's position in the valset, not a
        # list. Read as a list it enumerates its own keys, and every candidate
        # comes out with the identical score row 0..N-1 — thirteen candidates,
        # one distinct row, a mean of 50.5 on a scale that tops out at 1. Handle
        # both shapes, because the pickle is somebody else's schema.
        row = subscores[k] if k < len(subscores) else {}
        pairs = row.items() if isinstance(row, dict) else enumerate(row or [])
        scores = {instance_ids[i]: float(v) for i, v in pairs
                  if isinstance(i, int) and 0 <= i < len(instance_ids) and v is not None}
        parent_idx = parents[k] if k < len(parents) else None
        if isinstance(parent_idx, list):           # merges carry more than one
            parent_idx = parent_idx[0] if parent_idx else None
        out.append(Entry(
            id=entry_id, components=dict(components), generation=generation,
            parent=ids[parent_idx] if isinstance(parent_idx, int) and parent_idx < len(ids) else None,
            scores=scores,
            discovered_after_calls=int(found_at[k]) if k < len(found_at) else 0,
            promoted=entry_id == promoted_id))
    return out
