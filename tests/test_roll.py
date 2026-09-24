"""``scripts/roll_question_set.py``: the weekly roll adds, prunes, and nothing else.

Offline, like everything else here. Each topic's roll takes its venue as
callables — ``discover``, ``fill``, ``build``, ``quote``, ``fill_paths`` — so a test supplies
fakes and the assertions are about the files on disk afterwards: which window
files exist, which are gone, which are byte for byte what they were.

The properties under test are the ones a roll can break silently and expensively:

- an existing window's ``id``/``mid_now``/``realised`` is the scoreboard's key,
  so a surviving file that changed is a memory detached from its question;
- a prune that deletes the wrong file costs a question set that cannot be
  rebuilt without the venue;
- a roll run twice on a Sunday must be one roll, because the workflow retries;
- ``--dry-run`` must write nothing at all, including the daily bars a liquidity
  screen would otherwise leave in ``benchmarks/``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def load_script():
    spec = importlib.util.spec_from_file_location("roll_question_set",
                                                  ROOT / "scripts" / "roll_question_set.py")
    mod = importlib.util.module_from_spec(spec)
    # Registered before it is executed: ``@dataclass`` resolves a
    # ``from __future__ import annotations`` string annotation by looking the
    # defining module up in ``sys.modules``, and a module that is not there
    # yet raises rather than resolving.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def roll():
    return load_script()


# -- fixtures on disk -----------------------------------------------------------

def _window(ticker: str, minute: int) -> dict:
    return {"ticker": ticker, "at": (datetime(2026, 9, 1, 12, tzinfo=UTC)
                                     + timedelta(minutes=minute)).isoformat(),
            "mid_now": 0.5, "realised": 0.52, "game": {"game_id": "1"},
            "event": ticker.rsplit("-", 1)[0]}


def kalshi_set(tmp_path: Path, events: list[str]) -> tuple[Path, Path]:
    """A benchmark of ``events`` with a window file each, as a built set looks."""
    bench = tmp_path / "soccer.json"
    windows = tmp_path / "windows"
    windows.mkdir()
    rows = [{"league": e.split("-")[0].replace("KX", "").replace("GAME", ""),
             "game": str(9000 + i), "event": e, "tickers": [e + "-A", e + "-B"]}
            for i, e in enumerate(events)]
    bench.write_text(json.dumps(rows, indent=2) + "\n")
    for e in events:
        (windows / f"{e}.every5.h5.json").write_text(json.dumps([_window(e + "-A", 0)]))
    return bench, windows


def crypto_set(tmp_path: Path, start: date, end: date) -> tuple[Path, Path]:
    bench = tmp_path / "crypto.json"
    windows = tmp_path / "windows-crypto"
    windows.mkdir()
    bench.write_text(json.dumps({"symbols": ["BTCUSDT"], "from": start.isoformat(),
                                 "to": end.isoformat(), "every": 5, "horizon": 1,
                                 "data_dir": str(tmp_path / "data")}, indent=1))
    d = start
    while d <= end:
        (windows / f"D{d:%Y%m%d}.every5.h1.json").write_text(json.dumps([{"symbol": "BTCUSDT"}]))
        d += timedelta(days=1)
    return bench, windows


def news_item(symbol: str, day: str, news_id: str, hour: int = 15) -> dict:
    """One benchmark row. 15:00 UTC is mid-session in New York all year."""
    return {"symbol": symbol, "news_id": news_id, "at": f"{day}T{hour:02d}:00:00+00:00",
            "headline": f"{symbol} {news_id}", "summary": "", "source": "benzinga",
            "symbols": [symbol], "updated_at": None}


def news_set(tmp_path: Path, rows: list[dict]) -> tuple[Path, Path]:
    bench = tmp_path / "news.json"
    windows = tmp_path / "windows-news"
    windows.mkdir()
    bench.write_text(json.dumps(rows, indent=1) + "\n")
    for r in rows:
        group = f"{r['symbol']}-{r['at'][:10].replace('-', '')}"
        (windows / f"{group}.h5.json").write_text(json.dumps([{"symbol": r["symbol"]}]))
    return bench, windows


# -- kalshi ---------------------------------------------------------------------

def test_kalshi_appends_dedupes_and_prunes_only_what_it_dropped(roll, tmp_path):
    """Four matches in, two settled since, keep three: the two oldest go and
    their window files with them - and nothing else in the directory moves."""
    events = ["KXEPLGAME-26SEP01AAABBB", "KXEPLGAME-26SEP05CCCDDD",
              "KXEPLGAME-26SEP09EEEFFF", "KXEPLGAME-26SEP14GGGHHH"]
    bench, windows = kalshi_set(tmp_path, events)
    before = {p.name: p.read_bytes() for p in windows.iterdir()}
    found = [{"league": "EPL", "game": "1", "event": "KXEPLGAME-26SEP18IIIJJJ",
              "tickers": ["a", "b"], "windows": 30},
             {"league": "EPL", "game": "2", "event": "KXEPLGAME-26SEP20KKKLLL",
              "tickers": ["c", "d"], "windows": 30}]
    built, quoted = [], []

    def discover(rows, since, until):
        assert since == date(2026, 9, 14)            # the newest ticker date in the set
        return found, False

    def build(rows):
        built.extend(r["event"] for r in rows)
        for r in rows:
            (windows / f"{r['event']}.every5.h5.json").write_text(json.dumps([_window("t", 0)]))

    def quote(paths):
        quoted.extend(p.name for p in paths)

    out = roll.roll_kalshi(bench, windows, keep=4, until=date(2026, 9, 22), dry_run=False,
                           discover=discover, build=build, quote=quote, log=lambda m: None)

    assert out.added == ["KXEPLGAME-26SEP18IIIJJJ", "KXEPLGAME-26SEP20KKKLLL"]
    assert out.removed == ["KXEPLGAME-26SEP01AAABBB", "KXEPLGAME-26SEP05CCCDDD"]
    assert out.groups == 4 and out.status == "ok"
    rows = json.loads(bench.read_text())
    assert [r["event"] for r in rows] == events[2:] + [f["event"] for f in found]
    # `windows` is diagnostic on a discovery row and is not part of the contract.
    assert all("windows" not in r for r in rows)
    on_disk = {p.name for p in windows.iterdir()}
    assert on_disk == {f"{e}.every5.h5.json" for e in events[2:] + [f["event"] for f in found]}
    # The two that stayed are untouched, byte for byte.
    for name in (f"{events[2]}.every5.h5.json", f"{events[3]}.every5.h5.json"):
        assert (windows / name).read_bytes() == before[name]
    assert built == [f["event"] for f in found]
    assert quoted == [f"{f['event']}.every5.h5.json" for f in found]


def test_kalshi_dedupes_by_event_and_by_match(roll, tmp_path):
    """One match listed under two tickers is one row, not two sides of a split."""
    bench, windows = kalshi_set(tmp_path, ["KXEPLGAME-26SEP01AAABBB"])
    seen = []

    def events_of(league):
        return [{"event_ticker": t} for t in
                ("KXEPLGAME-26SEP05RENPSG", "KXEPLGAME-26SEP05PSGREN",
                 "KXEPLGAME-26SEP01AAABBB")]

    def resolve(league, ticker):
        seen.append(ticker)
        return {"league": "EPL", "game": "401876487", "event": ticker,
                "tickers": ["a", "b"], "windows": 30}, ""

    rows = json.loads(bench.read_text())
    new, cut = roll.discover_kalshi(rows, date(2026, 9, 1), date(2026, 9, 22),
                                    events_of=events_of, resolve=resolve,
                                    budget=roll.Budget(10), log=lambda m: None, date_spread=3)
    assert cut is False
    assert [r["event"] for r in new] == ["KXEPLGAME-26SEP05RENPSG"]
    # The ticker already in the set was never resolved - that is a round trip saved.
    assert seen == ["KXEPLGAME-26SEP05RENPSG", "KXEPLGAME-26SEP05PSGREN"]


def test_kalshi_budget_stops_adding_and_keeps_what_it_found(roll, tmp_path):
    """The clock runs out mid-listing: what was found is kept, the roll says so."""
    bench, windows = kalshi_set(tmp_path, ["KXEPLGAME-26SEP01AAABBB"])
    now = [0.0]

    def clock():
        return now[0]

    budget = roll.Budget(1, clock=clock)

    def events_of(league):
        return [{"event_ticker": f"KXEPLGAME-26SEP0{n}AAA{n}BB"} for n in (2, 3, 4, 5)]

    def resolve(league, ticker):
        now[0] += 40.0                                # two resolutions spend the minute
        return {"league": "EPL", "game": ticker[-3:], "event": ticker,
                "tickers": ["a", "b"], "windows": 30}, ""

    new, cut = roll.discover_kalshi(json.loads(bench.read_text()), date(2026, 9, 1),
                                    date(2026, 9, 22), events_of=events_of, resolve=resolve,
                                    budget=budget, log=lambda m: None, date_spread=3)
    assert cut is True and len(new) == 2

    out = roll.roll_kalshi(bench, windows, keep=99, until=date(2026, 9, 22), dry_run=True,
                           discover=lambda *a: (new, True), build=None, quote=None,
                           log=lambda m: None)
    assert out.status == "budget" and "budget" in out.reason


def test_kalshi_second_run_the_same_day_does_nothing(roll, tmp_path):
    bench, windows = kalshi_set(tmp_path, ["KXEPLGAME-26SEP01AAABBB", "KXEPLGAME-26SEP05CCCDDD"])
    before = bench.read_bytes(), {p.name: p.read_bytes() for p in windows.iterdir()}
    out = roll.roll_kalshi(bench, windows, keep=2, until=date(2026, 9, 22), dry_run=False,
                           discover=lambda *a: ([], False),
                           build=lambda rows: pytest.fail("built on a no-op roll"),
                           quote=lambda paths: pytest.fail("quoted on a no-op roll"),
                           log=lambda m: None)
    assert out.status == "nothing" and out.reason == "nothing to add"
    assert (bench.read_bytes(), {p.name: p.read_bytes() for p in windows.iterdir()}) == before


def test_kalshi_dry_run_writes_nothing(roll, tmp_path):
    bench, windows = kalshi_set(tmp_path, ["KXEPLGAME-26SEP01AAABBB", "KXEPLGAME-26SEP05CCCDDD"])
    before = bench.read_bytes(), sorted(p.name for p in windows.iterdir())
    found = [{"league": "EPL", "game": "1", "event": "KXEPLGAME-26SEP18IIIJJJ",
              "tickers": ["a", "b"]}]
    out = roll.roll_kalshi(bench, windows, keep=1, until=date(2026, 9, 22), dry_run=True,
                           discover=lambda *a: (found, False),
                           build=lambda rows: pytest.fail("built on a dry run"),
                           quote=lambda paths: pytest.fail("quoted on a dry run"),
                           log=lambda m: None)
    assert out.added == ["KXEPLGAME-26SEP18IIIJJJ"]
    assert out.removed == ["KXEPLGAME-26SEP01AAABBB", "KXEPLGAME-26SEP05CCCDDD"]
    assert (bench.read_bytes(), sorted(p.name for p in windows.iterdir())) == before


def test_kalshi_refuses_to_finish_if_a_surviving_window_changed(roll, tmp_path):
    """The one thing a roll may never do. A build that rewrote a file already in
    the set would detach the scoreboard's memory from its question."""
    bench, windows = kalshi_set(tmp_path, ["KXEPLGAME-26SEP01AAABBB"])
    found = [{"league": "EPL", "game": "1", "event": "KXEPLGAME-26SEP18IIIJJJ",
              "tickers": ["a", "b"]}]

    def build(rows):
        (windows / "KXEPLGAME-26SEP01AAABBB.every5.h5.json").write_text("[]")

    with pytest.raises(roll.RollError, match="surviving window file changed"):
        roll.roll_kalshi(bench, windows, keep=99, until=date(2026, 9, 22), dry_run=False,
                         discover=lambda *a: (found, False), build=build,
                         quote=lambda paths: None, log=lambda m: None)



