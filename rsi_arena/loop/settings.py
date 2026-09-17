"""Everything a run of the loop is parameterised by, in one place."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Settings:
    topic: str = "kalshi-horizon-5m"
    harness: str = "harnesses/horizon-5m.json"     # a harness file, or a run directory to continue from
    benchmark: str = "benchmarks/epl-2026-09.json"
    windows_dir: str = "benchmarks/windows"         # the question set, built once and versioned
    holdout: int = 2                               # instance groups (fixtures) the optimizer never sees
    # Matches cut away before anything else and never shown to the search or the
    # gate. Scored only to confirm a promotion. Free until something is promoted,
    # which is why it can be generous.
    audit: int = 0
    # Which turn of the loop this is, and how often the held-out matches rotate.
    # Rotation bounds how many times one set of matches can be queried at a
    # one-sided 2.5% threshold; rotating every generation would be stricter and
    # would also pay for the incumbent's held-out evaluation again every time.
    generation: int = 0
    holdout_rotate_every: int = 4
    seed: int = 0
    every: int = 5                                 # minutes between windows
    per_fixture: int = 0                           # cap windows kept per match; 0 keeps all
    model: str | None = None                       # override the harness model
    # The model that reads traces and rewrites harnesses. Deliberately the
    # strongest available and deliberately not the one under test: it runs tens
    # of times a generation against the task model's thousands, so it is under
    # three per cent of the bill, and rewriting a harness from its own failures
    # is the part of the loop that most rewards reasoning.
    reflection_model: str = "anthropic/claude-opus-5"
    cache_dir: str = ".cache"
    llm_cache: bool = True
    # Reuse an outcome already paid for rather than buying it again. The
    # incumbent is the same harness on the same windows every generation until
    # something is promoted, which was nearly a third of the bill.
    reuse_scores: bool = True
    concurrency: int = 4
    # Window evaluations GEPA may spend searching. At a minibatch of eight this
    # is about sixty reflection rounds — enough to try a dozen rewrites and keep
    # the ones that survive, which is where the earlier budget of 200 fell down:
    # it bought exactly one rewrite before running out.
    #
    # The search is the largest recurring line now that the probe replaced the
    # full train evaluation, so it is the number to revisit if a generation ever
    # gets accepted and the loop is worth running deeper.
    max_metric_calls: int = 600                    # instance evaluations GEPA may spend
    # Instances GEPA scores a candidate on to place it on its Pareto frontier.
    #
    # Not the train split, which is what it used to be, and that quietly stopped
    # the search from running at all. GEPA scores the seed across the whole
    # valset before it consults the stop condition, so a valset larger than
    # `max_metric_calls` spends the entire budget before proposing a single
    # rewrite. Four generations ran that way: gen5 spent 2600 calls of a 600
    # budget and returned exactly one candidate, the seed, which was then gated
    # and reported as a verdict.
    #
    # Sized so the seed evaluation leaves room to iterate: at 600 calls, 240
    # leaves about 360, which buys roughly forty-five proposals at a minibatch
    # of eight.
    valset: int = 240
    minibatch: int = 8                             # instances per reflection step
    max_cost_ratio: float = 2.0                    # a candidate may cost at most this times the incumbent
    # What a whole generation may spend. The per-window ledger caps one run at
    # twenty cents; until today nothing capped the thousand runs around it, so
    # the real ceiling was `max_metric_calls` times whatever a window happened
    # to cost — and a candidate that grows the context roughly doubles that.
    # Sized against what a generation measurably costs, which is not what it was
    # estimated to cost. gen5 paid $12.08 for 357 windows that reached the model:
    # 3.4 cents each, against the 1.3 the docs had claimed since a different task
    # model. At a hundred held-out matches and four windows each, a cold
    # generation is about $53 and a warm one - the incumbent's held-out rollouts
    # still cached - is nearer $35. Sixty is a backstop, and `scripts/preflight.py`
    # now refuses to run a split the ceiling cannot buy.
    max_generation_usd: float = 60.0
    # Train matches the candidate is scored on. Doubles as the regression
    # check, so the full train set is never re-scored: the gate asks of train
    # only "did this get worse", which twenty matches answer as well as a
    # hundred and forty at a fifteenth of the price. Held-out is what needs
    # power, and held-out is scored in full.
    cascade: int = 20                              # 0 disables and scores all of train
    # A candidate half a point behind the incumbent on the probe is rejected
    # without paying for the rest. Tight on purpose: the full evaluation is two
    # thirds of a generation's bill, no candidate has yet cleared the gate, and
    # the cost of cutting one that would have is one wasted generation rather
    # than a wrong result — the gate is still the only way in.
    #
    # Worth loosening once something is accepted, because then a near miss is
    # evidence rather than noise.
    cascade_floor: float = -0.005
    run_dir: str = "runs/latest"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
