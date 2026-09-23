"""The paper-book publisher: what it writes, in what order, and when it must not.

Offline like the rest. The database is a cursor that records every statement
and its parameters; ``execute_values`` is replaced by one that hands the rows
to that cursor, so the batch inserts land in the same log as the deletes and
the order can be asserted.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")

ROOT = Path(__file__).resolve().parent.parent
TOPIC = "kalshi-horizon-5m"


def load_script(name: str):
    """A script under ``scripts/`` as a module. They are entry points, not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCursor:
    """Records (sql, params) and answers the one probe the publisher makes."""

    def __init__(self, tables=("books", "trades", "book_marks")):
        self.calls: list[tuple[str, object]] = []
        self.tables = set(tables)
        self._pending: list[tuple] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        if "information_schema.tables" in sql:
            self._pending = [(t,) for t in sorted(self.tables)]

    def fetchall(self):
        out, self._pending = self._pending, []
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.calls if "information_schema" not in sql]


class FakeConn:
    """Enough of a psycopg2 connection for ``main``: a context that commits on
    a clean exit and, when told to, fails the commit instead."""

    def __init__(self, cursor: FakeCursor, *, commit_fails: bool = False):
        self.cur = cursor
        self.commit_fails = commit_fails
        self.autocommit = None
        self.closed = False

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.commit_fails:
            raise RuntimeError("commit failed: connection lost")
        return False

    def close(self):
        self.closed = True


@pytest.fixture
def pub(monkeypatch):
    module = load_script("publish_trading")

    def values(cur, sql, rows, **_kw):
        cur.execute(sql, list(rows))

    monkeypatch.setattr(module, "execute_values", values)
    return module


def trade(instrument="KXEPL-BRE", opened="2026-09-20T15:00:00+00:00", **over) -> dict:
    row = {"instrument": instrument, "side": "long", "opened_at": opened, "closed_at": None,
           "entry_px": 0.32, "exit_px": None, "qty": 100, "size_usd": 32.0, "fees_usd": 0.7,
           "pnl_usd": None, "reason": None, "source": "harness", "run_id": "gen1@kalshi-jev",
           "instance_id": "KXEPL-BRE@2026-09-20T15:00:00+00:00"}
    row.update(over)
    return row


def mark(at="2026-09-20T15:00:00+00:00", **over) -> dict:
    row = {"at": at, "equity_usd": 1000.0, "cash_usd": 968.0, "gross_exposure_usd": 32.0,
           "open_positions": 1, "drawdown": 0.0, "event": None}
    row.update(over)
    return row


STATS = {"total_return": 0.01, "sharpe": 0.5, "daily_sharpe": 0.4, "max_drawdown": -0.02,
         "hit_rate": 0.5, "avg_win": 1.0, "avg_loss": -1.0, "profit_factor": 1.0,
         "turnover": 2.0, "fees_usd": 1.4, "trades": 2, "cycles": 3, "open_positions": 0,
         "start_equity": 1000.0, "end_equity": 1010.0, "handovers": 0, "refusals": 0}


def book_file(run_dir: Path, side="baseline", split="holdout", topic_in_manifest=True) -> Path:
    (run_dir / "books").mkdir(parents=True, exist_ok=True)
    if topic_in_manifest:
        (run_dir / "manifest.json").write_text(json.dumps({"topic": TOPIC}))
    run_id = f"{run_dir.name}@kalshi-jev"
    closed = trade(opened="2026-09-20T14:00:00+00:00", closed_at="2026-09-20T14:05:00+00:00",
                   exit_px=0.34, pnl_usd=1.3, reason="horizon")
    path = run_dir / "books" / f"{side}.{split}.json"
    path.write_text(json.dumps({
        "book_id": f"{run_id}:{side}:{split}", "harness_fp": "fp-1", "harness_name": "horizon-5m",
        "kind": "replay", "run_id": run_id, "side": side, "split": split,
        "started_at": "2026-09-20T14:00:00+00:00", "stats": STATS,
        "trades": [closed, trade()], "marks": [mark("2026-09-20T14:00:00+00:00"), mark()]}))
    return path


# ---------------------------------------------------------------------------
# A replay book file.