def test_kalshi_fills_paths_on_the_new_files_only(roll, tmp_path):
    """``path_windows.fill_paths`` is given the files the roll just built, and
    nothing that was already in the set."""
    bench, windows = kalshi_set(tmp_path, ["KXEPLGAME-26SEP01AAABBB", "KXEPLGAME-26SEP05CCCDDD"])
    found = [{"league": "EPL", "game": "1", "event": "KXEPLGAME-26SEP18IIIJJJ",
              "tickers": ["a", "b"]}]
    pathed = []

    def build(rows):
        for r in rows:
            (windows / f"{r['event']}.every5.h5.json").write_text(json.dumps([_window("t", 0)]))

    out = roll.roll_kalshi(bench, windows, keep=99, until=date(2026, 9, 22), dry_run=False,
                           discover=lambda *a: (found, False), build=build,
                           quote=lambda paths: None,
                           fill_paths=lambda paths: pathed.extend(p.name for p in paths),
                           log=lambda m: None)
    assert out.status == "ok"
    assert pathed == ["KXEPLGAME-26SEP18IIIJJJ.every5.h5.json"]


def test_a_missing_path_script_is_a_notice_and_not_a_failure(roll, monkeypatch, tmp_path):
    """The path filler is written on another branch. A checkout without it
    rolls anyway, with the notice in the log."""
    said = []
    monkeypatch.setattr(roll, "load_script",
                        lambda name: (_ for _ in ()).throw(FileNotFoundError(f"no {name}.py")))
    assert roll.load_optional("path_windows", "fill_paths", log=said.append) is None
    assert any("path_windows.py not available" in m for m in said)

    class Stub:
        pass

    monkeypatch.setattr(roll, "load_script", lambda name: Stub())
    said.clear()
    assert roll.load_optional("path_windows", "fill_paths", log=said.append) is None
    assert any("no fill_paths()" in m for m in said)

