"""The task: a harness is put back at an instant of a finished match, with the
book, the price path and the tape frozen there, and asked where the mid goes in
five minutes. The answer is in the candle history."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import sys
from typing import Any

from ...harness import Run, Toolbox, model_tool_names
from ...kalshi._history import History
from ...kalshi.replay import MatchTimeline, ToolCache, match_timeline, replay_tools
from ...loop import Outcome, Settings
from .score import WindowScore, pooled, pooled_skill, score_output
from .windows import Window, build_windows, load_fixtures

#: What a run that produced no usable forecast loses against deliberate silence,
#: on the optimizer's value scale. Half a cent of edge: enough that no crash can
#: tie a quiet forecast, small enough that a candidate genuinely better where it
#: does run still outranks one that is merely never broken.
BREAKAGE = 0.05

BACKGROUND = """The harness forecasts the short-term path of a Kalshi sports contract while the
match is being played. It is given {{question}}, the contract ticker, and {{game}}, the
score, clock and recent events as they stood at that instant. It may call three tools,
each frozen at that instant: market_quote (the book), candlesticks (minute bars up to
now) and previous_trades (the print tape). Nothing else exists: no news, no live game
feed, because those cannot be replayed honestly.

Its last step must return a JSON object with numeric delta_cents, how many cents the
mid will move over the next five minutes (0 is a real answer, and the right one on a
quiet book), and half_width_cents, half the width of the two-sided quote it would
stand behind. Code applies delta_cents to the exchange's mid; the harness never states
a price level.

