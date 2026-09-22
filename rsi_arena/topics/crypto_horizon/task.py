"""The task: a harness is put back at an instant of the spot market, with the
bars, the tape, the perp and the chain frozen there, and asked where the
price goes over the next minute. The answer is in the bars."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...crypto._binance import BinanceSpot, KlineStore
from ...crypto._futures import FuturesStore
from ...crypto._onchain import OnchainStore
from ...crypto.replay import replay_tools
from ...harness import Run, ToolCache, Toolbox, model_tool_names
from ...loop import Outcome, Settings
from .._common.thin import thin
from .score import METRIC, MoveScore, pooled, pooled_skill, score_from_details, score_output, silent
from .windows import Benchmark, CryptoWindow, build_windows, load_benchmark

#: What a run that produced no usable forecast loses against deliberate
#: silence, on the optimizer's value scale. The Kalshi task says why.
BREAKAGE = 0.05

BACKGROUND = """The harness forecasts the very short-term path of a spot cryptocurrency price on
Binance. It is given {{question}}, the symbol (BTCUSDT, ETHUSDT or SOLUSDT), and
{{context}}, the calendar at that instant: the UTC hour, the weekday, and the minutes to
the next funding settlement. The market trades around the clock, and the instant is one
of a fixed cadence through every UTC day. Its tools are frozen at that instant and come
in five families: the bars and the tape (market_quote, candlesticks, recent_trades,
market_at_time, volume_profile), arithmetic with no clock in it (trading_fees,
price_the_edge, session_clock), the perpetual beside the spot (funding_rate,
open_interest, basis), the chain and the flows (chain_fees, chain_activity,
stablecoin_supply, dex_volume; daily figures are known only after the day they describe
has closed), and derived reads that do the counting for a model that cannot
(price_velocity, market_shock, cross_asset, hourly_seasonality, move_base_rate,
tape_imbalance, shock_absorption, state_summary). Nothing else exists: no news, no order
book, because those cannot be replayed honestly.

Its last step must return a JSON object with numeric delta_bps, how many basis points
the price will move over the next {horizon} minute{plural} from the last complete
one-minute close (0 is a real answer, and the right one most of the time), and
half_width_bps, half the width of the two-sided quote it would stand behind. Code applies
delta_bps to the last close; the harness never states a price level. A tick is
{tick:g} bps: a move smaller than that did not happen.