# -- crypto ---------------------------------------------------------------------

def test_crypto_moves_both_ends_and_deletes_the_days_it_dropped(roll, tmp_path):
    bench, windows = crypto_set(tmp_path, date(2026, 9, 1), date(2026, 9, 5))
    filled = []

    def fill(symbols, days, data_dir):
        filled.extend(days)
        return {"to": days[-1], "klines": {"BTCUSDT": {"days": len(days), "held": 0,
                                                       "fetched": len(days), "short_days": []}}}

    def build(bench_span):
        for d in bench_span.days():
            (windows / f"D{d:%Y%m%d}.every5.h1.json").write_text(json.dumps([{"symbol": "BTCUSDT"}]))

    out = roll.roll_crypto(bench, windows, keep=5, until=date(2026, 9, 7), dry_run=False,
                           fill=fill, build=build, log=lambda m: None)

    assert filled == [date(2026, 9, 6), date(2026, 9, 7)]
    assert out.added == ["D20260906", "D20260907"]
    assert out.removed == ["D20260901", "D20260902"]
    raw = json.loads(bench.read_text())
    assert (raw["from"], raw["to"]) == ("2026-09-03", "2026-09-07")
    assert {p.name for p in windows.iterdir()} == {
        f"D2026090{n}.every5.h1.json" for n in (3, 4, 5, 6, 7)}
    assert out.groups == 5
    # The kline store is never pruned: it is cheap, and the live collector reads it.
    assert raw["klines"]["BTCUSDT"]["fetched"] == 2