def test_a_book_file_is_replaced_whole_in_order(pub, tmp_path):
    path = book_file(tmp_path / "gen1")
    cur = FakeCursor()
    assert pub.publish_book_file(cur, path) == (2, 2)

    sql = cur.statements
    assert len(sql) == 5
    assert sql[0].startswith("insert into rsi.books")
    assert "on conflict (topic, book_id) do update" in sql[0]
    assert sql[1].startswith("delete from rsi.trades where topic = %s and book_id = %s")
    assert sql[2].startswith("delete from rsi.book_marks where topic = %s and book_id = %s")
    assert sql[3].startswith("insert into rsi.trades")
    assert "on conflict (topic, book_id, instrument, opened_at) do update" in sql[3]
    assert sql[4].startswith("insert into rsi.book_marks")
    assert "on conflict (topic, book_id, at) do nothing" in sql[4]

    # The books row: the topic came from the manifest two directories up.
    book = dict(zip(pub.BOOK_COLUMNS, cur.calls[0][1]))
    assert (book["topic"], book["book_id"], book["kind"]) == \
        (TOPIC, "gen1@kalshi-jev:baseline:holdout", "replay")
    assert (book["run_id"], book["side"], book["split"]) == ("gen1@kalshi-jev", "baseline", "holdout")
    assert book["stats"].adapted == STATS
    assert cur.calls[1][1] == cur.calls[2][1] == (TOPIC, "gen1@kalshi-jev:baseline:holdout")

    # Trades take the book's harness and id; the open one has nulls where it should.
    trades = [dict(zip(pub.TRADE_COLUMNS, t)) for t in cur.calls[3][1]]
    assert [t["closed_at"] for t in trades] == ["2026-09-20T14:05:00+00:00", None]
    assert {t["harness_fp"] for t in trades} == {"fp-1"}
    assert {t["book_id"] for t in trades} == {"gen1@kalshi-jev:baseline:holdout"}
    assert trades[0]["pnl_usd"] == 1.3 and trades[1]["pnl_usd"] is None
    marks = [dict(zip(pub.MARK_COLUMNS, m)) for m in cur.calls[4][1]]
    assert [m["at"] for m in marks] == ["2026-09-20T14:00:00+00:00", "2026-09-20T15:00:00+00:00"]
    assert marks[0]["equity_usd"] == 1000.0


def test_the_topic_can_be_told_and_falls_back_to_the_first(pub, tmp_path):
    cur = FakeCursor()
    pub.publish_book_file(cur, book_file(tmp_path / "gen2"), topic="crypto-horizon-1m")
    assert cur.calls[0][1][0] == "crypto-horizon-1m"

    cur = FakeCursor()
    pub.publish_book_file(cur, book_file(tmp_path / "gen3", topic_in_manifest=False))
    assert cur.calls[0][1][0] == pub.DEFAULT_TOPIC


def test_a_trade_that_opened_and_closed_in_one_file_is_one_row(pub):
    """``on conflict do update`` cannot touch a row twice in one statement, and
    a trade's open and close share a key. The close is the line that counts."""
    cur = FakeCursor()
    rows = [pub.trade_row(TOPIC, "b", trade()),
            pub.trade_row(TOPIC, "b", trade(closed_at="2026-09-20T15:05:00+00:00", pnl_usd=-0.4)),
            pub.trade_row(TOPIC, "b", trade(instrument=None))]        # malformed: dropped
    assert pub.upsert_trades(cur, rows) == 1
    (only,) = cur.calls[-1][1]
    assert dict(zip(pub.TRADE_COLUMNS, only))["pnl_usd"] == -0.4


# ---------------------------------------------------------------------------
# The live book, through the sidecar.

def live_files(live_dir: Path, topic: str = TOPIC) -> tuple[Path, Path, Path]:
    trades_path = live_dir / f"{topic}-trades.jsonl"
    marks_path = live_dir / f"{topic}-marks.jsonl"
    state_path = live_dir / "books" / f"{topic}.json"
    state_path.parent.mkdir(parents=True)
    live = {"topic": topic, "book_id": f"live:{topic}", "harness_fp": "fp-live",
            "harness_name": "horizon-5m"}
    trades_path.write_text(
        json.dumps({**trade(), **live}) + "\n"
        + json.dumps({**trade(closed_at="2026-09-20T15:05:00+00:00", pnl_usd=0.8,
                              reason="horizon"), **live}) + "\n")
    marks_path.write_text(
        json.dumps({**mark(), "topic": topic, "book_id": f"live:{topic}"}) + "\n"
        + json.dumps({**mark("2026-09-20T15:05:00+00:00", open_positions=0),
                      "topic": topic, "book_id": f"live:{topic}"}) + "\n")
    state_path.write_text(json.dumps({"harness_fp": "fp-live", "harness_name": "horizon-5m",
                                      "started_at": "2026-09-19T12:00:00+00:00", "stats": STATS}))
    return trades_path, marks_path, state_path