Scoring is skill against no-change: 1 minus (its error / the error of predicting no
move), pooled over windows by summing errors, with the benchmark's error floored at one
tick. A harness that says zero scores exactly zero skill. Negative skill is worse than
silence. The one thing that earns skill is calling the direction and rough size of moves
that actually happen: the minutes around a funding reset, a perp premium snapping back to
spot, the US cash open, a liquidation cascade visible as a taker-sell surge and a drop in
open interest, and one major leading the others by a minute. Round-trip fees on spot are
about 20 bps, several times a typical {horizon}-minute move, so a useful forecast is one
confident enough to be worth trading, which is rare."""


#: What the rewriter is told about the models it may choose, measured on this
#: task. Nothing has been measured yet; what is known is what kind of model
#: each is.
MODEL_NOTES = (
    "Nothing measured yet on this task. typesafe/jev-1.13 answers typed questions with a "
    "probability distribution, writes no text, calls no tools, and costs about two "
    "thousandths of a cent a window; it can only run a plan whose prompt steps carry "
    "\"questions\" and \"answers\" (the plan grammar describes them), and a chat model "
    "cannot run such a plan. openai/gpt-5-mini is a chat model at a fraction of a cent a "
    "window. Swap to or from Jev only if the plan matches, or the harness fails to load.")


class CryptoHorizon:
    name = "crypto-horizon-1m"
    inputs = frozenset({"question", "context"})
    #: The unit the move is measured in: basis points of the price at the instant.
    metric = METRIC
    model_notes = MODEL_NOTES
    background = BACKGROUND.format(horizon=1, plural="", tick=METRIC.tick)

    def __init__(self, *, benchmark: Benchmark | None = None, spot: Any = None,
                 futures: Any = None, onchain: Any = None, every_minutes: int | None = None,
                 horizon: int | None = None, windows_dir: str | None = None,
                 cache_dir: str | None = None, windows: list[CryptoWindow] | None = None,
                 per_fixture: int = 0) -> None:
        self.benchmark = benchmark
        self.spot = spot
        self.futures = futures
        self.onchain = onchain
        self.every_minutes = int(every_minutes or (benchmark.every if benchmark else 5))
        self.horizon = int(horizon or (benchmark.horizon if benchmark else 1))
        self.symbols = tuple(benchmark.symbols) if benchmark else ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        self.windows_dir = windows_dir
        self.tool_cache = ToolCache(f"{cache_dir}/tools") if cache_dir else None
        self._windows = windows
        #: Cap on windows kept per day. Power comes from days, not from instants within one.
        self.per_fixture = per_fixture
        self.background = BACKGROUND.format(horizon=self.horizon,
                                            plural="" if self.horizon == 1 else "s",
                                            tick=self.metric.tick)

    @classmethod
    def from_settings(cls, settings: Settings) -> "CryptoHorizon":
        """The benchmark file names the cadence and the horizon.

        ``settings.every`` is the flag the Kalshi topic reads; here the
        benchmark file wins, because the question set on disk was built at the
        cadence discovery wrote into it and a flag that disagreed would build a
        second question set beside the first.
        """
        bench = load_benchmark(settings.benchmark)
        data = Path(bench.data_dir)
        # Bars come off the disk and nowhere else: a replay is offline by construction.
        return cls(benchmark=bench, spot=BinanceSpot(KlineStore(data / "klines"), fetch_missing=False),
                   futures=FuturesStore(data), onchain=OnchainStore(data / "onchain"),
                   every_minutes=bench.every, horizon=bench.horizon,
                   windows_dir=settings.windows_dir, cache_dir=settings.cache_dir,
                   per_fixture=getattr(settings, "per_fixture", 0))

    # -- Task --

    def instances(self) -> list[CryptoWindow]:
        if self._windows is None:
            if self.benchmark is None or self.spot is None:
                self._windows = []
            else:
                built = build_windows(self.benchmark, self.spot, every_minutes=self.every_minutes,
                                      horizon=self.horizon, windows_dir=self.windows_dir,
                                      log=lambda m: print(m, file=sys.stderr))
                # Ordered by symbol first: the rows of a day interleave three
                # coins, and an evenly spaced pick over an interleaving lands on
                # the same coin every time. Symbol-major, each coin's day is a
                # contiguous run and the pick walks through all of them.
                self._windows = thin(built, self.per_fixture, key=lambda w: (w.symbol, w.at))
        return self._windows

    def use_instances(self, windows: list[CryptoWindow]) -> None:
        """Score exactly these rather than the benchmark's. For paired comparisons."""
        self._windows = list(windows)

    def instance_from_dict(self, d: dict[str, Any]) -> CryptoWindow:
        return CryptoWindow.from_dict(d)

    def moved(self, window: CryptoWindow) -> bool:
        """Did the price move at least a tick over the horizon?"""
        return self.metric.moved(window.mid_now, window.realised)

    def label(self, window: CryptoWindow) -> str:
        return f"{window.symbol} at {window.at.isoformat()[:16]}Z"

    def context_of(self, window: CryptoWindow) -> dict[str, Any]:
        """The world at the instant, as the harness was shown it."""
        return window.context

    def tools(self) -> list[str]:
        """Every tool a harness on this topic may name.

        Read off a box built at a stand-in instant: every tool here exists
        whatever the instant, and one that lacks its source answers
        "unavailable" rather than going missing, so the stand-in box is the
        real box's width.
        """
        box = replay_tools(datetime(2020, 1, 1, tzinfo=timezone.utc), self.spot, None, self.tool_cache,
                           symbols=self.symbols, futures=self.futures, onchain=self.onchain,
                           horizon=self.horizon)
        return sorted(set(box) | set(model_tool_names()))

    def toolbox(self, window: CryptoWindow) -> Toolbox:
        return replay_tools(window.at, self.spot, None, self.tool_cache, symbols=self.symbols,
                            futures=self.futures, onchain=self.onchain, horizon=self.horizon)

    def run_inputs(self, window: CryptoWindow) -> dict[str, Any]:
        return {"question": window.symbol, "context": json.dumps(window.context)[:1200]}

    def score(self, window: CryptoWindow, run: Run) -> Outcome:
        return self._outcome(window, score_output(run.output, window.mid_now, window.realised), run)

    def failed(self, window: CryptoWindow, reason: str) -> Outcome:
        """A harness that cannot run at all said nothing, which is what silence is."""
        out = self._outcome(window, None, None)
        return Outcome(value=out.value,
                       feedback=f"The harness could not run: {reason} {out.feedback}",
                       objectives=out.objectives, details=out.details)

    def statistic(self, outcomes: list[Outcome]) -> float:
        """Pooled skill. A run that produced no forecast counts as silence, not as missing."""
        return pooled_skill([score_from_details(o.details) for o in outcomes])

    def summary(self, outcomes: list[Outcome]) -> dict[str, Any]:
        out = pooled([score_from_details(o.details) for o in outcomes])
        out["unscored"] = sum(1 for o in outcomes if not o.details.get("scored"))
        return out

    # -- internals --

    def _outcome(self, w: CryptoWindow, score: MoveScore | None, run: Run | None) -> Outcome:
        """One window's result, read the same way by the optimizer and the gate.

        The Kalshi task's ``_outcome`` says why silence is a real value, why a
        run that could not run is priced a step below it, and why the gate
        still reads both as silence.
        """
        cost = run.cost_usd if run else 0.0
        reading = score or silent(w.mid_now, w.realised)
        value = reading.value if score is not None else max(0.0, reading.value - BREAKAGE)
        details = {"scored": score is not None, "mid_now": w.mid_now,
                   "realised": w.realised, **reading.to_dict()}
        return Outcome(
            value=value,
            feedback=_feedback(w, score, run, self.horizon),
            objectives={"skill": value,
                        "cost": max(0.0, 1.0 - cost / 0.05)},
            details=details)