def test_crypto_keeps_what_it_filled_when_the_budget_runs_out(roll, tmp_path):
    bench, windows = crypto_set(tmp_path, date(2026, 9, 1), date(2026, 9, 5))

    def fill(symbols, days, data_dir):
        return {"to": days[0], "klines": {}}          # one day of four

    def build(bench_span):
        for d in bench_span.days():
            (windows / f"D{d:%Y%m%d}.every5.h1.json").write_text("[]")

    out = roll.roll_crypto(bench, windows, keep=99, until=date(2026, 9, 9), dry_run=False,
                           fill=fill, build=build, log=lambda m: None)
    assert out.status == "budget" and out.added == ["D20260906"]
    assert json.loads(bench.read_text())["to"] == "2026-09-06"


def test_crypto_dry_run_and_second_run(roll, tmp_path):
    bench, windows = crypto_set(tmp_path, date(2026, 9, 1), date(2026, 9, 5))
    before = bench.read_bytes(), sorted(p.name for p in windows.iterdir())

    dry = roll.roll_crypto(bench, windows, keep=3, until=date(2026, 9, 6), dry_run=True,
                           fill=lambda *a: pytest.fail("fetched on a dry run"),
                           build=lambda b: pytest.fail("built on a dry run"), log=lambda m: None)
    assert dry.added == ["D20260906"] and dry.removed == ["D20260901", "D20260902", "D20260903"]
    assert (bench.read_bytes(), sorted(p.name for p in windows.iterdir())) == before

    same = roll.roll_crypto(bench, windows, keep=5, until=date(2026, 9, 5), dry_run=False,
                            fill=lambda *a: pytest.fail("fetched on a no-op roll"),
                            build=lambda b: pytest.fail("built on a no-op roll"),
                            log=lambda m: None)
    assert same.status == "nothing"
    assert (bench.read_bytes(), sorted(p.name for p in windows.iterdir())) == before