def run_main(pub, monkeypatch, conn: FakeConn, live_dir: Path, *extra: str) -> int:
    monkeypatch.setattr(pub.psycopg2, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(pub.sys, "argv", ["publish_trading.py", "--topic", TOPIC,
                                          "--live-dir", str(live_dir), "--db-url", "x", *extra])
    return pub.main()


def test_live_mode_publishes_past_the_sidecar_and_advances_it_after_commit(pub, tmp_path, monkeypatch):
    trades_path, marks_path, _state = live_files(tmp_path)
    # The first line of each file was published by an earlier invocation.
    first_trade = len(trades_path.read_text().splitlines()[0]) + 1
    first_mark = len(marks_path.read_text().splitlines()[0]) + 1
    trades_path.with_suffix(".jsonl.published").write_text(json.dumps({"bytes": first_trade}))
    marks_path.with_suffix(".jsonl.published").write_text(json.dumps({"bytes": first_mark}))

    cur = FakeCursor()
    assert run_main(pub, monkeypatch, FakeConn(cur), tmp_path) == 0

    sql = cur.statements
    assert [s.split(" (")[0] for s in sql] == \
        ["insert into rsi.books", "insert into rsi.trades", "insert into rsi.book_marks"]
    book = dict(zip(pub.BOOK_COLUMNS, cur.calls[1][1]))
    assert (book["book_id"], book["kind"], book["harness_fp"]) == (f"live:{TOPIC}", "live", "fp-live")
    assert book["stats"].adapted == STATS
    # Only the second line of each: the close, with its exit filled in.
    (only_trade,) = cur.calls[2][1]
    assert dict(zip(pub.TRADE_COLUMNS, only_trade))["closed_at"] == "2026-09-20T15:05:00+00:00"
    assert dict(zip(pub.TRADE_COLUMNS, only_trade))["book_id"] == f"live:{TOPIC}"
    (only_mark,) = cur.calls[3][1]
    assert dict(zip(pub.MARK_COLUMNS, only_mark))["open_positions"] == 0

    # And the sidecars now point at the end of each file.
    assert json.loads(trades_path.with_suffix(".jsonl.published").read_text())["bytes"] == \
        trades_path.stat().st_size
    assert json.loads(marks_path.with_suffix(".jsonl.published").read_text())["bytes"] == \
        marks_path.stat().st_size


def test_a_failed_commit_leaves_the_sidecar_where_it_was(pub, tmp_path, monkeypatch):
    trades_path, marks_path, _state = live_files(tmp_path)
    with pytest.raises(RuntimeError, match="commit failed"):
        run_main(pub, monkeypatch, FakeConn(FakeCursor(), commit_fails=True), tmp_path)
    assert not trades_path.with_suffix(".jsonl.published").exists()
    assert not marks_path.with_suffix(".jsonl.published").exists()
    # The next invocation reads from the start again.
    assert pub.offset_of(trades_path.with_suffix(".jsonl.published"), trades_path) == 0


def test_without_009_the_publisher_says_so_and_writes_nothing(pub, tmp_path, monkeypatch, capsys):
    trades_path, _marks, _state = live_files(tmp_path)
    cur = FakeCursor(tables=("books",))            # mid-migration counts as absent
    conn = FakeConn(cur)
    assert run_main(pub, monkeypatch, conn, tmp_path) == 0
    assert cur.statements == []
    assert conn.closed
    assert "009_trading.sql" in capsys.readouterr().out
    assert not trades_path.with_suffix(".jsonl.published").exists()


def test_the_dry_run_touches_nothing(pub, tmp_path, monkeypatch, capsys):
    trades_path, _marks, _state = live_files(tmp_path)

    def no_connect(*a, **k):
        raise AssertionError("a dry run must not connect")

    monkeypatch.setattr(pub.psycopg2, "connect", no_connect)
    monkeypatch.setattr(pub.sys, "argv", ["publish_trading.py", "--topic", TOPIC,
                                          "--live-dir", str(tmp_path), "--dry-run"])
    assert pub.main() == 0
    assert "2 trade lines, 2 marks would be written" in capsys.readouterr().out
    assert not trades_path.with_suffix(".jsonl.published").exists()


def test_nothing_on_disk_is_not_a_failure(pub, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pub.sys, "argv", ["publish_trading.py", "--topic", TOPIC,
                                          "--live-dir", str(tmp_path / "missing")])
    assert pub.main() == 0
    assert "nothing to publish" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# publish_runs hands its book files over.

def test_publish_runs_publishes_the_books_it_finds(tmp_path, monkeypatch):
    runs = load_script("publish_runs")
    run_dir = tmp_path / "gen4"
    book_file(run_dir, "baseline", "holdout")
    book_file(run_dir, "candidate", "holdout")
    book_file(run_dir, "candidate", "train")
    seen: list[tuple[Path, str]] = []
    monkeypatch.setattr(runs, "publish_book_file",
                        lambda cur, path, topic=None: seen.append((Path(path).name, topic)))

    cur = FakeCursor()
    assert runs.publish_books(cur, run_dir, TOPIC, trading=True) == 3
    assert seen == [("baseline.holdout.json", TOPIC), ("candidate.holdout.json", TOPIC),
                    ("candidate.train.json", TOPIC)]

    # No books directory: nothing to do, no probe.
    assert runs.publish_books(cur, tmp_path / "gen5", TOPIC) == 0
    assert cur.calls == []


def test_publish_runs_skips_the_books_with_a_notice_before_009(tmp_path, monkeypatch, capsys):
    runs = load_script("publish_runs")
    run_dir = tmp_path / "gen6"
    book_file(run_dir)
    monkeypatch.setattr(runs, "publish_book_file",
                        lambda *a, **k: pytest.fail("must not publish without the tables"))
    cur = FakeCursor(tables=())
    assert runs.publish_books(cur, run_dir, TOPIC) == 0          # probed, absent
    assert "009_trading.sql" in capsys.readouterr().out
    assert runs.publish_books(cur, run_dir, TOPIC, trading=False) == 0


# ---------------------------------------------------------------------------
# The workflows that call it.

WORKFLOWS = {
    "live.yml": ("kalshi-horizon-5m", "runs/live/forecasts.jsonl", None),
    "live-crypto.yml": ("crypto-horizon-1m", "runs/live/crypto-forecasts.jsonl",
                        "--books runs/live/crypto-books.jsonl"),
    "live-news.yml": ("news-equity-5m", "runs/live/news-forecasts.jsonl", None),
}


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_the_live_workflow_trades_and_publishes_the_book(name):
    yaml = pytest.importorskip("yaml")
    topic, forecasts, extra = WORKFLOWS[name]
    doc = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    steps = doc["jobs"]["collect"]["steps"]
    names = [s.get("name") for s in steps]
    order = ["Publish to the reader", "Restore the paper book", "Paper-trade the sweep",
             "Save the paper book", "Publish the paper book", "Keep the forecasts as an artifact"]
    assert [n for n in names if n in order] == order
    by = {s.get("name"): s for s in steps}

    restore, save = by["Restore the paper book"], by["Save the paper book"]
    assert restore["uses"].startswith("actions/cache/restore@")
    assert save["uses"].startswith("actions/cache/save@")
    assert restore["with"]["path"] == save["with"]["path"] == "runs/live/books"
    assert restore["with"]["key"] == save["with"]["key"] == \
        f"paper-book-{topic}-${{{{ github.run_id }}}}"
    assert restore["with"]["restore-keys"] == f"paper-book-{topic}-"
    assert "runs/live/books" in by["Keep the forecasts as an artifact"]["with"]["path"]

    run = by["Paper-trade the sweep"]["run"]
    assert "scripts/paper_trade.py" in run
    for arg in (f"--topic {topic}", f"--forecasts {forecasts}", "--harness",
                f"--state runs/live/books/{topic}.json"):
        assert arg in run, arg
    assert (extra in run) if extra else ("--books" not in run)
    assert "always()" in by["Paper-trade the sweep"]["if"]

    publish = by["Publish the paper book"]
    assert f"scripts/publish_trading.py --topic {topic}" in publish["run"]
    assert "env.SUPABASE_DB_URL != ''" in publish["if"]
    # The same guard as the forecasts' publish, whatever it is in this file.
    assert publish["if"] == by["Publish to the reader"]["if"]