def _feedback(w: CryptoWindow, s: MoveScore | None, run: Run | None, horizon: int) -> str:
    move = METRIC.move(w.mid_now, w.realised)
    lines = [f"{w.symbol} at {w.at.isoformat()[:16]}Z: last close {w.mid_now:,.2f}, {horizon} minute"
             f"{'' if horizon == 1 else 's'} later {w.realised:,.2f} ({move:+.1f} bps; no-change would "
             f"miss by {abs(move):.1f} bps)."]
    if run is not None and run.error:
        lines.append(f"The run failed ({run.error_kind}): {run.error}.")
    if s is None:
        lines.append("No usable forecast: the last step must return a JSON object with numeric "
                     "delta_bps and half_width_bps. A rewrite that cannot run scores BELOW "
                     "deliberate silence - if you meant to say 'no change', return delta_bps 0 "
                     "instead of breaking. If the failure names tools the harness does not list, "
                     "the plan may only call tools already in the tools component; adding a tool "
                     "there is its own separate edit.")
    else:
        lines.append(f"Predicted {METRIC.move(s.mid_now, s.predicted):+.1f} bps with half-width "
                     f"{s.half_width:.1f} bps: missed by {s.error:.1f} bps, skill {s.skill:+.2f}"
                     + (", price did not move so nothing to measure" if s.unmeasurable else "")
                     + (", echoed the last close" if s.echoed else "")
                     + (", realised price inside the quote" if s.covered else ", realised price outside the quote")
                     + ".")
    if run is not None:
        bad = [t for t in run.tools_seen() if t["error"]]
        if bad:
            lines.append("Tool errors: " + "; ".join(f"{t['tool']}: {t['error']}" for t in bad) + ".")
        lines.append(f"Cost ${run.cost_usd:.4f}.")
    return " ".join(lines)


__all__ = ["CryptoHorizon", "BACKGROUND", "BREAKAGE", "MODEL_NOTES"]
