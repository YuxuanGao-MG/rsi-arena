"""The task: a harness is put back at the second a Benzinga item broke on a
US stock, with the tape frozen there, and asked where the price goes in five
minutes. The answer is in the minute bars."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...alpaca._bars import AlpacaBars, BarStore
from ...alpaca._client import AlpacaData
from ...alpaca._news import AlpacaNews
from ...alpaca._session import session
from ...alpaca.replay import replay_tools
from ...harness import Run, ToolCache, Toolbox, model_tool_names
from ...loop import Outcome, Settings
from ...trading import TRADING_CONTRACT, TradingSpec
from .._common.metric import MoveScore
from .._common.thin import thin
from .._common.trading import book_line
from .score import METRIC, pooled, pooled_skill, score_output, silent
from .trading import trading_spec
from .windows import DATA_DIR, BenchmarkItem, NewsWindow, build_windows, load_benchmark

#: What a run that produced no usable forecast loses against deliberate
#: silence, on the optimizer's value scale. The same step as Kalshi's, for
#: the same reason: a crash must not tie a quiet forecast.
BREAKAGE = 0.05

BACKGROUND = """The harness forecasts the five-minute path of a US stock or ETF at the second a news
item about it was published. It is given {{question}}, the symbol; {{news}}, the item as it
broke (headline, summary, source, the symbols it names, the instant); and {{context}}, where
that instant sits in the trading session. Every item is a Benzinga story timestamped to the
second during regular hours, and every tool is frozen at that second. The tools come in five
families: the tape itself (market_quote, candlesticks, recent_trades, market_at_time,
volume_profile), arithmetic with no clock in it (trading_costs, price_the_edge), the day and
the market around the name (daily_context, relative_volume, market_tape, session_clock), the
story (the_story, news_before, sibling_moves), and derived reads that do the counting for a
model that cannot (price_velocity, market_shock, move_base_rate, tape_imbalance,
news_absorption, state_summary). Nothing else exists: no web, no quote from now, no bar that
had not closed.

Its last step must return a JSON object with numeric delta_bps, how many basis points the
price will move over the next five minutes from the last complete IEX minute close (0 is a
real answer, and the right one on a headline the market has already read), and
half_width_bps, half the width of the two-sided quote it would stand behind. Code applies
delta_bps to that close; the harness never states a price level.

