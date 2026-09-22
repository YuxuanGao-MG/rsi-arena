"""Everything a run of the loop is parameterised by, in one place."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Settings:
    topic: str = "kalshi-horizon-5m"
    harness: str = "harnesses/horizon-5m.json"     # a harness file, or a run directory to continue from
    # What the loop runs on, not the five-match set it was written against.
    # These four were the workflow's literals for a month while the dataclass
    # still said epl-2026-09 / 2 / 0 / 0; the first topic's TopicSpec is read
    # off this object, and the workflow now reads the spec, so the dataclass
    # has to be the scheduled run or the scheduled run would have changed.
    benchmark: str = "benchmarks/soccer-2026.json"
    windows_dir: str = "benchmarks/windows"         # the question set, built once and versioned
    # Instance groups (fixtures) the optimizer never sees. A hundred, because
    # the gate's interval narrows with matches and thirty-five could not
    # resolve any rewrite anyone has produced.
    holdout: int = 100
    # Matches cut away before anything else and never shown to the search or the
    # gate. Scored only to confirm a promotion. Free until something is promoted,
    # which is why it can be generous.
    audit: int = 60
    # Which turn of the loop this is, and how often the held-out matches rotate.
    # Rotation bounds how many times one set of matches can be queried at a
    # one-sided 2.5% threshold; rotating every generation would be stricter and
    # would also pay for the incumbent's held-out evaluation again every time.
    generation: int = 0
    holdout_rotate_every: int = 4
    seed: int = 0
    every: int = 5                                 # minutes between windows
    # Cap on windows kept per match; 0 keeps all. Four: a match's windows share
    # a scoreline and a horizon, so power comes from matches, and four takes a
    # cold generation from $86 to $53.
    per_fixture: int = 4
    model: str | None = None                       # override the harness model
    # Models the search may put in a rewrite. Measured on the same 68 held-out
    # windows before any of this ran on a schedule: Opus 5 +0.106 at $0.035 a
    # window, gpt-5-mini -0.008 at $0.0025, Sonnet 4.5 -0.011 at $0.013. Three
    # entries, all under the per-window ledger; a name outside the list fails
    # the harness before a call is made, and scores like any other breakage.
    model_choices: tuple = ("anthropic/claude-opus-5",
                            "anthropic/claude-sonnet-4.5",
                            "openai/gpt-5-mini",
                            # A decisions model: answers typed questions with
                            # probabilities, writes nothing, and costs about two
                            # thousandths of a cent a window. A plan for it ends
                            # in a prompt step with "questions"; see harnesses/
                            # horizon-5m-jev.json.
                            "typesafe/jev-1.13")
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
    # The least an incumbent is priced at when the ratio is applied. A Jev
    # incumbent runs at about $0.00015 a window, and twice nothing is nothing:
    # any candidate that asks Opus a single question (about $0.03) would fail
    # on cost by construction, which forbids exactly the delegation the model
    # tools exist for. A penny a window is the price the gate compares against
    # when the incumbent is cheaper than that; an incumbent dearer than the
    # floor is priced at its own rate as before.
    cost_floor_usd: float = 0.01
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
    # Dollars a window before anything in this generation has been measured:
    # what preflight prices the split at, and what the judgment reserve falls
    # back to when the baseline was served from a scoreboard that recorded no
    # cost. Measured, from gen5: $12.08 for 357 windows that reached Opus 5.
    window_usd: float = 0.034
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
    # Where the archive and the scoreboard live, and where run directories are
    # made. One root for every topic; the files inside it carry the topic's
    # name once there is more than one (see ``Archive.path_for``).
    runs_dir: str = "runs"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