# -- news -----------------------------------------------------------------------

def test_news_appends_dedupes_by_symbol_and_id_and_drops_oldest_sessions(roll, tmp_path):
    from rsi_arena.topics.news_equity.windows import BenchmarkItem

    rows = [news_item("AAPL", "2026-09-01", "1"), news_item("MSFT", "2026-09-01", "2"),
            news_item("AAPL", "2026-09-02", "3"), news_item("AAPL", "2026-09-03", "4")]
    bench, windows = news_set(tmp_path, rows)
    found = [BenchmarkItem.from_dict(news_item("AAPL", "2026-09-04", "5")),
             # The same story on the same name: already in the set, dropped.
             BenchmarkItem.from_dict(news_item("AAPL", "2026-09-03", "4")),
             # The same story on a second name: kept, it is a second question.
             BenchmarkItem.from_dict(news_item("NVDA", "2026-09-04", "5"))]
    built, filled = [], []

    def build(items):
        built.extend(sorted({i.group for i in items}))
        for i in items:
            (windows / f"{i.group}.h5.json").write_text(json.dumps([{"symbol": i.symbol}]))

    # Four symbol-days kept: dropping 1 September's two leaves exactly four, and
    # dropping 2 September's one as well would leave three, which is under the
    # floor - so the prune stops after the first session.
    out = roll.roll_news(bench, windows, keep=4, until=date(2026, 9, 4), dry_run=False,
                         discover=lambda s, e: (found, False),
                         fill=lambda items: filled.extend(i.news_id for i in items),
                         build=build, log=lambda m: None)

    assert out.added == ["AAPL-20260904", "NVDA-20260904"]
    # The first session goes whole, both of its symbol-days with it.
    assert out.removed == ["AAPL-20260901", "MSFT-20260901"]
    assert out.groups == 4
    kept = json.loads(bench.read_text())
    assert [r["news_id"] for r in kept] == ["3", "4", "5", "5"]
    assert {p.name for p in windows.iterdir()} == {
        "AAPL-20260902.h5.json", "AAPL-20260903.h5.json",
        "AAPL-20260904.h5.json", "NVDA-20260904.h5.json"}
    assert sorted(filled) == ["5", "5"]


