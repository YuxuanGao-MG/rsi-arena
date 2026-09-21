"""The judgment is paid for before the search spends.

A generation is one pot of money and three buyers: the incumbent's baseline,
the search, and the judgment of whatever the search returns - the probe and
the held-out set, scored on the candidate. Until now the pot was shared in
the order the buyers arrived, which is the order that pays the judge last.

gen11 is what that costs. The ceiling was $64.26, priced by preflight at the
incumbent's 3.3 cents a window. The search ran its 600 calls and the one full
valset pass GEPA always adds, but on candidates that carried a second prompt
step and cost 5.5 cents a window; the cascade then paid 80 windows at that
price; and held-out got $6.50 of the $22 it needed. 339 of 400 windows scored
as silence, the gate refused to read the comparison, and $64.48 bought
nothing at all. The money was not short by $64 - it was short by about $8,
and the structure turned $8 into everything.

So the pot is split before the search sees it. What judging a candidate can
cost is known once the baseline is scored: the incumbent's measured rate,
times the windows the judgment needs, times the most the gate will let a
candidate cost relative to the incumbent - anything dearer is rejected on
cost, so anything that reaches held-out fits the reserve by construction.
The search gets the rest, and stops on dollars rather than on calls: when it
can no longer afford to finish another accepted candidate's full evaluation
without eating the reserve, it stops proposing. The judgment is then paid
from money nobody else could touch.

Everything here is arithmetic over rollouts and a ceiling; none of it knows
what a window is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .task import Rollout


def fresh_rate(rollouts: Sequence[Rollout]) -> float:
    """Dollars a window when it was actually run - remembered answers excluded.

    A memoised rollout reports the cost it was bought for, which is the right
    number for the gate's cost ratio and the wrong one here: the question is
    what the *next* window will cost, and a remembered window costs nothing.
    """
    run = [r for r in rollouts if r.run is not None]
    if not run:
        return 0.0
    return sum(r.run.cost_usd for r in run) / len(run)


def any_rate(rollouts: Sequence[Rollout]) -> float:
    """Dollars a window, remembered or bought. What the incumbent costs to run."""
    if not rollouts:
        return 0.0
    return sum(r.cost_usd for r in rollouts) / len(rollouts)


@dataclass(frozen=True)
class Reserve:
    """What judging one candidate may cost, kept back from the search."""

    windows: int          # probe plus held-out
    rate: float           # the incumbent's dollars a window
    ratio: float          # the most a candidate may cost relative to it

    @property
    def usd(self) -> float:
        return self.windows * self.rate * self.ratio

    def describe(self) -> str:
        return (f"${self.usd:.2f} to judge a candidate: {self.windows} windows at "
                f"${self.rate:.4f} each, up to {self.ratio:.1f}x the incumbent")


def judgment_reserve(rollouts: Sequence[Rollout], windows: int, ratio: float,
                     fallback_rate: float) -> Reserve:
    """Price the judgment off the incumbent's own rollouts.

    ``fallback_rate`` covers a baseline that reports no cost at all - every
    answer remembered from a scoreboard written before costs were recorded -
    because a reserve of zero is the old behaviour under a new name.
    """
    rate = any_rate(rollouts)
    return Reserve(windows=windows, rate=rate if rate > 0 else fallback_rate, ratio=ratio)


class SpendStopper:
    """Stops GEPA when the search can no longer afford to finish a candidate.

    GEPA checks its stoppers between iterations, and an iteration that accepts
    a candidate goes on to score it on the whole valset before the next check.
    So the line is drawn one iteration early: the search stops when what is
    spent, plus what one more accepted candidate would cost at the rate the
    search is currently paying, would cross into the reserve. The rate is the
    search's own - candidates are dearer than the incumbent when they grow
    the plan, and cheaper when they swap the model - with the incumbent's as
    the answer before any candidate has run.
    """

    def __init__(self, spent: Any, ceiling_usd: float, *, lookahead_windows: int,
                 rate_of: Any, fallback_rate: float) -> None:
        #: Anything with a ``spent_usd`` attribute: the model client.
        self.spent = spent
        self.ceiling_usd = ceiling_usd
        self.lookahead_windows = lookahead_windows
        #: Anything with a ``rate`` attribute: the adapter, which watches every
        #: window the search runs.
        self.rate_of = rate_of
        self.fallback_rate = fallback_rate
        self.fired = False
        self.spent_at_stop: float | None = None

    @property
    def rate(self) -> float:
        rate = float(getattr(self.rate_of, "rate", 0.0) or 0.0)
        return rate if rate > 0 else self.fallback_rate

    def __call__(self, gepa_state: Any = None) -> bool:
        spent = float(getattr(self.spent, "spent_usd", 0.0) or 0.0)
        if spent + self.lookahead_windows * self.rate >= self.ceiling_usd:
            if not self.fired:
                self.spent_at_stop = spent
            self.fired = True
        return self.fired

    def describe(self) -> str:
        return (f"the search stopped for money at ${self.spent_at_stop or 0.0:.2f}: another "
                f"accepted candidate would cost about "
                f"${self.lookahead_windows * self.rate:.2f} to evaluate and the search's "
                f"share ends at ${self.ceiling_usd:.2f}")


def cascade_verdict(candidate: Sequence[Rollout], incumbent: Sequence[Rollout], *,
                    gap: float, floor: float, max_unscored: float,
                    max_cost_ratio: float) -> str | None:
    """Why the probe alone is enough to stop, or None to go on to held-out.

    Three ways a candidate can be refused before held-out is paid for, in the
    order they are cheapest to know. A harness that mostly could not run has
    nothing to confirm (gen7 paid twelve dollars to confirm one). One below the
    floor would only be confirmed worse. And one dearer than the gate allows is
    rejected on cost however well it scores - the gate's own check, asked of
    the probe, where it costs a fifth as much to ask and keeps the reserve's
    promise: nothing that reaches held-out costs more than was kept back for it.
    """
    n = max(1, len(candidate))
    unscored = sum(1 for r in candidate if not r.outcome.details.get("scored"))
    if unscored / n > max_unscored:
        return (f"{unscored} of {len(candidate)} probe windows never ran; held-out would "
                f"be paying to confirm a broken harness")
    if gap < floor:
        return f"{gap:+.3f} is below {floor:+.3f}, and held-out would only confirm it"
    cand_rate, inc_rate = fresh_rate(candidate), any_rate(incumbent)
    if inc_rate > 0 and cand_rate > max_cost_ratio * inc_rate:
        return (f"it costs {cand_rate / inc_rate:.1f}x the incumbent a window on the probe "
                f"(limit {max_cost_ratio:.1f}x), so the gate would refuse it on cost "
                f"whatever held-out said")
    return None


def holdout_shortfall(windows: int, rate: float, remaining_usd: float | None) -> str | None:
    """Why held-out cannot be started, or None if it can.

    The reserve should make this unreachable, and it is kept because the
    reserve is priced off a rate that can be stale: a scoreboard remembers
    what the incumbent cost under the model it had then. Half a held-out set
    is worse than none - the gate reads the missing half as silence - so
    either all of it is affordable or none of it is bought.
    """
    if remaining_usd is None:
        return None
    need = windows * rate
    if remaining_usd >= need:
        return None
    return (f"held-out would cost about ${need:.2f} at the candidate's ${rate:.4f} a "
            f"window and ${remaining_usd:.2f} remains; half a held-out set reads as "
            f"silence, so none of it was bought")