Scoring is skill against no-change: 1 minus (its error / the error of predicting no move),
in basis points, pooled over windows by summing errors, with the benchmark's error floored at
a five basis point tick. A harness that echoes the last close scores exactly zero skill.
Negative skill is worse than silence. What earns skill is calling the direction and rough
size of the reaction to material news - guidance, a deal, a downgrade, a recall - and
fading the headlines that are already priced: a story that repeats the morning's, a
market-wide move mistaken for a stock-specific one, a name that has already run. Large caps
quote one to five basis points wide, so a useful call is one that clears a spread."""

BACKGROUND = BACKGROUND + "\n\n" + TRADING_CONTRACT


#: What the rewriter is told about the models it may choose. Nothing has
#: been measured on this task yet; what is known is from soccer and is said
#: as such.
MODEL_NOTES = (
    "Nothing has been measured on this task yet. On the Kalshi topic anthropic/claude-opus-5 "
    "was the only chat model above silence, at about 3.5 cents a window, and "
    "openai/gpt-5-mini was near silence at a fourteenth of the price; a cheaper model that "
    "stays quiet at the right times can beat an expensive one that speaks badly, because the "
    "gate charges for cost as well as error. typesafe/jev-1.13 is different in kind: it "
    "answers typed questions with a probability distribution, writes no text, calls no "
    "tools, and costs about two thousandths of a cent a window; it can only run a plan whose "
    "prompt steps carry \"questions\" and \"answers\" (the plan grammar describes them), and "
    "a chat model cannot run such a plan. Swap to or from it only if the plan matches, or "
    "the harness fails to load.")


class NewsEquity:
    name = "news-equity-5m"
    background = BACKGROUND
    inputs = frozenset({"question", "news", "context"})
    metric = METRIC
    model_notes = MODEL_NOTES

    def __init__(self, *, items: list[BenchmarkItem] = (), bars: Any = None, news: Any = None,
                 windows_dir: str | None = None, cache_dir: str | None = None,
                 data_dir: str | Path | None = DATA_DIR, windows: list[NewsWindow] | None = None,
                 per_fixture: int = 0) -> None:
        self.items = list(items)
        client = AlpacaData()
        self.bars = bars or AlpacaBars(client, BarStore(Path(data_dir) / "bars" if data_dir else None))
        self.news = news or AlpacaNews(client)
        self.windows_dir = windows_dir
        self.tool_cache = ToolCache(f"{cache_dir}/tools-news") if cache_dir else None
        self._windows = windows
        #: Cap on windows kept per symbol-day. Power comes from groups, not
        #: from the fourth story on one name in one afternoon.
        self.per_fixture = per_fixture
        self._trading: TradingSpec | None = None

    @property
    def trading(self) -> TradingSpec:
        """How a window of this task becomes a paper trade in the shares."""
        if self._trading is None:
            self._trading = trading_spec(self)
        return self._trading

    @classmethod
    def from_settings(cls, settings: Settings) -> "NewsEquity":
        return cls(items=load_benchmark(settings.benchmark), windows_dir=settings.windows_dir,
                   cache_dir=settings.cache_dir, per_fixture=getattr(settings, "per_fixture", 0))

    # -- Task --

    def instances(self) -> list[NewsWindow]:
        if self._windows is None:
            built = build_windows(self.items, bars=self.bars, windows_dir=self.windows_dir,
                                  log=lambda m: print(m, file=sys.stderr))
            self._windows = thin(built, self.per_fixture, key=lambda w: (w.at, w.symbol))
        return self._windows

    def use_instances(self, windows: list[NewsWindow]) -> None:
        """Score exactly these rather than the benchmark's. For paired comparisons."""
        self._windows = list(windows)

    def instance_from_dict(self, d: dict[str, Any]) -> NewsWindow:
        return NewsWindow.from_dict(d)

    def moved(self, window: NewsWindow) -> bool:
        """Did the price move at least a tick over the horizon?"""
        return self.metric.moved(window.mid_now, window.realised)

    def label(self, window: NewsWindow) -> str:
        return f"{window.symbol} at {window.at.isoformat()[:19]}Z: {window.headline[:70]}"

    def context_of(self, window: NewsWindow) -> dict[str, Any]:
        """The world at the instant, as the harness was shown it."""
        return {"headline": window.headline, "source": window.source,
                "symbols": list(window.symbols), "session": session(window.at)}

    def tools(self) -> list[str]:
        """Every tool a harness on this topic may name. The box is the same at
        every instant, so a stand-in instant reports it exactly."""
        stand_in = datetime(2020, 1, 6, 15, 0, tzinfo=timezone.utc)
        return sorted(set(replay_tools(stand_in, self.bars, self.news, self.tool_cache))
                      | set(model_tool_names()))

    def toolbox(self, window: NewsWindow) -> Toolbox:
        return replay_tools(window.at, self.bars, self.news, self.tool_cache, item=window.item(),
                            symbols_in_story=window.symbols)

    def run_inputs(self, window: NewsWindow) -> dict[str, Any]:
        news = {"headline": window.headline, "summary": window.summary[:600],
                "source": window.source, "symbols": list(window.symbols),
                "at": window.at.isoformat()}
        return {"question": window.symbol, "news": json.dumps(news)[:1200],
                "context": json.dumps(session(window.at))}

    def score(self, window: NewsWindow, run: Run) -> Outcome:
        return self._outcome(window, score_output(run.output, window.mid_now, window.realised), run)

    def failed(self, window: NewsWindow, reason: str) -> Outcome:
        """A harness that cannot run at all said nothing, which is what silence is."""
        out = self._outcome(window, None, None)
        return Outcome(value=out.value, feedback=f"The harness could not run: {reason} {out.feedback}",
                       objectives=out.objectives, details=out.details)

    def statistic(self, outcomes: list[Outcome]) -> float:
        """Pooled skill. A run that produced no forecast counts as silence, not as missing."""
        return pooled_skill([_score_of(o) for o in outcomes])

    def summary(self, outcomes: list[Outcome]) -> dict[str, Any]:
        out = pooled([_score_of(o) for o in outcomes])
        out["unscored"] = sum(1 for o in outcomes if not o.details.get("scored"))
        return out

    # -- internals --

    def _outcome(self, w: NewsWindow, score: MoveScore | None, run: Run | None) -> Outcome:
        """One window's result, read the same way by the optimizer and the gate.
        ``kalshi_horizon/task.py`` has the history of why."""
        cost = run.cost_usd if run else 0.0
        reading = score or silent(w.mid_now, w.realised)
        value = reading.value if score is not None else max(0.0, reading.value - BREAKAGE)
        details = {"scored": score is not None, "mid_now": w.mid_now, "realised": w.realised,
                   **reading.to_dict()}
        return Outcome(value=value, feedback=_feedback(w, score, run),
                       objectives={"skill": value, "cost": max(0.0, 1.0 - cost / 0.05)},
                       details=details)


def _score_of(o: Outcome) -> MoveScore:
    d = o.details
    return MoveScore(mid_now=d["mid_now"], predicted=d["predicted"], realised=d["realised"],
                     half_width=d["half_width"], metric=METRIC)


def _feedback(w: NewsWindow, s: MoveScore | None, run: Run | None,
              trade: dict[str, Any] | None = None) -> str:
    """``trade`` is the paper book's record, present only after a replay."""
    move = METRIC.move(w.mid_now, w.realised)
    lines = [f"{w.symbol} at {w.at.isoformat()[:19]}Z on \"{w.headline[:80]}\": last {w.mid_now:.2f}, "
             f"five minutes later {w.realised:.2f} ({move:+.1f} bps; no-change would miss by "
             f"{abs(move):.1f} bps)."]
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
                     + (", market did not move so nothing to measure" if s.unmeasurable else "")
                     + (", echoed the last close" if s.echoed else "")
                     + (", realised price inside the quote" if s.covered
                        else ", realised price outside the quote")
                     + ".")
    if run is not None:
        bad = [t for t in run.tools_seen() if t["error"]]
        if bad:
            lines.append("Tool errors: " + "; ".join(f"{t['tool']}: {t['error']}" for t in bad) + ".")
        lines.append(f"Cost ${run.cost_usd:.4f}.")
    if trade:
        lines.append(book_line(trade))
    return " ".join(lines)


__all__ = ["NewsEquity", "BACKGROUND", "MODEL_NOTES", "BREAKAGE"]