def test_news_second_run_and_dry_run_write_nothing(roll, tmp_path):
    rows = [news_item("AAPL", "2026-09-01", "1"), news_item("AAPL", "2026-09-02", "2")]
    bench, windows = news_set(tmp_path, rows)
    before = bench.read_bytes(), sorted(p.name for p in windows.iterdir())

    from rsi_arena.topics.news_equity.windows import BenchmarkItem
    found = [BenchmarkItem.from_dict(news_item("AAPL", "2026-09-03", "3"))]
    dry = roll.roll_news(bench, windows, keep=1, until=date(2026, 9, 3), dry_run=True,
                         discover=lambda s, e: (found, False),
                         fill=lambda items: pytest.fail("fetched on a dry run"),
                         build=lambda items: pytest.fail("built on a dry run"),
                         log=lambda m: None)
    assert dry.added == ["AAPL-20260903"] and dry.removed == ["AAPL-20260901", "AAPL-20260902"]
    assert (bench.read_bytes(), sorted(p.name for p in windows.iterdir())) == before

    same = roll.roll_news(bench, windows, keep=2, until=date(2026, 9, 2), dry_run=False,
                          discover=lambda s, e: ([], False),
                          fill=lambda items: pytest.fail("fetched on a no-op roll"),
                          build=lambda items: pytest.fail("built on a no-op roll"),
                          log=lambda m: None)
    assert same.status == "nothing"
    assert (bench.read_bytes(), sorted(p.name for p in windows.iterdir())) == before


# -- validation and the revert --------------------------------------------------

def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_a_failed_validation_reverts_the_topic_and_reports_rather_than_failing(
        roll, tmp_path, monkeypatch):
    """The set that was committed is the set that is there afterwards: the
    benchmark restored, the window files the roll wrote removed."""
    repo = tmp_path / "repo"
    (repo / "benchmarks").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    bench, windows = kalshi_set(repo / "benchmarks", ["KXEPLGAME-26SEP01AAABBB"])
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "the set", cwd=repo)
    original = bench.read_bytes()

    found = [{"league": "EPL", "game": "1", "event": "KXEPLGAME-26SEP18IIIJJJ",
              "tickers": ["a", "b"]}]

    def sources(args, budget, log):
        def build(rows):
            for r in rows:
                (windows / f"{r['event']}.every5.h5.json").write_text("[]")
        return {"discover": lambda rows, since, until: (found, False),
                "build": build, "quote": lambda paths: None}

    monkeypatch.setitem(roll.SOURCES, roll.KALSHI, sources)
    monkeypatch.setattr(roll, "validate",
                        lambda topic, **kw: (False, "preflight.py exited 1: groups fill the split", {}))
    monkeypatch.chdir(repo)

    import argparse
    args = argparse.Namespace(topic=roll.KALSHI, keep=99, until=None, dry_run=False,
                              budget_minutes=5.0, validate=True, benchmark=str(bench),
                              windows_dir=str(windows), data_dir=None, limit=10,
                              universe="", min_dollar_volume=0.0, per_symbol_day=4)
    out = roll.roll_one(args, log=lambda m: None)

    assert out.status == "reverted"
    assert out.reason.startswith("roll reverted: ")
    assert bench.read_bytes() == original
    assert {p.name for p in windows.iterdir()} == {"KXEPLGAME-26SEP01AAABBB.every5.h5.json"}


