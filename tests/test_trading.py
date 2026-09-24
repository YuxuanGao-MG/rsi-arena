"""A forecast is worth what a fill says it is, after the spread and the fee.

Each test here is one way a paper book flatters or cheats a harness: filling
at the mid, forgetting a tax, letting a position exceed its cap, marking a
short as if it were a long, closing at a price the venue never printed, or
replaying the same rollouts to two different answers.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from rsi_arena.harness.spec import Plan
from rsi_arena.kalshi._fees import taker_fee
from rsi_arena.trading import (MAX_GROSS, MAX_POSITION, MIN_SIZE, START_EQUITY, Book, Cycle, Decision,
                               EquityCosts, KalshiCosts, Mark, PerpCosts, Quote, Trade, TradingSpec,
                               cycles_of, decide, default_decision, delta_from_details, equity_stats,
                               load_state, read_decision, replay_book, rows_to_cycles, save_state,
                               step_live, walk_book)
from rsi_arena.trading.costs import SEC_FEE_PER_DOLLAR, TAF_MAX, TAF_PER_SHARE
from rsi_arena.trading.policy import GRAMMAR_NOTE, TRADING_CONTRACT

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def at(minutes: float) -> datetime:
    return T0 + minutes * M


def kq(bid: float, ask: float, t: datetime = T0) -> Quote:
    return Quote(at=t, bid=bid, ask=ask, mid=(bid + ask) / 2)


# -- fills ---------------------------------------------------------------------

def test_kalshi_long_buys_yes_at_the_ask_and_pays_the_taker_fee():
    f = KalshiCosts().fill("long", 10_000.0, kq(0.48, 0.50))
    assert f.px == 0.50 and f.qty == 20_000 and f.notional_usd == 10_000.0
    assert abs(f.fees_usd - taker_fee(0.50, 20_000)) < 1e-9
    assert abs(f.fees_usd - 20_000 * 0.02) < 1e-9, "1.75c rounds up to 2c a contract at fifty cents"


def test_kalshi_short_is_a_no_priced_at_one_minus_the_bid():
    f = KalshiCosts().fill("short", 10_000.0, kq(0.30, 0.32))
    assert abs(f.px - 0.70) < 1e-12 and f.qty == math.floor(10_000 / 0.70)
    assert abs(f.fees_usd - taker_fee(0.30, f.qty)) < 1e-9, "the fee curve is symmetric in p(1-p)"


def test_kalshi_fee_caps_at_three_and_a_half_cents():
    f = KalshiCosts().fill("long", 1000.0, kq(0.49, 0.51))
    per = f.fees_usd / f.qty
    assert abs(per - taker_fee(0.5, 1)) < 1e-12 and per <= 0.035
    assert max(taker_fee(p / 100, 1) for p in range(1, 100)) <= 0.035, "the cap holds along the curve"
    assert taker_fee(0.99, 1) < taker_fee(0.5, 1), "and the tails are cheaper"


def test_kalshi_exit_crosses_again_and_settlement_is_free():
    c = KalshiCosts()
    close = c.fill("long", 0.0, kq(0.60, 0.62), closing=True, qty=100)
    assert close.px == 0.60 and close.fees_usd > 0
    settled = c.fill("long", 0.0, kq(1.0, 1.0), closing="settled", qty=100)
    assert settled.px == 1.0 and settled.fees_usd == 0.0
    no_close = c.fill("short", 0.0, kq(0.60, 0.62), closing=True, qty=100)
    assert abs(no_close.px - 0.38) < 1e-12, "selling NO is the same as buying YES at the ask"


def test_kalshi_round_trip_cost_is_spread_plus_two_fees_in_cents():
    q = kq(0.48, 0.50)
    expect = 100 * (0.02 + taker_fee(0.50) + taker_fee(0.48))
    assert abs(KalshiCosts().round_trip_cost(q, 1000.0) - expect) < 1e-9


def test_perp_long_pays_the_ask_and_short_receives_the_bid():
    q = Quote(at=T0, bid=99.9, ask=100.1, mid=100.0)
    c = PerpCosts()
    long = c.fill("long", 10_000.0, q)
    short = c.fill("short", 10_000.0, q)
    assert long.px == 100.1 and short.px == 99.9
    assert abs(long.fees_usd - 10_000 * 5e-4) < 1e-9, "five basis points a side"
    assert abs(c.round_trip_cost(q, 10_000.0) - (20.0 + 10.0)) < 1e-9, "spread 20 bps plus 10 of fees"


def test_walk_book_averages_across_levels_and_flags_partial():
    asks = ((100.0, 10.0), (101.0, 10.0), (102.0, 5.0))
    avg, filled, used = walk_book(asks, 1500.0)
    assert used == 2 and abs(filled - 1500.0) < 1e-9
    assert abs(avg - 1500.0 / (10 + 500 / 101)) < 1e-9
    avg, filled, used = walk_book(asks, 10_000.0)
    assert used == 3 and abs(filled - (1000 + 1010 + 510)) < 1e-9, "the ladder ran out"
    q = Quote(at=T0, bid=99.0, ask=100.0, mid=99.5, asks=asks)
    f = PerpCosts().fill("long", 10_000.0, q)
    assert f.partial and f.levels == 3 and f.notional_usd < 10_000.0
    touch = PerpCosts().fill("long", 10_000.0, Quote(at=T0, bid=99.0, ask=100.0, mid=99.5))
    assert not touch.partial and touch.px == 100.0, "no ladder: the touch, in full"


def test_equity_crosses_a_three_bps_half_spread_and_only_sells_pay_taxes():
    q = Quote(at=T0, bid=0, ask=0, mid=200.0)
    c = EquityCosts()
    buy = c.fill("long", 100_000.0, q)
    assert abs(buy.px - 200.0 * 1.0003) < 1e-9 and buy.fees_usd == 0.0
    assert buy.qty == math.floor(100_000 / buy.px)
    sell = c.fill("short", 100_000.0, q)
    assert abs(sell.px - 200.0 * 0.9997) < 1e-9
    assert abs(sell.fees_usd - (sell.notional_usd * SEC_FEE_PER_DOLLAR + sell.qty * TAF_PER_SHARE)) < 1e-9
    big = c.fill("long", 5_000_000.0, Quote(at=T0, bid=0, ask=0, mid=1.0), closing=True, qty=5_000_000)
    assert abs(big.fees_usd - (big.notional_usd * SEC_FEE_PER_DOLLAR + TAF_MAX)) < 1e-9, "TAF caps at 8.30"
    rt = c.round_trip_cost(q, 100_000.0)
    assert abs(rt - (6.0 + 0.278 + min(500 * TAF_PER_SHARE, TAF_MAX) / 100_000 * 1e4)) < 1e-9


# -- the book -------------------------------------------------------------------

def book(costs=None) -> Book:
    return Book("b", "t", "fp", costs or KalshiCosts())


def test_a_long_marked_at_the_mid_has_paid_the_half_spread_and_the_fee():
    b = book()
    q = kq(0.48, 0.50)
    f = b.open("X", "long", 0.10, q, T0, at(10))
    assert f is not None
    m = b.mark(T0)
    expect = START_EQUITY - f.fees_usd - f.qty * (0.50 - 0.49)
    assert abs(m.equity_usd - expect) < 1e-6
    assert abs(m.gross_exposure_usd - f.qty * 0.49) < 1e-6


def test_a_kalshi_short_is_a_no_worth_one_minus_the_mid():
    b = book()
    q = kq(0.48, 0.50)
    f = b.open("X", "short", 0.10, q, T0, at(10))
    m = b.mark(T0)
    assert abs(m.equity_usd - (START_EQUITY - f.fees_usd - f.qty * (0.49 - 0.48))) < 1e-6
    assert abs(m.gross_exposure_usd - f.qty * (1 - 0.49)) < 1e-6


def test_a_perp_short_marked_at_the_mid_is_symmetric_with_the_long():
    q = Quote(at=T0, bid=99.9, ask=100.1, mid=100.0)
    lb, sb = book(PerpCosts()), book(PerpCosts())
    fl = lb.open("BTC", "long", 0.10, q, T0, at(10))
    fs = sb.open("BTC", "short", 0.10, q, T0, at(10))
    assert abs(lb.mark(T0).equity_usd - (START_EQUITY - fl.fees_usd - fl.qty * (100.1 - 100.0))) < 1e-6
    assert abs(sb.mark(T0).equity_usd - (START_EQUITY - fs.fees_usd - fs.qty * (100.0 - 99.9))) < 1e-6


def test_a_round_trip_pnl_is_the_sum_of_its_cash_flows():
    b = book(PerpCosts())
    q0 = Quote(at=T0, bid=99.9, ask=100.1, mid=100.0)
    q1 = Quote(at=at(1), bid=101.9, ask=102.1, mid=102.0)
    f = b.open("BTC", "long", 0.10, q0, T0, at(10))
    t = b.close("BTC", q1, at(1), "agent")
    assert abs(t.pnl_usd - (f.qty * (101.9 - 100.1) - t.fees_usd)) < 1e-6
    assert abs(b.cash - (START_EQUITY + t.pnl_usd)) < 1e-6 and not b.positions
    short = book(PerpCosts())
    f = short.open("BTC", "short", 0.10, q0, T0, at(10))
    t = short.close("BTC", q1, at(1), "agent")
    assert abs(t.pnl_usd - (f.qty * (99.9 - 102.1) - t.fees_usd)) < 1e-6


def test_the_position_cap_is_ten_percent_of_equity():
    b = book()
    f = b.open("X", "long", 0.50, kq(0.48, 0.50), T0, at(10))
    assert f.notional_usd <= MAX_POSITION * START_EQUITY + 1e-6
    assert f.notional_usd > 0.099 * START_EQUITY


def test_the_gross_cap_is_half_of_equity_and_the_rest_is_refused():
    b = book(PerpCosts())
    q = Quote(at=T0, bid=99.9, ask=100.1, mid=100.0)
    for i in range(5):
        assert b.open(f"C{i}", "long", 0.10, q, T0, at(10)) is not None
    assert b.gross() <= MAX_GROSS * START_EQUITY + 1e-6
    assert b.open("C5", "long", 0.10, q, T0, at(10)) is None
    assert len(b.refusals) == 1 and b.refusals[0]["instrument"] == "C5"


def test_dust_is_refused_and_recorded():
    b = book()
    assert b.open("X", "long", MIN_SIZE / 2, kq(0.48, 0.50), T0, at(10)) is None
    assert b.refusals and b.refusals[0]["reason"] == "dust"
    assert not b.positions and b.cash == START_EQUITY


def test_one_position_per_instrument_an_open_against_an_open_closes_first():
    b = book()
    b.open("X", "long", 0.05, kq(0.48, 0.50), T0, at(10))
    b.open("X", "short", 0.05, kq(0.50, 0.52), at(1), at(10))
    assert len(b.positions) == 1 and b.positions["X"].side == "short"
    assert len(b.trades) == 1 and b.trades[0].reason == "agent" and b.trades[0].side == "long"


def test_the_deadline_force_closes_at_the_last_quote_seen():
    b = book()
    b.open("X", "long", 0.05, kq(0.48, 0.50), T0, at(5))
    b.last_quote["X"] = kq(0.55, 0.57, at(4))
    assert b.expire(at(4)) == [], "not yet"
    closed = b.expire(at(5))
    assert len(closed) == 1 and closed[0].reason == "force_close" and closed[0].exit_px == 0.55
    assert not b.positions


def test_a_book_round_trips_through_its_dict():
    b = book(PerpCosts())
    q = Quote(at=T0, bid=99.9, ask=100.1, mid=100.0, asks=((100.1, 5.0),))
    b.open("BTC", "long", 0.05, q, T0, at(10))
    b.open("ETH", "short", 0.05, q, T0, at(10))
    b.close("ETH", q, at(1), "agent")
    b.mark(at(1))
    b.handovers.append({"at": T0.isoformat(), "from": "a", "to": "b"})
    b.last_at, b.processed, b.harness_name = at(1), 2, "b"
    again = Book.from_dict(json.loads(json.dumps(b.to_dict())))
    assert again.to_dict() == b.to_dict()
    assert again.costs.name == "perp" and again.positions["BTC"].deadline == at(10)
    assert again.last_quote["BTC"].asks == ((100.1, 5.0),)
    assert abs(again.equity() - b.equity()) < 1e-9


# -- stats ------------------------------------------------------------------------

def marks_of(series):
    return [Mark(at=at(i), equity_usd=e, cash_usd=e, gross_exposure_usd=0.0, open_positions=0,
                 drawdown=0.0) for i, e in enumerate(series)]


def trade(pnl, size=100_000.0, fees=10.0):
    return Trade(instrument="X", side="long", opened_at=T0, closed_at=at(1), entry_px=1.0, exit_px=1.0,
                 qty=1.0, size_usd=size, fees_usd=fees, pnl_usd=pnl, reason="agent")


def test_stats_on_a_known_series():
    series = [1.00e6, 1.01e6, 0.99e6, 1.02e6]
    s = equity_stats(marks_of(series), [], 100, start=1.0e6)
    assert abs(s["total_return"] - 0.02) < 1e-12
    assert abs(s["max_drawdown"] - (1.01 - 0.99) / 1.01) < 1e-12, "1.98% off the 1.01 peak"
    full = [1.0e6] + series
    r = [b / a - 1 for a, b in zip(full, full[1:])]
    assert abs(s["sharpe"] - statistics.mean(r) / statistics.stdev(r) * math.sqrt(100)) < 1e-9
    assert s["cycles"] == 4 and s["end_equity"] == 1.02e6 and s["trades"] == 0


def test_stats_from_three_hand_built_trades():
    trades = [trade(+300.0), trade(-100.0), trade(+50.0, size=50_000.0)]
    s = equity_stats([], trades, 100)
    assert abs(s["hit_rate"] - 2 / 3) < 1e-12
    assert s["avg_win"] == 175.0 and s["avg_loss"] == -100.0
    assert abs(s["profit_factor"] - 3.5) < 1e-12
    assert abs(s["turnover"] - 2 * 250_000.0 / START_EQUITY) < 1e-12
    assert s["fees_usd"] == 30.0 and s["trades"] == 3


def test_a_flat_book_has_no_sharpe():
    s = equity_stats(marks_of([1e6, 1e6, 1e6]), [], 100)
    assert s["sharpe"] == 0.0 and s["daily_sharpe"] == 0.0 and s["max_drawdown"] == 0.0
    assert equity_stats([], [], 100)["sharpe"] == 0.0


# -- policy -----------------------------------------------------------------------

def test_the_default_rule_opens_long_on_a_clear_edge_and_caps_the_size():
    d = default_decision(delta=6.0, half_width=1.0, round_trip_cost=2.0, tick=1.0, open_side=None)
    assert d.action == "open_long" and d.size == MAX_POSITION and d.source == "default"
    assert d.edge == 3.0


def test_the_default_rule_holds_when_the_edge_does_not_clear_costs():
    d = default_decision(delta=2.0, half_width=1.0, round_trip_cost=2.0, tick=1.0, open_side=None)
    assert d.action == "hold" and d.size == 0.0
    d = default_decision(delta=-2.0, half_width=1.0, round_trip_cost=2.0, tick=1.0, open_side=None)
    assert d.action == "hold"


def test_the_default_rule_closes_an_open_long_on_a_reversal_and_holds_through_noise():
    assert default_decision(delta=-5.0, half_width=1.0, round_trip_cost=2.0, tick=1.0,
                            open_side="long").action == "close"
    assert default_decision(delta=-0.5, half_width=1.0, round_trip_cost=2.0, tick=1.0,
                            open_side="long").action == "hold", "inside half the round trip"
    assert default_decision(delta=+9.0, half_width=1.0, round_trip_cost=2.0, tick=1.0,
                            open_side="long").action == "hold", "already on the right side"


def test_the_default_size_is_quarter_kelly_over_the_wider_of_width_and_tick():
    d = default_decision(delta=4.0, half_width=1.0, round_trip_cost=2.0, tick=1.0, open_side=None)
    assert abs(d.size - 0.25 * 1.0 / 1.0) < 1e-12 or d.size == MAX_POSITION
    d = default_decision(delta=2.2, half_width=0.0, round_trip_cost=2.0, tick=1.0, open_side=None)
    assert d.action == "open_long" and abs(d.size - 0.05) < 1e-12
    d = default_decision(delta=2.01, half_width=0.0, round_trip_cost=2.0, tick=1.0, open_side=None)
    assert d.action == "hold", "a size under MIN_SIZE is not worth the fee"


def test_a_harness_action_and_size_override_the_default():
    d = decide({"action": "open_short", "size": 0.03, "delta_cents": 9}, 9.0, 1.0, 2.0, 1.0, None)
    assert d == Decision("open_short", 0.03, "harness")
    d = decide({"action": "close"}, 9.0, 1.0, 2.0, 1.0, "long")
    assert d.action == "close" and d.source == "harness"


def test_a_malformed_action_falls_through_and_a_size_is_clamped():
    assert read_decision({"action": "buy", "size": 0.05}) is None
    assert read_decision({"action": "open_long", "size": "big"}) is None
    assert read_decision({"action": "open_long"}) is None
    assert read_decision({"action": "open_long", "size": True}) is None
    assert read_decision("open_long") is None
    assert read_decision({"action": "open_long", "size": 0.5}).size == MAX_POSITION
    assert read_decision({"action": "open_long", "size": -1}).size == 0.0
    d = decide({"action": "buy"}, 6.0, 1.0, 2.0, 1.0, None)
    assert d.action == "open_long" and d.source == "default"


def test_the_contract_and_the_grammar_agree():
    assert "open_long" in TRADING_CONTRACT and "0.10" in TRADING_CONTRACT and "NO" in TRADING_CONTRACT
    assert " ".join(GRAMMAR_NOTE.split()) in " ".join(Plan.GRAMMAR.split())


# -- replay -------------------------------------------------------------------------

class _Inst:
    def __init__(self, ticker, t, mid, realised, deadline_min=15):
        self.ticker, self.at, self.mid_now, self.realised = ticker, t, mid, realised
        self.deadline = t + deadline_min * M

    @property
    def id(self):
        return f"{self.ticker}@{self.at.isoformat()}"

    @property
    def group(self):
        return self.ticker


class _Out:
    def __init__(self, predicted, half=0.005, scored=True):
        self.details = {"scored": scored, "predicted": predicted, "half_width": half, "unit": "cents"}


class _Roll:
    def __init__(self, inst, outcome, output=None):
        self.instance, self.outcome, self.run = inst, outcome, None
        if output is not None:
            self.run = type("R", (), {"output": output, "run_id": "r1"})()


def kalshi_spec(horizon_min=5) -> TradingSpec:
    def quote_of(i):
        return (Quote.from_mid(i.at, i.mid_now, 0.01), Quote.from_mid(i.at + horizon_min * M, i.realised, 0.01))

    def delta_of(o):
        d = dict(o.details)
        return delta_from_details({**d, "mid_now": d.get("mid_now", 0.5)}, "cents")

    return TradingSpec(costs=KalshiCosts(), unit="cents", tick=1.0, cycles_per_year=105_120,
                       quote_of=quote_of, deadline_of=lambda i: i.deadline,
                       instrument_of=lambda i: i.ticker, delta_of=delta_of, topic="kalshi")


def roll(ticker, minute, mid, realised, predicted, output=None, deadline_min=15):
    inst = _Inst(ticker, at(minute), mid, realised, deadline_min)
    out = _Out(predicted)
    out.details["mid_now"] = mid
    return _Roll(inst, out, output)


def test_replay_is_deterministic_over_shuffled_rollouts():
    rollouts = []
    for k in range(12):
        rollouts.append(roll("A", 5 * k, 0.50, 0.50 + 0.01 * (k % 3 - 1), 0.60 if k % 2 else 0.40))
        rollouts.append(roll("B", 5 * k, 0.30, 0.31, 0.40))
    spec = kalshi_spec()
    a, ra = replay_book(cycles_of(list(rollouts), spec), spec, book_id="x", harness_fp="fp")
    shuffled = list(rollouts)
    random.Random(7).shuffle(shuffled)
    b, rb = replay_book(cycles_of(shuffled, spec), spec, book_id="x", harness_fp="fp")
    assert a.to_dict() == b.to_dict() and ra == rb
    assert a.trades, "something traded"
    assert abs(sum(r["pnl_usd"] for r in ra.values()) - sum(t.pnl_usd for t in a.trades)) < 1e-9


def test_a_position_carries_past_every_horizon_and_out_at_the_deadline():
    spec = kalshi_spec(horizon_min=5)
    # A is asked twice, five minutes apart, and says the same thing both times.
    # Neither horizon closes anything; the deadline does, at minute 15.
    rollouts = [roll("A", 0, 0.50, 0.52, 0.60), roll("A", 5, 0.52, 0.53, 0.62)]
    b, rec = replay_book(cycles_of(rollouts, spec), spec, book_id="x", harness_fp="fp")
    first = rec["A@" + at(0).isoformat()]
    assert first["action"] == "open_long" and first["pnl_usd"] == 0.0 and first["reason"] is None
    assert len(b.trades) == 1 and b.trades[0].reason == "force_close"
    assert b.trades[0].closed_at == at(15) and b.trades[0].opened_at == at(0)
    assert not any(m.event == "horizon" for m in b.marks), "nothing happens at a horizon"
    # The wind-down is credited to the last cycle that carried the position.
    last = rec["A@" + at(5).isoformat()]
    assert last["pnl_usd"] == b.trades[0].pnl_usd and last["reason"] == "force_close"
    # Asked again only twenty minutes later: the deadline still bounds it, and
    # the second cycle opens a position of its own that is wound down after.
    rollouts = [roll("A", 0, 0.50, 0.52, 0.60), roll("A", 20, 0.52, 0.53, 0.62)]
    b, rec = replay_book(cycles_of(rollouts, spec), spec, book_id="x", harness_fp="fp")
    assert [t.reason for t in b.trades] == ["force_close", "force_close"]
    # The sweep runs at a cycle, so the first is found out of time at minute 20,
    # and the second is wound down at its own deadline once the cycles stop.
    assert b.trades[0].closed_at == at(20) and b.trades[1].closed_at == at(35)
    # Both are realised during the cycle at minute 20: the sweep and then the
    # wind-down of what that cycle opened.
    second = rec["A@" + at(20).isoformat()]
    assert second["pnl_usd"] == pytest.approx(sum(t.pnl_usd for t in b.trades))
    assert rec["A@" + at(0).isoformat()]["pnl_usd"] == 0.0, "it was still carrying"


def test_the_deadline_settles_a_resolved_market_at_zero_or_one():
    spec = kalshi_spec(horizon_min=5)
    rollouts = [roll("A", 0, 0.50, 0.52, 0.60, deadline_min=3)]
    for settles, entry in ((1.0, 1.0), (0.0, 0.0)):
        settled = replace(spec, settlement_of=lambda i, s=settles: s)
        b, rec = replay_book(cycles_of(rollouts, settled), settled, book_id="x", harness_fp="fp")
        assert [t.reason for t in b.trades] == ["settled"] and b.trades[0].exit_px == entry
        assert b.trades[0].closed_at == at(3), "out at the deadline, not the horizon"
        # Settlement is free; the entry's fee is the whole of the round trip's.
        assert abs(b.trades[0].fees_usd - taker_fee(b.trades[0].entry_px, b.trades[0].qty)) < 1e-9
    # No settlement known: the last mid, and a fee on the way out.
    b, _ = replay_book(cycles_of(rollouts, spec), spec, book_id="x", harness_fp="fp")
    assert b.trades[0].reason == "force_close" and b.trades[0].fees_usd > taker_fee(0.51, b.trades[0].qty)


def test_a_harness_output_in_the_run_drives_the_replay():
    spec = kalshi_spec()
    rollouts = [roll("A", 0, 0.50, 0.52, 0.60, output={"action": "open_short", "size": 0.02})]
    b, rec = replay_book(cycles_of(rollouts, spec), spec, book_id="x", harness_fp="fp")
    r = rec["A@" + at(0).isoformat()]
    assert r["action"] == "open_short" and r["source"] == "harness" and r["size"] == 0.02
    assert b.trades[0].side == "short" and b.trades[0].run_id == "r1"


def test_delta_is_read_off_the_outcome_not_the_run():
    assert delta_from_details({"scored": True, "mid_now": 0.50, "predicted": 0.56, "half_width": 0.02},
                              "cents") == pytest.approx((6.0, 2.0))
    assert delta_from_details({"scored": True, "mid_now": 100.0, "predicted": 100.5, "half_width": 3.0},
                              "bps") == pytest.approx((50.0, 3.0))
    assert delta_from_details({"scored": False}, "cents") is None


# -- live -------------------------------------------------------------------------

def live_spec(costs, unit):
    return type("S", (), {"costs": costs, "unit": unit, "horizon": 5 * M})()


def test_rows_to_cycles_reads_the_three_row_shapes_and_skips_the_unresolved():
    kalshi = [{"at": at(0).isoformat(), "ticker": "K1", "mid_now": 0.50, "realised": 0.52, "harness": "h",
               "yes_bid": 0.49, "yes_ask": 0.51, "output": {"delta_cents": 4, "half_width_cents": 1}},
              {"at": at(5).isoformat(), "ticker": "K1", "mid_now": 0.52, "realised": None, "harness": "h",
               "output": {}}]
    cs = rows_to_cycles(kalshi, live_spec(KalshiCosts(), "cents"))
    assert len(cs) == 1 and cs[0].entry.bid == 0.49 and not cs[0].entry.proxy
    assert cs[0].delta == 4.0 and cs[0].half_width == 1.0
    assert cs[0].exit.proxy and cs[0].exit.mid == 0.52 and cs[0].horizon_at == at(5)

    crypto = [{"at": at(0).isoformat(), "symbol": "BTCUSDT", "mid_now": 100.0, "realised": 100.2, "harness": "h",
               "context": {"book": {"bid": 99.9, "ask": 100.1}},
               "realised_quote": {"bid": 100.1, "ask": 100.3, "mid": 100.2},
               "output": {"delta_bps": 30, "half_width_bps": 5}}]
    ladders = {("BTCUSDT", at(0).isoformat()): {"bids": [[99.9, 1.0]], "asks": [[100.1, 2.0], [100.2, 3.0]]}}
    cs = rows_to_cycles(crypto, live_spec(PerpCosts(), "bps"), ladders)
    assert cs[0].entry.asks == ((100.1, 2.0), (100.2, 3.0)) and cs[0].entry.ask == 100.1
    assert cs[0].exit.bid == 100.1 and not cs[0].exit.proxy and cs[0].delta == 30.0

    equity = [{"at": at(0).isoformat(), "ticker": "AAPL", "mid_now": 200.0, "realised": 201.0, "harness": "h",
               "output": {"delta_bps": 40, "half_width_bps": 10}}]
    cs = rows_to_cycles(equity, live_spec(EquityCosts(), "bps"))
    assert cs[0].entry.proxy and abs(cs[0].entry.ask - 200.0 * 1.0003) < 1e-9
    assert cs[0].deadline == at(10), "one horizon past the horizon"


def crypto_cycle(minute, mid, delta=60.0):
    q = Quote(at=at(minute), bid=mid - 0.01, ask=mid + 0.01, mid=mid)
    return Cycle(at=at(minute), instrument="BTCUSDT", instance_id=f"c{minute}", run_id="", output=None,
                 delta=delta, half_width=5.0, entry=q, exit=q, horizon_at=at(minute + 1),
                 deadline=at(minute + 30))


def test_step_live_skips_what_the_book_has_seen_and_carries_to_the_deadline():
    b = Book("live", "crypto", "fp", PerpCosts())
    trades, marks = step_live(b, [crypto_cycle(0, 100.0), crypto_cycle(1, 100.5)], harness_fp="fp",
                              harness_name="h1")
    assert b.last_at == at(1) and b.processed == 2 and len(marks) == 2
    assert "BTCUSDT" in b.positions and not trades, "no lookahead: it carries"
    again, _ = step_live(b, [crypto_cycle(0, 100.0), crypto_cycle(1, 100.5)], harness_fp="fp",
                         harness_name="h1")
    assert b.processed == 2 and not again, "nothing new"
    step_live(b, [crypto_cycle(31, 101.0, delta=0.0)], harness_fp="fp", harness_name="h1")
    assert not b.positions and b.trades[0].reason == "force_close", "the deadline swept it"


def test_a_handover_keeps_the_positions_and_marks_the_event():
    b = Book("live", "crypto", "fp", PerpCosts())
    step_live(b, [crypto_cycle(0, 100.0)], harness_fp="fp1", harness_name="h1")
    assert b.harness_name == "h1" and not b.handovers
    pos = b.positions["BTCUSDT"]
    step_live(b, [crypto_cycle(1, 100.2)], harness_fp="fp2", harness_name="h2")
    assert b.handovers == [{"at": at(1).isoformat(), "from": "h1", "to": "h2"}]
    assert b.harness_name == "h2" and b.harness_fp == "fp2"
    assert b.positions["BTCUSDT"] == pos, "the new harness inherits the exposure"
    assert any(m.event == "handover" for m in b.marks)


def test_state_round_trips_through_the_file(tmp_path):
    path = tmp_path / "books" / "crypto.h1.json"
    assert load_state(path) is None
    b = Book("live", "crypto", "fp", PerpCosts())
    step_live(b, [crypto_cycle(0, 100.0)], harness_fp="fp", harness_name="h1")
    save_state(path, b)
    again = load_state(path)
    assert again is not None and again.to_dict() == b.to_dict()
    assert again.last_at == at(0) and again.harness_name == "h1" and again.processed == 1
    step_live(again, [crypto_cycle(1, 100.1)], harness_fp="fp", harness_name="h1")
    assert again.processed == 2


def test_the_package_stays_out_of_the_loop_and_the_topics():
    import subprocess
    import sys
    code = ("import sys, rsi_arena.trading; "
            "bad = [m for m in sys.modules if m.startswith(('rsi_arena.topics', 'rsi_arena.loop', "
            "'rsi_arena.alpaca', 'rsi_arena.crypto'))]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout
