"""The paper book reaches the topics, the loop, the collectors and the seeds.

``test_trading.py`` proves the engine; this proves the wiring: each topic
says how its windows become cycles, a generation writes its books and tells
the rewriter what each forecast was worth, the collectors keep the touch the
book needs, the seeds carry the two decision fields, and the live trader is
the same book resumed from a file.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsi_arena.harness import Harness
from rsi_arena.harness.decisions import answers_to_output, validate_questions
from rsi_arena.loop import Outcome, Rollout
from rsi_arena.loop.books import book_id_for, replay_books
from rsi_arena.topics._common.trading import book_line, with_trade
from rsi_arena.topics.crypto_horizon import CryptoHorizon
from rsi_arena.topics.crypto_horizon.windows import CryptoWindow
from rsi_arena.topics.kalshi_horizon import KalshiHorizon, Window
from rsi_arena.topics.kalshi_horizon import trading as ktrading
from rsi_arena.topics.news_equity import NewsEquity
from rsi_arena.topics.news_equity.windows import BenchmarkItem, NewsWindow, build_windows
from rsi_arena.trading import TRADING_CONTRACT, read_decision
from tests.conftest import FakeBars, FakeHistory

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 8, 23, 15, 0, tzinfo=UTC)
M = timedelta(minutes=1)


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the topics' specs ------------------------------------------------------------------

def test_the_kalshi_window_keeps_the_touch_and_an_old_file_still_loads():
    w = Window(ticker="K", at=T0, mid_now=0.5, realised=0.52, yes_bid=0.49, yes_ask=0.51,
               yes_bid_h=0.51, yes_ask_h=0.53)
    assert Window.from_dict(w.to_dict()) == w
    old = Window.from_dict({"ticker": "K", "at": T0.isoformat(), "mid_now": 0.5, "realised": 0.52})
    assert old.yes_bid is None and old.yes_ask_h is None


def test_the_kalshi_spec_crosses_the_touch_when_kept_and_the_proxy_otherwise():
    spec = KalshiHorizon(windows=[]).trading
    assert spec.costs.name == "kalshi" and spec.unit == "cents" and spec.tick == 1.0
    assert spec.cycles_per_year == 105_120 and spec.horizon == 5 * M
    kept = Window(ticker="K", at=T0, mid_now=0.5, realised=0.52, yes_bid=0.49, yes_ask=0.51,
                  yes_bid_h=0.515, yes_ask_h=0.525)
    entry, exit_ = spec.quote_of(kept)
    assert (entry.bid, entry.ask, entry.proxy) == (0.49, 0.51, False)
    assert (exit_.bid, exit_.ask, exit_.proxy, exit_.at) == (0.515, 0.525, False, T0 + 5 * M)
    bare = Window(ticker="K", at=T0, mid_now=0.5, realised=0.52)
    entry, exit_ = spec.quote_of(bare)
    assert entry.proxy and entry.bid == pytest.approx(0.49) and entry.ask == pytest.approx(0.51)
    assert exit_.proxy and exit_.mid == 0.52
    assert spec.instrument_of(bare) == "K"
    out = Outcome(value=0.5, feedback="", details={"scored": True, "mid_now": 0.5, "predicted": 0.53,
                                                   "half_width": 0.01})
    assert spec.delta_of(out) == pytest.approx((3.0, 1.0))


def test_the_kalshi_deadline_is_the_whistle_or_the_last_window_on_the_contract():
    kickoff = T0 - 20 * M
    scheduled = Window(ticker="K", at=T0, mid_now=0.5, realised=0.5,
                       game={"status": "scheduled", "kickoff": kickoff.isoformat()})
    task = KalshiHorizon(windows=[scheduled])
    assert task.trading.deadline_of(scheduled) == kickoff + 115 * M
    # A timeline the task already built counts; one it would have to fetch does not.
    known = Window(ticker="K", at=T0, mid_now=0.5, realised=0.5, game={"game_id": "g1", "status": "in_progress"})
    task._timelines["g1"] = SimpleNamespace(kickoff=kickoff)
    assert task.trading.deadline_of(known) == kickoff + 115 * M
    # No kickoff anywhere: five minutes past the contract's last window, scanned once.
    ws = [Window(ticker="A", at=T0 + i * 5 * M, mid_now=0.5, realised=0.5, game={"game_id": "x"})
          for i in range(4)] + [Window(ticker="B", at=T0, mid_now=0.5, realised=0.5)]
    task = KalshiHorizon(windows=ws)
    calls = []
    original = task.instances
    task.instances = lambda: calls.append(1) or original()
    assert task.trading.deadline_of(ws[0]) == T0 + 15 * M + 5 * M
    assert task.trading.deadline_of(ws[4]) == T0 + 5 * M
    assert len(calls) == 1, "the scan happens once"
    # Off a task: five minutes past the window itself.
    assert ktrading.trading_spec().deadline_of(ws[0]) == T0 + 5 * M


def test_the_crypto_spec_is_a_perp_with_a_two_bps_proxy_and_an_hour():
    spec = CryptoHorizon(windows=[]).trading
    assert spec.costs.name == "perp" and spec.unit == "bps" and spec.tick == 2.0
    assert spec.cycles_per_year == 525_600 and spec.horizon == M
    w = CryptoWindow(symbol="BTCUSDT", at=T0, mid_now=100_000.0, realised=100_010.0)
    entry, exit_ = spec.quote_of(w)
    assert entry.proxy and (entry.ask - entry.bid) / entry.mid * 1e4 == pytest.approx(2.0)
    assert exit_.mid == 100_010.0 and exit_.at == T0 + M
    assert spec.deadline_of(w) == T0 + 60 * M and spec.instrument_of(w) == "BTCUSDT"
    assert CryptoHorizon(windows=[], horizon=3).trading.horizon == 3 * M


def test_the_news_spec_prefers_the_vwap_and_closes_at_the_bell():
    spec = NewsEquity(windows=[]).trading
    assert spec.costs.name == "equity" and spec.unit == "bps" and spec.tick == 5.0
    assert spec.cycles_per_year == 19_656
    at = datetime(2026, 6, 1, 14, 20, tzinfo=UTC)               # 10:20 New York, a Monday
    w = NewsWindow(symbol="ACME", at=at, mid_now=100.0, realised=101.0, news_id="n", headline="h",
                   vwap_now=100.2, vwap_h=100.9)
    entry, exit_ = spec.quote_of(w)
    assert entry.mid == 100.2 and exit_.mid == 100.9 and entry.proxy
    bare = NewsWindow(symbol="ACME", at=at, mid_now=100.0, realised=101.0, news_id="n", headline="h")
    assert spec.quote_of(bare)[0].mid == 100.0 and spec.quote_of(bare)[1].mid == 101.0
    assert spec.deadline_of(w) == datetime(2026, 6, 1, 20, 0, tzinfo=UTC), "16:00 New York in June"
    assert spec.instrument_of(w) == "ACME"
    assert NewsWindow.from_dict(w.to_dict()) == w and NewsWindow.from_dict(bare.to_dict()).vwap_now is None


def test_build_windows_writes_the_vwap_through_a_source_that_hands_bars_over(tmp_path):
    t0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    tape = FakeBars(t0, {"ACME": {m: 100.0 + m for m in range(-10, 40)}})
    item = BenchmarkItem(symbol="ACME", news_id="a", at=t0 + 20 * M, headline="h")
    [w] = build_windows([item], bars=tape, windows_dir=tmp_path)
    assert w.vwap_now == w.mid_now and w.vwap_h == w.realised, "the fake's vwap is its close"
    closes_only = SimpleNamespace(price_at=tape.price_at, realised_price=tape.realised_price)
    [w2] = build_windows([item], bars=closes_only)
    assert w2.vwap_now is None and w2.mid_now == w.mid_now


def test_every_background_carries_the_contract_and_the_feedback_gets_the_book_line():
    for task in (KalshiHorizon(windows=[]), CryptoHorizon(windows=[]), NewsEquity(windows=[])):
        assert task.background.endswith(TRADING_CONTRACT)
    record = {"action": "open_long", "size": 0.05, "source": "default", "pnl_usd": 123.4,
              "fees_usd": 10.0, "reason": "horizon", "refused": False}
    assert book_line(record) == "Book: open_long 5% -> +123 USD (horizon)"
    out = with_trade(Outcome(value=0.5, feedback="Missed by 0.01.", details={"scored": True}), record)
    assert out.details["trade"] == record and out.details["scored"] is True
    assert out.feedback == "Missed by 0.01. Book: open_long 5% -> +123 USD (horizon)"
    assert book_line({"action": "hold", "size": 0, "pnl_usd": 0, "refused": True}) == "Book: hold 0% -> +0 USD (refused)"


# -- a generation's books ----------------------------------------------------------------

def _run(output, run_id="r1"):
    return SimpleNamespace(output=output, run_id=run_id, cost_usd=0.001, ok=True, harness="h")


def _rollouts(task, n=3, delta=0.08):
    """Windows on one contract, every forecast a big up-move, the mid rising a cent each."""
    out = []
    for i in range(n):
        mid = 0.50 + 0.01 * i
        w = Window(ticker="K1", at=T0 + i * 5 * M, mid_now=mid, realised=mid + 0.01,
                   yes_bid=mid - 0.01, yes_ask=mid + 0.01, yes_bid_h=mid, yes_ask_h=mid + 0.02,
                   game={"game_id": "g"}, event="E")
        outcome = Outcome(value=0.6, feedback=f"window {i}.", objectives={"skill": 0.6, "cost": 1.0},
                          details={"scored": True, "mid_now": mid, "realised": mid + 0.01,
                                   "predicted": mid + delta, "half_width": 0.005})
        out.append(Rollout(instance=w, run=_run({"delta_cents": delta * 100, "half_width_cents": 0.5}),
                           outcome=outcome))
    return out


def test_replay_books_writes_the_file_the_publisher_reads_and_attaches_the_trade(tmp_path):
    task = KalshiHorizon(windows=[])
    rollouts = _rollouts(task)
    task.use_instances([r.instance for r in rollouts])
    run_dir = tmp_path / "runs" / "kalshi-jev" / "gen7"
    stats, path = replay_books(task, rollouts, run_dir=run_dir, side="candidate", split="holdout",
                               harness_fp="fp-1", harness_name="h+gen7")
    assert path == run_dir / "books" / "candidate.holdout.json"
    book = json.loads(path.read_text())
    assert book["book_id"] == "gen7@kalshi-jev:candidate:holdout" == book_id_for(run_dir, task.name, "candidate", "holdout")
    assert {k: book[k] for k in ("harness_fp", "harness_name", "kind", "run_id", "side", "split")} == \
        {"harness_fp": "fp-1", "harness_name": "h+gen7", "kind": "replay", "run_id": "gen7@kalshi-jev",
         "side": "candidate", "split": "holdout"}
    assert book["started_at"] == T0.isoformat() and book["stats"] == stats
    assert stats["trades"] >= 1 and stats["cycles"] >= 3 and len(book["marks"]) == stats["cycles"]
    assert all(set(t) >= {"instrument", "opened_at", "closed_at", "entry_px", "exit_px", "pnl_usd", "reason"}
               for t in book["trades"])
    # Every rollout's outcome carries its cycle, and says so in the feedback.
    for r in rollouts:
        trade = r.outcome.details["trade"]
        assert trade["action"] in ("open_long", "hold", "close", "open_short")
        assert r.outcome.feedback.endswith(book_line(trade))
    assert sum(r.outcome.details["trade"]["pnl_usd"] for r in rollouts) == pytest.approx(
        sum(t["pnl_usd"] for t in book["trades"]))
    assert book_id_for(None, task.name, "bench", "all") == "bench:bench:all"


def test_the_cli_summary_carries_the_book_and_a_book_failure_never_loses_a_generation(tmp_path, capsys):
    from rsi_arena.cli import _with_book
    task = KalshiHorizon(windows=[])
    rollouts = _rollouts(task)
    task.use_instances([r.instance for r in rollouts])
    harness = Harness.load(ROOT / "harnesses" / "horizon-5m-jev.json")
    out = _with_book(task, rollouts, tmp_path / "gen1", "baseline", "train", harness)
    assert out["instances"] == 3 and "statistic" in out and out["book"]["cycles"] >= 3
    assert (tmp_path / "gen1" / "books" / "baseline.train.json").exists()
    assert rollouts[0].outcome.details["trade"], "attached in place, so the dump that follows sees it"
    # Off a run: stats, no file.
    fresh = _rollouts(task)
    out = _with_book(task, fresh, None, "bench", "all", harness)
    assert out["book"]["cycles"] >= 3 and not list(tmp_path.glob("**/bench.all.json"))
    # A spec that blows up is logged and the summary is the plain one.
    broken = KalshiHorizon(windows=[])
    broken._trading = SimpleNamespace(costs=None)
    out = _with_book(broken, _rollouts(task), tmp_path / "gen2", "baseline", "train", harness)
    assert "book" not in out and out["instances"] == 3
    assert "paper book for baseline.train not built" in capsys.readouterr().err
    # A task with no trading at all.
    plain = SimpleNamespace(name="t", statistic=lambda o: 0.0, summary=lambda o: {})
    assert "book" not in _with_book(plain, _rollouts(task), None, "b", "t", harness)


def test_backfill_rebuilds_the_books_of_a_run_from_its_rollouts(tmp_path):
    task = KalshiHorizon(windows=[])
    rollouts = _rollouts(task)
    run_dir = tmp_path / "runs" / "kalshi-jev" / "gen3"
    (run_dir / "rollouts").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "topic": "kalshi-horizon-5m", "run_dir": str(run_dir), "incumbent": "harnesses/horizon-5m-jev.json",
        "incumbent_fingerprint": "fp-in", "candidate_fingerprint": "fp-cand",
        "settings": {"benchmark": "benchmarks/soccer-2026.json", "windows_dir": "benchmarks/windows"}}))
    rows = [{"instance": r.instance.to_dict(), "outcome": r.outcome.to_dict(), "cost_usd": 0.001,
             "run": {"harness": "kalshi-horizon-5m-jev", "run_id": "r1", "ok": True, "cost_usd": 0.001,
                     "output": r.run.output}} for r in rollouts]
    (run_dir / "rollouts" / "baseline.train.json").write_text(json.dumps(rows))
    remembered = [dict(r, run=None) for r in rows]
    (run_dir / "rollouts" / "candidate.holdout.json").write_text(json.dumps(remembered))
    backfill = load_script("backfill_books")
    log = []
    stats = backfill.backfill(run_dir, log=log.append)
    assert set(stats) == {"baseline.train", "candidate.holdout"}
    base = json.loads((run_dir / "books" / "baseline.train.json").read_text())
    cand = json.loads((run_dir / "books" / "candidate.holdout.json").read_text())
    assert base["harness_fp"] == "fp-in" and base["harness_name"] == "kalshi-horizon-5m-jev"
    assert cand["harness_fp"] == "fp-cand" and cand["book_id"] == "gen3@kalshi-jev:candidate:holdout"
    assert base["stats"]["trades"] >= 1, "the run's outputs drove the book"
    assert cand["stats"]["trades"] == base["stats"]["trades"], "a remembered rollout trades off its details"
    assert backfill.main([str(run_dir)]) == 0 and backfill.main([str(tmp_path)]) == 1


def test_backfill_replays_a_bare_rollout_with_the_touch_the_set_has_since_kept():
    quoted = Window(ticker="K", at=T0, mid_now=0.5, realised=0.52, yes_bid=0.47, yes_ask=0.53,
                    yes_bid_h=0.51, yes_ask_h=0.53)
    other = Window(ticker="K", at=T0 + 5 * M, mid_now=0.6, realised=0.6, yes_bid=0.59, yes_ask=0.61)
    task = KalshiHorizon(windows=[quoted, other])
    bare = Window.from_dict({"ticker": "K", "at": T0.isoformat(), "mid_now": 0.5, "realised": 0.52})
    rebuilt = Window.from_dict({"ticker": "K", "at": (T0 + 5 * M).isoformat(), "mid_now": 0.61, "realised": 0.6})
    outcome = Outcome(value=0.5, feedback="", details={})
    rollouts = [Rollout(instance=bare, run=None, outcome=outcome, remembered_cost=0.001),
                Rollout(instance=rebuilt, run=None, outcome=outcome, remembered_cost=0.001)]
    backfill = load_script("backfill_books")
    out = backfill.with_set_quotes(task, rollouts)
    assert out[0].instance is quoted and out[0].remembered_cost == 0.001, "same question: the set's copy"
    assert out[1].instance is rebuilt, "a different mid is a different question; the dump stands"
    entry, _ = task.trading.quote_of(out[0].instance)
    assert (entry.bid, entry.ask, entry.proxy) == (0.47, 0.53, False)
    # A task that cannot list its set leaves the rollouts alone.
    broken = SimpleNamespace(instances=lambda: (_ for _ in ()).throw(RuntimeError("no set")))
    assert backfill.with_set_quotes(broken, rollouts) is rollouts


# -- the seeds ---------------------------------------------------------------------------

JEV_SEEDS = ["horizon-5m-jev.json", "horizon-5m-jev-gated.json",
             "crypto-horizon-1m-jev.json", "news-equity-5m-jev.json"]
CHAT_SEEDS = ["horizon-5m.json", "crypto-horizon-1m.json", "news-equity-5m.json"]


def task_of(name: str):
    """The topic a seed file belongs to, built empty - only its box is wanted."""
    if name.startswith("crypto"):
        return CryptoHorizon(windows=[])
    if name.startswith("news"):
        return NewsEquity(windows=[])
    return KalshiHorizon(windows=[])


def width_answer(level: int, levels: int = 5) -> dict:
    return {"type": "score", "score": float(level), "confidence": 0.7,
            "probabilities": {str(i): (1.0 if i == level else 0.0) for i in range(levels)}}


@pytest.mark.parametrize("name", JEV_SEEDS + CHAT_SEEDS)
def test_every_seed_names_only_tools_its_topic_offers(name):
    """The seeds used to list three of the twenty a box holds, and a rewrite of
    ``tools`` almost never survived its minibatch, so the derived tools were
    never reached. They are listed now - and every name has to be one the topic
    would actually bind, or the harness fails every window at ``check``."""
    h = Harness.load(ROOT / "harnesses" / name)
    task = task_of(name)
    box = set(task.tools())
    assert set(h.tools) <= box, sorted(set(h.tools) - box)
    assert h.plan.tools_used() - {"*"} <= set(h.tools), "the plan calls what it lists"
    assert h.plan.required_inputs() <= task.inputs, sorted(h.plan.required_inputs() - task.inputs)
    derived = {"state_summary", "move_base_rate", "tape_imbalance"}
    assert derived <= set(h.tools), sorted(derived - set(h.tools))
    assert "ask_opus" in h.description or "ask_opus" in h.tools, \
        "the seed says the model tools exist even when it does not use them"


@pytest.mark.parametrize("name", JEV_SEEDS)
def test_the_jev_seeds_choose_the_width_they_post(name):
    """Width is a question with venue-appropriate levels, not the move
    distribution's dispersion. The levels start at the venue tick and increase,
    and the widest is well past the median path, so the answer is a choice
    between a market that trades and one that does not."""
    h = Harness.load(ROOT / "harnesses" / name)
    step = h.plan.steps[-1]
    validate_questions(step.questions, step.answers)
    tick = task_of(name).trading.tick
    key = "half_width_cents" if name.startswith("horizon") else "half_width_bps"
    values = step.questions["width"]["values"]
    assert step.questions["width"]["type"] == "score"
    assert len(values) == len(step.questions["width"]["criteria"]) == 5
    assert values[0] == tick, f"the narrowest offered width is the venue tick, {tick}"
    assert all(values[i] < values[i + 1] for i in range(len(values) - 1)), values
    assert values[-1] >= 8 * tick, "the widest offered width is well past the median path"
    assert step.answers[key] == {"from": "width", "as": "mean", "min": tick}
    assert "half_range" not in json.dumps(step.answers), "dispersion no longer sets the width"
    # Every level, and a spread over two of them, maps to a width at or above the tick.
    for level in range(len(values)):
        out = answers_to_output(step.questions, {**answers_of(step), "width": width_answer(level)},
                               step.answers)
        assert out[key] == pytest.approx(values[level]) and out[key] >= tick
    blurred = {"type": "score", "score": 0.5, "probabilities": {"0": 0.5, "1": 0.5}}
    out = answers_to_output(step.questions, {**answers_of(step), "width": blurred}, step.answers)
    assert out[key] == pytest.approx((values[0] + values[1]) / 2) and out[key] >= tick


def answers_of(step) -> dict:
    """A full answer set for a seed's questions but the width, which each test sets."""
    n = len(step.questions["move"]["criteria"])
    return {"move": {"type": "score", "score": 3.0, "confidence": 0.5,
                     "probabilities": {str(i): (1.0 if i == 3 else 0.0) for i in range(n)}},
            "action": {"type": "choice", "choice": "open_short",
                       "probabilities": {"open_short": 0.9, "hold": 0.1}},
            "size": {"type": "score", "score": 3.0, "confidence": 0.8,
                     "probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0}}}


@pytest.mark.parametrize("name", JEV_SEEDS)
def test_the_jev_seeds_ask_for_an_order_and_the_answers_become_one(name):
    h = Harness.load(ROOT / "harnesses" / name)
    step = h.plan.steps[-1]
    validate_questions(step.questions, step.answers)
    assert step.questions["action"]["type"] == "choice"
    assert set(step.questions["action"]["criteria"]) == {"open_long", "open_short", "close", "hold"}
    assert step.questions["size"]["criteria"] == ["nothing", "a toe: 2%", "a normal position: 5%", "the cap: 10%"]
    assert step.questions["size"]["values"] == [0, 0.02, 0.05, 0.10]
    assert step.answers["action"] == {"from": "action", "as": "choice"}
    assert step.answers["size"] == {"from": "size", "as": "mean", "min": 0, "max": 0.1}
    answers = {**answers_of(step), "width": width_answer(1)}
    out = answers_to_output(step.questions, answers, step.answers)
    assert out["action"] == "open_short" and isinstance(out["size"], float) and out["size"] == 0.1
    assert read_decision(out).size == 0.1 and read_decision(out).action == "open_short"
    spread = dict(answers, size={"type": "score", "score": 1.5,
                                 "probabilities": {"0": 0.0, "1": 0.5, "2": 0.5, "3": 0.0}})
    assert 0.0 <= answers_to_output(step.questions, spread, step.answers)["size"] <= 0.1


@pytest.mark.parametrize("name", CHAT_SEEDS)
def test_the_chat_seeds_require_both_decision_fields_in_strict_mode(name):
    schema = Harness.load(ROOT / "harnesses" / name).plan.steps[-1].output_schema
    assert {"action", "size"} <= set(schema["required"])
    assert set(schema["required"]) == set(schema["properties"]), "strict mode: every property required"
    assert schema["properties"]["action"]["enum"] == ["open_long", "open_short", "close", "hold"]
    assert (schema["properties"]["size"]["minimum"], schema["properties"]["size"]["maximum"]) == (0, 0.1)
    assert schema["additionalProperties"] is False
    # The width is described as the market that gets posted, not a confidence band.
    key = "half_width_cents" if name.startswith("horizon") else "half_width_bps"
    said = schema["properties"][key]["description"]
    assert "POSTED" in said and "not a confidence band" in said, said


def test_the_width_table_counts_a_fill_the_way_the_book_does(tmp_path):
    """``scripts/width_fills.py`` is what says whether the offered widths bracket
    the range the answer changes over, so it has to fill a side exactly as
    ``Book.post`` does: the bid at a bar whose low reaches it, the ask at a bar
    whose high does. One window, three outcomes as the width widens."""
    fills = load_script("width_fills")
    (tmp_path / "w.json").write_text(json.dumps([
        {"ticker": "K", "at": T0.isoformat(), "mid_now": 0.50, "realised": 0.52,
         "path": [{"ts": (T0 + M).isoformat(), "high": 0.54, "low": 0.47, "close": 0.52}]},
        {"ticker": "K", "at": T0.isoformat(), "mid_now": 0.50, "realised": 0.50, "path": None},
    ]))
    topic = fills.Topic("t", str(tmp_path), "cents", False, (1, 4, 8))
    out = fills.measure(topic)
    assert out["windows"] == 1 and out["pathless"] == 1, "a window with no path posts nothing"
    assert out["reach"]["p50"] == 4.0, "the wider side: four cents up against three down"
    both, one, none = out["widths"]
    assert (both["width"], both["both"]) == (1, 1.0), "a one-cent market is taken on both sides"
    assert (one["width"], one["one_side_only"], one["both"]) == (4, 1.0, 0.0), "the ask only"
    assert (none["width"], none["no_fill"]) == (8, 1.0), "nobody reaches eight cents"
    assert fills.main(["--topic", "news-equity-5m", "--json"]) == 0


# -- the collectors keep the touch ---------------------------------------------------------

def test_the_kalshi_collector_keeps_the_touch_at_the_instant_and_the_horizon():
    live = load_script("collect_live")
    now = datetime.now(UTC)
    hist = FakeHistory(now - 12 * M, {"T": {m: 0.40 + 0.01 * m for m in range(0, 12)}})
    row = {"at": (now - 7 * M).isoformat(), "ticker": "T", "mid_now": 0.45,
           "output": {"delta_cents": 2.0, "half_width_cents": 1.0}}
    assert live.resolve(row, hist) is True
    assert row["realised"] == pytest.approx(0.50)
    assert row["realised_quote"] == {"bid": pytest.approx(0.49), "ask": pytest.approx(0.51), "mid": pytest.approx(0.50)}
    # A book with no touch at the horizon grades and carries no quote.
    hist = FakeHistory(now - 12 * M, {"T": {m: 0.40 + 0.01 * m for m in range(0, 12)}}, dead={("T", 10)})
    row = {"at": (now - 7 * M).isoformat(), "ticker": "T", "mid_now": 0.45, "output": {"delta_cents": 2.0}}
    assert live.resolve(row, hist) is True and "realised_quote" not in row


def test_the_crypto_book_carries_its_ladder_and_the_sweep_prices_the_exit_off_its_snapshots():
    from rsi_arena.crypto.replay import live_tools
    from tests.conftest import ramp
    from tests.test_live_crypto import FakeBook, FakeSources
    t0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=14)
    spot = FakeBook(t0, {"BTCUSDT": ramp(60, 100000.0, 1.0)})
    box = live_tools(t0 + 30 * M, spot, None, symbols=("BTCUSDT",))
    res = box["order_book"].safe_call(symbol="BTCUSDT", depth=3)
    assert res.ok and len(res.data["bids"]) == 3 and len(res.data["asks"]) == 3
    assert res.data["bids"][0][0] == res.data["bid"] and "bids" not in json.loads(res.text)
    live = load_script("collect_live_crypto")
    at = t0 + 30 * M
    row = {"at": at.isoformat(), "ticker": f"BTCUSDT@{at.isoformat()}", "symbol": "BTCUSDT",
           "mid_now": 100000.0, "output": {"delta_bps": 1.0, "half_width_bps": 3.0}}
    snaps = [{"symbol": "BTCUSDT", "at": (at + 2 * M).isoformat(),
              "book": {"order_book": {"bid": 99.0, "ask": 101.0, "mid": 100.0, "bids": [[99.0, 1.0]], "asks": [[101.0, 1.0]]}}},
             {"symbol": "BTCUSDT", "at": (at + 1 * M).isoformat(),
              "book": {"order_book": {"bid": 98.0, "ask": 102.0, "mid": 100.0}}},
             {"symbol": "ETHUSDT", "at": (at + 1 * M).isoformat(),
              "book": {"order_book": {"bid": 1.0, "ask": 2.0, "mid": 1.5}}}]
    late = dict(row, at=(at - 10 * M).isoformat(), ticker=f"BTCUSDT@{(at - 10 * M).isoformat()}")
    bare = dict(row)
    assert live.resolve(row, FakeSources(spot, symbols=("BTCUSDT",)), 1, snaps) is True
    assert row["realised_quote"] == {"bid": 98.0, "ask": 102.0, "mid": 100.0}, "the nearest book after the horizon"
    assert live.resolve(late, FakeSources(spot, symbols=("BTCUSDT",)), 1, snaps) is True
    assert "realised_quote" not in late, "nothing within three minutes after the horizon"
    assert live.resolve(bare, FakeSources(spot, symbols=("BTCUSDT",)), 1) is True
    assert "realised_quote" not in bare, "no snapshots, no quote"


def test_the_news_collector_grades_without_a_quote():
    live = load_script("collect_live_news")
    now = datetime(2026, 6, 1, 14, 20, tzinfo=UTC)
    tape = FakeBars(now, {"ACME": {m: 100.0 * (1 + 2.0 * m / 1e4) for m in range(-40, 40)}})
    row = {"at": now.isoformat(), "ticker": f"ACME@{now.isoformat()}#n", "symbol": "ACME", "mid_now": 100.0,
           "output": {"delta_bps": 10.0, "half_width_bps": 5.0}}
    assert live.resolve(row, tape, now=now + 7 * M) is True and "realised_quote" not in row


# -- the live trader ------------------------------------------------------------------------

def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _kalshi_rows(t0: datetime) -> list[dict]:
    rows = []
    for i in range(3):
        mid = 0.50 + 0.01 * i
        rows.append({"at": (t0 + i * 5 * M).isoformat(), "ticker": "KXEPL-BRE", "mid_now": mid,
                     "yes_bid": mid - 0.005, "yes_ask": mid + 0.005, "harness": "kalshi-horizon-5m",
                     "realised": mid + 0.01, "realised_quote": {"bid": mid + 0.005, "ask": mid + 0.015, "mid": mid + 0.01},
                     "output": {"delta_cents": 8.0, "half_width_cents": 0.5}, "scored": {"skill": 0.1}})
    rows.append({"at": (t0 + 15 * M).isoformat(), "ticker": "KXEPL-BRE", "mid_now": 0.53, "harness": "kalshi-horizon-5m",
                 "realised": None, "output": None, "scored": None})           # ungraded: skipped
    return rows


def _crypto_rows(t0: datetime) -> tuple[list[dict], list[dict]]:
    rows, books = [], []
    for i in range(3):
        at = (t0 + i * M).isoformat()
        mid = 100_000.0 * (1 + 0.0001 * i)
        rows.append({"at": at, "topic": "crypto-horizon-1m", "symbol": "BTCUSDT", "ticker": f"BTCUSDT@{at}",
                     "mid_now": mid, "realised": mid * 1.0001, "harness": "crypto-horizon-1m-jev",
                     "context": {"book": {"bid": mid - 1, "ask": mid + 1}},
                     "output": {"delta_bps": 60.0, "half_width_bps": 5.0}})
        books.append({"topic": "crypto-horizon-1m", "symbol": "BTCUSDT", "at": at,
                      "book": {"order_book": {"bid": mid - 1, "ask": mid + 1, "bids": [[mid - 1, 5.0]],
                                              "asks": [[mid + 1, 0.5], [mid + 2, 5.0]]}}})
    return rows, books


def _news_rows(t0: datetime) -> list[dict]:
    return [{"at": (t0 + i * 5 * M).isoformat(), "topic": "news-equity-5m", "symbol": "ACME",
             "ticker": f"ACME@{(t0 + i * 5 * M).isoformat()}#n{i}", "mid_now": 100.0 + i,
             "realised": 100.5 + i, "harness": "news-equity-5m-jev",
             "output": {"delta_bps": 40.0, "half_width_bps": 10.0}} for i in range(2)]


@pytest.mark.parametrize("topic", ["kalshi-horizon-5m", "crypto-horizon-1m", "news-equity-5m"])
def test_paper_trade_dry_run_reads_every_topic_shape_and_writes_nothing(tmp_path, topic):
    pt = load_script("paper_trade")
    t0 = datetime(2026, 6, 1, 14, 20, tzinfo=UTC)
    books = None
    if topic == "kalshi-horizon-5m":
        rows, harness = _kalshi_rows(t0), "harnesses/horizon-5m.json"
    elif topic == "crypto-horizon-1m":
        (rows, snaps), harness = _crypto_rows(t0), "harnesses/crypto-horizon-1m-jev.json"
        books = _write(tmp_path / "books.jsonl", snaps)
    else:
        rows, harness = _news_rows(t0), "harnesses/news-equity-5m-jev.json"
    forecasts = _write(tmp_path / "f.jsonl", rows)
    argv = ["--topic", topic, "--forecasts", str(forecasts), "--harness", str(ROOT / harness),
            "--state", str(tmp_path / "books" / f"{topic}.json"), "--out-dir", str(tmp_path), "--dry-run"]
    if books:
        argv += ["--books", str(books)]
    summary = pt.run(pt.build_parser().parse_args(argv), log=lambda m: None)
    assert summary["dry_run"] and summary["applied"] == summary["cycles"] >= 2
    assert summary["opened"] + summary["trades_closed"] >= 1, "a sixty-bps call clears every venue's costs"
    assert summary["harness"] == rows[0]["harness"]
    assert not (tmp_path / "books").exists() and not list(tmp_path.glob("*-trades.jsonl"))


def test_paper_trade_appends_the_lines_the_publisher_expects_and_a_rerun_appends_nothing(tmp_path):
    pt = load_script("paper_trade")
    t0 = datetime(2026, 6, 1, 14, 20, tzinfo=UTC)
    rows, snaps = _crypto_rows(t0)
    forecasts, books = _write(tmp_path / "f.jsonl", rows), _write(tmp_path / "b.jsonl", snaps)
    state = tmp_path / "live" / "books" / "crypto-horizon-1m.json"
    argv = ["--topic", "crypto-horizon-1m", "--forecasts", str(forecasts), "--books", str(books),
            "--harness", str(ROOT / "harnesses" / "crypto-horizon-1m-jev.json"),
            "--state", str(state), "--out-dir", str(tmp_path / "live")]
    first = pt.run(pt.build_parser().parse_args(argv), log=lambda m: None)
    assert first["applied"] == 3 and first["marks"] == 3 and first["open_positions"] == 1
    trades = [json.loads(l) for l in (tmp_path / "live" / "crypto-horizon-1m-trades.jsonl").read_text().splitlines()]
    marks = [json.loads(l) for l in (tmp_path / "live" / "crypto-horizon-1m-marks.jsonl").read_text().splitlines()]
    assert len(marks) == 3 and all(m["topic"] == "crypto-horizon-1m" and m["book_id"] == "live:crypto-horizon-1m" for m in marks)
    assert trades and all(t["book_id"] == "live:crypto-horizon-1m" and t["harness_name"] == "crypto-horizon-1m-jev"
                          and t["harness_fp"] and t["instrument"] == "BTCUSDT" for t in trades)
    assert trades[-1]["closed_at"] is None, "the open position is a line with no close"
    saved = json.loads(state.read_text())
    assert saved["stats"]["cycles"] == 3 and saved["harness_name"] == "crypto-horizon-1m-jev"
    assert saved["started_at"] == t0.isoformat() and saved["last_at"] == (t0 + 2 * M).isoformat()
    assert saved["positions"]["BTCUSDT"]["qty"] > 0

    again = pt.run(pt.build_parser().parse_args(argv), log=lambda m: None)
    assert again["applied"] == 0 and again["marks"] == 0 and again["opened"] == 0
    assert len((tmp_path / "live" / "crypto-horizon-1m-marks.jsonl").read_text().splitlines()) == 3
    assert len((tmp_path / "live" / "crypto-horizon-1m-trades.jsonl").read_text().splitlines()) == len(trades)
    assert json.loads(state.read_text()) == saved

    # A later sweep past the hour closes the carried position at its deadline.
    later = dict(rows[0], at=(t0 + 90 * M).isoformat(), ticker=f"BTCUSDT@{(t0 + 90 * M).isoformat()}",
                 output={"delta_bps": 0.0, "half_width_bps": 5.0})
    _write(forecasts, rows + [later])
    third = pt.run(pt.build_parser().parse_args(argv), log=lambda m: None)
    assert third["applied"] == 1 and third["trades_closed"] == 1
    closed = [json.loads(l) for l in (tmp_path / "live" / "crypto-horizon-1m-trades.jsonl").read_text().splitlines()][-1]
    assert closed["reason"] == "force_close" and closed["closed_at"] is not None
    assert closed["opened_at"] == trades[-1]["opened_at"], "the close fills in the line the open made"


def test_paper_trade_builds_the_spec_without_a_task():
    pt = load_script("paper_trade")
    for topic, venue in (("kalshi-horizon-5m", "kalshi"), ("crypto-horizon-1m", "perp"), ("news-equity-5m", "equity")):
        spec = pt.trading_spec_for(topic)
        assert spec.costs.name == venue and spec.topic == topic