def test_validate_runs_both_commands_and_stops_at_the_first_failure(roll):
    calls = []

    class Done:
        def __init__(self, code, out=""):
            self.returncode, self.stdout, self.stderr = code, out, ""

    def run(cmd, **kw):
        calls.append(cmd)
        if "preflight.py" in cmd[1]:
            return Done(1, "  FAIL  groups fill the split")
        return Done(0, json.dumps({"instances": 10, "train": 8, "holdout": 2, "groups": [1, 2]}))

    ok, why, summary = validate = roll.validate(roll.KALSHI, benchmark="b.json",
                                                windows_dir="w", run=run, log=lambda m: None)
    assert ok is False and "preflight.py exited 1" in why
    assert len(calls) == 2
    assert calls[0][3] == "windows" and "--benchmark" in calls[0] and "--windows-dir" in calls[0]
    assert "--topic" in calls[1] and "--benchmark" in calls[1]


# -- the loop guard --------------------------------------------------------------

def test_the_roll_waits_for_the_loop_and_then_skips(roll):
    now = [0.0]
    naps = []

    def clock():
        return now[0]

    def sleep(s):
        naps.append(s)
        now[0] += s

    free = roll.wait_for_loop(minutes=30, poll_s=120, runs=lambda: ["123 (in_progress)"],
                              sleep=sleep, clock=clock, log=lambda m: None)
    assert free is False
    # Every two minutes for half an hour, and not a call more.
    assert naps == [120.0] * 15


def test_the_roll_goes_ahead_once_the_loop_is_idle(roll):
    answers = [["123 (in_progress)"], []]
    now = [0.0]
    free = roll.wait_for_loop(minutes=30, poll_s=120, runs=lambda: answers.pop(0),
                              sleep=lambda s: now.__setitem__(0, now[0] + s),
                              clock=lambda: now[0], log=lambda m: None)
    assert free is True and answers == []


def test_loop_runs_reads_gh_and_a_gh_that_fails_does_not_stop_the_roll(roll):
    class Done:
        def __init__(self, code, out):
            self.returncode, self.stdout, self.stderr = code, out, "gh: not logged in"

    seen = []

    def run(cmd, **kw):
        status = cmd[cmd.index("--status") + 1]
        seen.append(status)
        return Done(0, json.dumps([{"databaseId": 7, "status": status}]))

    assert roll.loop_runs(run=run) == ["7 (in_progress)", "7 (queued)"]
    assert seen == ["in_progress", "queued"]

    def broken(cmd, **kw):
        return Done(1, "")

    with pytest.raises(roll.RollError):
        roll.loop_runs(run=broken)
    # And the wait treats that as "go ahead": GitHub being unreachable must not
    # mean the set never rolls again.
    assert roll.wait_for_loop(minutes=1, runs=lambda: roll.loop_runs(run=broken),
                              sleep=lambda s: None, clock=lambda: 0.0,
                              log=lambda m: None) is True


# -- small tools -----------------------------------------------------------------

def test_write_atomic_leaves_no_temp_file_and_replaces_whole(roll, tmp_path):
    p = tmp_path / "deep" / "b.json"
    roll.write_atomic(p, '{"a": 1}')
    roll.write_atomic(p, '{"a": 2}')
    assert json.loads(p.read_text()) == {"a": 2}
    assert [q.name for q in p.parent.iterdir()] == ["b.json"]


def test_with_retries_backs_off_three_times_then_gives_up(roll):
    pauses = []
    tries = [0]

    def flaky():
        tries[0] += 1
        if tries[0] < 3:
            raise ConnectionError("reset by peer")
        return "ok"

    assert roll.with_retries(flaky, sleep=pauses.append, log=lambda m: None) == "ok"
    assert pauses == [2.0, 4.0]

    with pytest.raises(ConnectionError):
        roll.with_retries(lambda: (_ for _ in ()).throw(ConnectionError("down")),
                          sleep=pauses.append, log=lambda m: None)


def test_event_date_reads_the_ticker(roll):
    assert roll.event_date("KXEPLGAME-26SEP14LEENEW") == date(2026, 9, 14)
    assert roll.event_date("not-a-fixture") is None
    assert roll.newest_kickoff([{"event": "KXEPLGAME-26SEP14LEENEW"},
                                {"event": "KXMLSGAME-26AUG02ATLCLT"}]) == date(2026, 9, 14)