Scoring is skill against no-change: 1 minus (its error / the error of predicting no
move), pooled over windows by summing errors. A harness that copies the current mid
scores exactly zero skill. Negative skill is worse than silence. The one thing that
earns skill is calling the direction and rough size of moves that actually happen:
goals and cards that the price has not absorbed yet, time decay on a draw or a lead
as the clock runs down, and thin books drifting back after a single order moved them.
Round-trip fees on Kalshi are two to three cents, the same size as typical moves, so a
useful forecast is one confident enough to be worth trading."""


class KalshiHorizon:
    name = "kalshi-horizon-5m"
    background = BACKGROUND
    inputs = frozenset({"question", "game"})

    def __init__(self, *, fixtures=(), history: History | None = None, every_minutes: int = 5,
                 windows_dir: str | None = None, cache_dir: str | None = None,
                 windows: list[Window] | None = None,
                 per_fixture: int = 0) -> None:
        self.fixtures = list(fixtures)
        self.history = history or History()
        self.every_minutes = every_minutes
        self.windows_dir = windows_dir
        self.tool_cache = ToolCache(f"{cache_dir}/tools") if cache_dir else None
        self._windows = windows
        #: Cap on windows kept per match. Statistical power comes from matches —
        #: the gate resamples by match — so thirty-four windows of one game are
        #: thirty-four correlated observations bought at thirty-four times the
        #: price of eight. Zero keeps all of them.
        self.per_fixture = per_fixture
        self._timelines: dict[str, MatchTimeline | None] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "KalshiHorizon":
        return cls(fixtures=load_fixtures(settings.benchmark), every_minutes=settings.every,
                   windows_dir=settings.windows_dir, cache_dir=settings.cache_dir,
                   per_fixture=getattr(settings, "per_fixture", 0))

    # -- Task --

    def instances(self) -> list[Window]:
        if self._windows is None:
            built = build_windows(self.fixtures, history=self.history,
                                  every_minutes=self.every_minutes, windows_dir=self.windows_dir,
                                  log=lambda m: print(m, file=sys.stderr))
            self._windows = _thin(built, self.per_fixture)
        return self._windows

    def tools(self) -> list[str]:
        """Every tool a harness on this topic may name.

        Named rather than derived. The box is built per window and three of its
        tools need that window's timeline, so probing it with a made-up instant
        reports a box narrower than any real one — which is exactly the mistake
        that told the rewriter only three tools existed.
        """
        stand_in = MatchTimeline(game_id="", league="", home="", away="",
                                 kickoff=datetime(2020, 1, 1, tzinfo=timezone.utc),
                                 events=[])
        return sorted(set(replay_tools(datetime(2020, 1, 1, tzinfo=timezone.utc),
                                       self.history, self.tool_cache, line=stand_in))
                      | set(model_tool_names()))

    def _timeline(self, window: Window) -> MatchTimeline | None:
        """The match this window belongs to, built once per match.

        Every window of a fixture shares one, and a fixture has dozens — asking
        the feed for each would be a hundred and seventy calls per generation
        for the same answer.
        """
        key = window.game.get("game_id") if isinstance(window.game, dict) else None
        if not key:
            return None
        if key not in self._timelines:
            league = (window.game or {}).get("league") or "EPL"
            try:
                self._timelines[key] = match_timeline(league, str(key))
            except Exception:
                self._timelines[key] = None
        return self._timelines[key]

    def toolbox(self, window: Window) -> Toolbox:
        # The timeline makes game state replayable: it is timestamped events, so
        # the score at an instant is a lookup rather than a guess. Without one
        # those tools are absent rather than wrong.
        return replay_tools(window.at, self.history, self.tool_cache,
                            line=self._timeline(window))

    def run_inputs(self, window: Window) -> dict[str, Any]:
        return {"question": window.ticker, "game": json.dumps(window.game)[:1200]}

    def score(self, window: Window, run: Run) -> Outcome:
        score = score_output(run.output, window.mid_now, window.realised)
        return self._outcome(window, score, run)

    def failed(self, window: Window, reason: str) -> Outcome:
        """A harness that cannot run at all said nothing, which is what silence is.

        Scored identically to a run that finished and declined to forecast.
        Anything else would pay the optimizer to avoid breaking rather than to
        be right, and would still not match what the gate reads.
        """
        out = self._outcome(window, None, None)
        return Outcome(value=out.value,
                       feedback=f"The harness could not run: {reason} {out.feedback}",
                       objectives=out.objectives, details=out.details)

    def statistic(self, outcomes: list[Outcome]) -> float:
        """Pooled skill. A run that produced no forecast counts as silence, not as missing."""
        return pooled_skill([_score_of(o) for o in outcomes])

    def summary(self, outcomes: list[Outcome]) -> dict[str, Any]:
        out = pooled([_score_of(o) for o in outcomes])
        out["unscored"] = sum(1 for o in outcomes if not o.details.get("scored"))
        return out

    # -- internals --

    def _outcome(self, w: Window, score: WindowScore | None, run: Run | None) -> Outcome:
        """One window's result, read the same way by the optimizer and the gate.

        It was not. A run that produced nothing handed the optimizer ``0.0`` —
        the floor of the scale, what a harness gets for being ten cents wrong —
        while the gate read the same window through ``WindowScore.silent`` and
        scored it as exactly silence, which is ``0.5`` on that scale. The two
        halves of the loop disagreed by half the range about the most common
        failure there is. Direction made it conservative rather than exploitable,
        so it never showed up as a wrong promotion, but it is the same defect
        class as the dead-market gap the benchmark floor was written to kill, and
        it was quietly taxing every candidate whose rewrite was merely fragile.
        """
        cost = run.cost_usd if run else 0.0
        # Silence is a real result with a real value, not a missing one.
        reading = score or WindowScore.silent(w.mid_now, w.realised)
        # But a harness that COULD NOT run is not choosing silence, and pricing
        # the two identically built a flat spot the search found in one night:
        # gen7's rewrite called four tools its allowlist did not name, failed
        # every window at exactly 0.5, and 0.5-flat beat a parent that averaged
        # below silence - GEPA crowned the crash. A small step below silence
        # breaks the flat spot without reviving the old half-the-range gap: to
        # the optimizer, crashing is now strictly worse than shutting up, while
        # the gate's statistic still reads both as silence, which only ever
        # errs against the candidate and so cannot fabricate a promotion.
        value = reading.value if score is not None else max(0.0, reading.value - BREAKAGE)
        details = {"scored": score is not None, "mid_now": w.mid_now,
                   "realised": w.realised, **reading.to_dict()}
        return Outcome(
            value=value,
            feedback=_feedback(w, score, run),
            objectives={"skill": value,
                        "cost": max(0.0, 1.0 - cost / 0.05)},
            details=details)


def _score_of(o: Outcome) -> WindowScore:
    d = o.details
    return WindowScore(mid_now=d["mid_now"], predicted=d["predicted"], realised=d["realised"],
                       half_width=d["half_width"])


def _feedback(w: Window, s: WindowScore | None, run: Run | None) -> str:
    move = w.realised - w.mid_now
    lines = [f"{w.ticker} at {w.at.isoformat()[:16]}Z: mid {w.mid_now:.3f}, five minutes later "
             f"{w.realised:.3f} ({100 * move:+.1f}c; no-change would miss by {abs(move):.3f})."]
    if run is not None and run.error:
        lines.append(f"The run failed ({run.error_kind}): {run.error}.")
    if s is None:
        lines.append("No usable forecast: the last step must return a JSON object with numeric "
                     "delta_cents and half_width_cents. A rewrite that cannot run scores BELOW "
                     "deliberate silence - if you meant to say 'no change', return delta_cents 0 "
                     "instead of breaking. If the failure names tools the harness does not list, "
                     "the plan may only call tools already in the tools component; adding a tool "
                     "there is its own separate edit.")
    else:
        lines.append(f"Predicted {100 * (s.predicted - s.mid_now):+.1f}c with half-width "
                     f"{100 * s.half_width:.1f}c: missed by {s.error:.3f}, skill {s.skill:+.2f}"
                     + (", market did not move so nothing to measure" if s.unmeasurable else "")
                     + (", echoed the current mid" if s.echoed else "")
                     + (", realised price inside the quote" if s.covered else ", realised price outside the quote")
                     + ".")
    if run is not None:
        bad = [t for t in run.tools_seen() if t["error"]]
        if bad:
            lines.append("Tool errors: " + "; ".join(f"{t['tool']}: {t['error']}" for t in bad) + ".")
        lines.append(f"Cost ${run.cost_usd:.4f}.")
    return " ".join(lines)


def _thin(windows: list[Window], per_fixture: int) -> list[Window]:
    """At most ``per_fixture`` windows per match, spread across the match.

    Evenly spaced rather than the first N, because the first N of a football
    match are all the opening twenty minutes — the quietest part, before the
    scoreline has done anything a forecast could be wrong about.
    """
    if per_fixture <= 0:
        return windows
    by_group: dict[str, list[Window]] = {}
    for w in windows:
        by_group.setdefault(w.group, []).append(w)
    out: list[Window] = []
    for group in sorted(by_group):
        rows = sorted(by_group[group], key=lambda w: (w.at, w.ticker))
        if len(rows) <= per_fixture:
            out.extend(rows)
            continue
        step = len(rows) / per_fixture
        out.extend(rows[int(i * step)] for i in range(per_fixture))
    return out
