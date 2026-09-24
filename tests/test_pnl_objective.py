"""The paper book is on the search's frontier.

``TaskAdapter.evaluate`` replays each batch it scores through a book of its
own, attaches the cycle's record to the outcome the rewriter reads, and
reports ``objectives["pnl"]`` beside ``skill`` and ``cost``. The gate reads
none of it (``loop/gate.py`` promotes on skill); GEPA's frontier does.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import get_args

from rsi_arena.harness import Harness, run_sync
from rsi_arena.loop import Outcome, TaskAdapter, evaluate, reflection_templates
from rsi_arena.loop.adapter import BOOK_NOTE
from rsi_arena.topics.kalshi_horizon import KalshiHorizon, Window
from rsi_arena.trading import PNL_SCALE_USD, START_EQUITY, cycles_of, pnl_objective
from tests.conftest import FakeLLM

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "harnesses" / "horizon-5m.json"


def windows(t0, n=6):
    """One contract, the mid rising five cents a window, touch not kept (the
    book crosses the venue's proxy spread)."""
    return [Window(ticker="A", at=t0 + timedelta(minutes=5 * k), mid_now=0.40 + 0.05 * k,
                   realised=0.45 + 0.05 * k, game={"clock": f"{5 * k}'"}, event="E" if k % 2 else "F")
            for k in range(1, n + 1)]


def forecast(**order):
    def script(m, s, t):
        return {"delta_cents": 5, "half_width_cents": 2, "confidence": 0.5, "driver": "d",
                "falsifier": "f", **order}
    return script


#: A harness that opens long a twentieth of the book on every window. An open
#: against an open closes the old one first, so every cycle after the first
#: realises a round trip, and the last closes at its horizon.
trader = forecast(action="open_long", size=0.05)
#: A harness that forecasts and declines to trade.
holder = forecast(action="hold", size=0.0)


class Bookless(KalshiHorizon):
    """The Kalshi task without a paper book."""
    trading = None


class Memo:
    """The scoreboard's contract, in a dict."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], Outcome] = {}
        self.hits = 0

    def get(self, fp, instance):
        out = self.rows.get((fp, instance.id))
        self.hits += out is not None
        return out

    def cost_of(self, fp, instance):
        return 0.001

    def absorb(self, fp, rollouts):
        for r in rollouts:
            self.rows[(fp, r.instance.id)] = r.outcome


def _adapter(tk, script, **kw):
    return TaskAdapter(tk, Harness.load(BASE), FakeLLM(script), **kw)


# -- the objective ------------------------------------------------------------------

def test_the_objective_is_a_percent_of_the_book_either_way_and_clipped():
    assert PNL_SCALE_USD == START_EQUITY / 100 == 10_000
    assert pnl_objective(0.0) == 0.5
    assert pnl_objective(PNL_SCALE_USD / 2) == 0.75 and pnl_objective(-PNL_SCALE_USD / 2) == 0.25
    assert pnl_objective(PNL_SCALE_USD) == 1.0 and pnl_objective(-PNL_SCALE_USD) == 0.0
    assert pnl_objective(50 * PNL_SCALE_USD) == 1.0 and pnl_objective(-50 * PNL_SCALE_USD) == 0.0


# -- the adapter ----------------------------------------------------------------------

def test_a_batch_that_trades_puts_pnl_on_the_frontier_and_the_book_in_the_feedback(t0, history):
    tk = KalshiHorizon(history=history, windows=windows(t0))
    adapter = _adapter(tk, trader)
    batch = adapter.evaluate(tk.instances(), adapter.base.to_components(), capture_traces=True)
    assert all(set(o) == {"skill", "cost", "pnl"} for o in batch.objective_scores)
    pnl = [o["pnl"] for o in batch.objective_scores]
    assert all(0.0 <= p <= 1.0 for p in pnl)
    # The first cycle only opens; every later one re-opens over the position
    # it is carrying, which closes it first, and the last is wound down at its
    # deadline. Nothing is realised by a horizon any more.
    assert pnl[0] == 0.5 and all(p != 0.5 for p in pnl[1:]), pnl
    # The scalar the gate and GEPA's best-candidate choice read is untouched.
    assert all(0.5 < v <= 1.0 for v in batch.scores)
    for traj in batch.trajectories:
        assert re.search(r"Book: quote [\d.]+/[\d.]+ \d+% (bid|ask|both) hit|unhit, "
                         r"(open_long|close|hold) \d+% -> [+-]\d+ USD", traj["feedback"]), traj["feedback"]
    data = adapter.make_reflective_dataset(adapter.base.to_components(), batch, ["plan", "context"])
    assert all("Book:" in rec["Feedback"] for rec in data["plan"] + data["context"])


def test_the_record_is_attached_to_the_outcome_and_a_holder_scores_exactly_half(t0, history):
    tk = KalshiHorizon(history=history, windows=windows(t0))
    harness = Harness.load(BASE)
    traded = _adapter(tk, trader)._traded(
        run_sync(evaluate(tk, harness, tk.instances(), FakeLLM(trader))), "fp")
    for r in traded:
        trade = r.outcome.details["trade"]
        assert trade["source"] == "harness" and trade["action"] == "open_long"
        assert r.outcome.feedback.endswith(f"USD ({trade['reason'] or 'no trade'})")
    assert traded[-1].outcome.details["trade"]["reason"] == "force_close", "nothing closes at a horizon"
    assert sum(r.outcome.details["trade"]["pnl_usd"] for r in traded) != 0.0

    held = _adapter(tk, holder)._traded(
        run_sync(evaluate(tk, harness, tk.instances(), FakeLLM(holder))), "fp")
    for r in held:
        assert r.outcome.objectives["pnl"] == 0.5
        assert r.outcome.details["trade"]["action"] == "hold"
        assert r.outcome.details["trade"]["pnl_usd"] == 0.0
        assert r.outcome.feedback.endswith("hold 0% -> +0 USD (no trade)")
        assert "Book: quote " in r.outcome.feedback, "a holder still posts a quote"


def test_a_task_without_a_book_is_untouched(t0, history):
    tk = Bookless(history=history, windows=windows(t0))
    adapter = _adapter(tk, trader)
    batch = adapter.evaluate(tk.instances()[:2], adapter.base.to_components(), capture_traces=True)
    assert all(set(o) == {"skill", "cost"} for o in batch.objective_scores)
    assert all("Book:" not in traj["feedback"] for traj in batch.trajectories)
    rollouts = adapter._traded(run_sync(evaluate(tk, Harness.load(BASE), tk.instances()[:2],
                                                 FakeLLM(trader))), "fp")
    assert all("trade" not in r.outcome.details for r in rollouts)


def test_a_book_that_fails_never_fails_an_evaluation_and_is_logged_once(t0, history, capsys):
    tk = KalshiHorizon(history=history, windows=windows(t0))
    tk._trading = object()          # a spec with none of the spec's functions
    adapter = _adapter(tk, trader)
    for _ in range(2):
        batch = adapter.evaluate(tk.instances()[:2], adapter.base.to_components(), capture_traces=True)
        assert all(0.5 < v <= 1.0 for v in batch.scores)
        assert all(set(o) == {"skill", "cost"} for o in batch.objective_scores)
    assert capsys.readouterr().err.count("paper book for the search batch not built") == 1


def test_a_remembered_rollout_trades_by_the_order_it_gave(t0, history):
    """A memoised rollout has no run, so the book used to trade the default
    rule on its behalf - a remembered candidate judged by a rule, a fresh one
    by its own hand. The record the adapter absorbed keeps the order."""
    tk = KalshiHorizon(history=history, windows=windows(t0))
    memo = Memo()
    adapter = _adapter(tk, trader, memo=memo)
    fresh = adapter.evaluate(tk.instances(), adapter.base.to_components(), capture_traces=False)
    assert memo.hits == 0 and len(memo.rows) == 6
    assert all(o.details["trade"]["source"] == "harness" for o in memo.rows.values())
    again = adapter.evaluate(tk.instances(), adapter.base.to_components(), capture_traces=False)
    assert memo.hits == 6 and adapter._fresh_windows == 6, "the second pass was remembered"
    assert [o["pnl"] for o in again.objective_scores] == [o["pnl"] for o in fresh.objective_scores]
    assert all(o["pnl"] != 0.5 for o in again.objective_scores[1:])
    # The same windows in a smaller batch make a different book, and the
    # remembered order is still the harness's, with one Book line, not two.
    half = adapter.evaluate(tk.instances()[:3], adapter.base.to_components(), capture_traces=False)
    assert len(half.objective_scores) == 3 and half.objective_scores[-1]["pnl"] != 0.5
    stored = memo.rows[(next(iter(memo.rows))[0], tk.instances()[0].id)]
    assert stored.feedback.count("Book:") == 1
    # A default-rule record is not replayed as an order.
    plain = Outcome(value=0.5, feedback="", details={"scored": True, "mid_now": 0.5, "predicted": 0.55,
                                                     "half_width": 0.01,
                                                     "trade": {"action": "open_long", "size": 0.1,
                                                               "source": "default"}})
    from rsi_arena.loop import Rollout
    cycle = cycles_of([Rollout(instance=tk.instances()[0], run=None, outcome=plain)], tk.trading)[0]
    assert cycle.output is None


# -- the rewriter's prompt and the frontier --------------------------------------------

def test_the_context_and_plan_rewriters_are_told_what_the_book_line_is():
    with_book = reflection_templates(KalshiHorizon(windows=[]), Harness.load(BASE))
    assert BOOK_NOTE in with_book["context"] and BOOK_NOTE in with_book["plan"]
    assert BOOK_NOTE not in with_book["tools"] and BOOK_NOTE not in with_book["model"]
    assert "promotion is by skill" in BOOK_NOTE and "round trip" in BOOK_NOTE
    without = reflection_templates(Bookless(windows=[]), Harness.load(BASE))
    assert all(BOOK_NOTE not in text for text in without.values())
    # Otherwise byte-identical.
    for name in with_book:
        assert with_book[name].replace(BOOK_NOTE + " ", "") == without[name]


def test_the_search_keeps_a_hybrid_frontier_and_gepa_knows_the_word():
    from gepa.core.state import FrontierType

    from rsi_arena.cli import FRONTIER_TYPE
    assert FRONTIER_TYPE == "hybrid" and FRONTIER_TYPE in get_args(FrontierType)
    source = (ROOT / "rsi_arena" / "cli.py").read_text()
    assert "frontier_type=FRONTIER_TYPE" in source, "the optimize call passes it"
