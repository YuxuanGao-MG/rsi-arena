"""The working record: the quote on a rollout, the search in two tables.

Offline like the rest. The database is a cursor that records every statement and
its parameters and answers the probes; ``execute_values`` is replaced by one that
hands the rows to that cursor, so the batch inserts land in the same log as the
deletes and the order can be asserted.

What is being pinned down here is mostly what happens when something is *not*
there: a rollout with no paper book, a run with no ``gepa/`` directory, a
database that has not had migration 010 applied. Every one of those is a notice
and a published run, because the publisher runs on a cron beside a migration
someone applies by hand and a reader missing one column must not cost a day of
forecasts.
"""

from __future__ import annotations

import importlib.util
import json
import pickle
from pathlib import Path

import pytest

pytest.importorskip("psycopg2")

ROOT = Path(__file__).resolve().parent.parent
TOPIC = "kalshi-horizon-5m"
TICKER = "KXEPL-26SEP20ARSMCI-ARS"
AT = "2026-09-20T15:00:00+00:00"
INSTANCE = f"{TICKER}@{AT}"

#: The columns migration 008 left rsi.rollouts with, which is what a database
#: that has not had 010 applied answers the probe with.
PRE_010 = ("id", "run_id", "side", "split", "fixture", "ticker", "at", "mid_now", "realised",
           "predicted", "half_width", "err", "naive_error", "skill", "echoed", "unmeasurable",
           "scored", "cost_usd", "ok", "error_text", "output", "game", "feedback", "topic", "unit")

#: The same instant for rsi.live_forecasts: 002's columns and 008's, no more.
LIVE_PRE_010 = ("at", "league", "game_id", "ticker", "mid_now", "realised", "harness", "output",
                "game", "spans", "skill", "scored", "ok", "error_text", "topic", "symbol",
                "venue", "context", "unit")


def load_script(name: str):
    """A script under ``scripts/`` as a module. They are entry points, not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The database.

class FakeCursor:
    """Records (sql, params) and answers the probes and selects it is given.

    ``columns`` is what ``information_schema.columns`` says about the table being
    published, ``tables`` what ``information_schema.tables`` says (009's three
    and 010's two), and ``rows`` queues answers for the reading tools by a
    fragment of their SQL.
    """

    def __init__(self, *, columns=PRE_010 + ("quote", "fills", "path"),
                 tables=("books", "trades", "book_marks", "candidates", "candidate_scores"),
                 rows=None):
        self.calls: list[tuple[str, object]] = []
        self.columns, self.tables = tuple(columns), set(tables)
        self.answers = dict(rows or {})
        self._pending: list[tuple] = []
        self.description: list[tuple] | None = None

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))
        self.description = None
        if "information_schema.columns" in flat:
            self._pending = [(c,) for c in self.columns]
            return
        if "information_schema.tables" in flat:
            self._pending = [(t,) for t in sorted(self.tables)]
            return
        for fragment, (names, rows) in self.answers.items():
            if fragment in flat:
                self.description = [(n,) for n in names]
                self._pending = [tuple(r) for r in rows]
                return
        self._pending = []

    def fetchall(self):
        out, self._pending = self._pending, []
        return out

    def fetchone(self):
        out = self.fetchall()
        return out[0] if out else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _ in self.calls if "information_schema" not in sql]

    def params_of(self, fragment: str):
        return next(params for sql, params in self.calls if sql.startswith(fragment))


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self.cur = cursor
        self.autocommit = None
        self.closed = False

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self.closed = True


def batched(module):
    """``execute_values`` that logs on the cursor and can return ids."""
    def values(cur, sql, rows, **kw):
        rows = list(rows)
        cur.execute(sql, rows)
        return [(i + 1,) for i, _ in enumerate(rows)] if kw.get("fetch") else None
    return values


@pytest.fixture
def runs(monkeypatch):
    module = load_script("publish_runs")
    monkeypatch.setattr(module, "execute_values", batched(module))
    return module


# ---------------------------------------------------------------------------
# Fixtures on disk.

TRADE = {
    "action": "open_long", "size": 0.05, "source": "harness",
    "quote": {"bid": 0.3, "ask": 0.34, "size_frac": 0.02, "size_usd": 20000.0,
              "source": "harness", "mid_now": 0.32, "unit": "cents"},
    "fills": [{"side": "buy", "px": 0.3, "qty": 66666.0, "notional_usd": 20000.0,
               "fees_usd": 7.4, "at": AT, "bar_ts": AT, "effect": "open"}],
    "path_summary": {"bars": 5, "high": 0.36, "low": 0.29, "close": 0.35,
                     "crossed_bid": True, "crossed_ask": True},
    "pnl_usd": 120.0, "fees_usd": 7.4, "reason": "quote", "refused": False,
}


def rollout(*, trade: dict | None = TRADE, ticker: str = TICKER) -> dict:
    details = {"mid_now": 0.32, "realised": 0.35, "predicted": 0.33, "half_width": 0.02,
               "error": 0.02, "naive_error": 0.03, "skill": 0.33, "echoed": False,
               "unmeasurable": False, "scored": True, "unit": "cents"}
    if trade is not None:
        details["trade"] = trade
    return {"instance": {"ticker": ticker, "at": AT, "group": "KXEPL-26SEP20ARSMCI",
                         "game": {"game_id": "1", "league": "EPL"}},
            "outcome": {"value": 0.5, "feedback": "scored. Book: open_long 5% -> +120 USD (quote)",
                        "objectives": {}, "details": details},
            "cost_usd": 0.03,
            "run": {"ok": True, "output": {"delta_cents": 1.0, "half_width_cents": 2.0}}}


SEED = {"context": "the seed context", "plan": '{"steps": []}', "tools": "game_state"}
GREW = {"context": "the seed context, with rather more words than it had", "plan": SEED["plan"],
        "tools": SEED["tools"]}
REPLANNED = {"context": SEED["context"], "plan": '{"steps": ["look"]}', "tools": SEED["tools"]}
VALSET = [INSTANCE, "KXEPL-26SEP20ARSMCI-MCI@2026-09-20T15:05:00+00:00"]


def run_dir(tmp_path: Path, *, gepa: bool = True, state: bool = True,
            accepted: bool = True, rollouts: bool = True) -> Path:
    """A run directory shaped like one ``rsi-arena optimize`` leaves behind."""
    path = tmp_path / "gen12"
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps({
        "topic": TOPIC, "created": AT, "parent": None,
        "settings": {"model": "anthropic/claude-opus-5"},
        "decision": {"accepted": accepted, "reasons": ["for the test"]},
        "search": {"best_idx": 1, "candidates": 3, "valset": len(VALSET)}}))
    if rollouts:
        (path / "rollouts").mkdir(exist_ok=True)
        (path / "rollouts" / "baseline.holdout.json").write_text(json.dumps([rollout()]))
    if gepa:
        (path / "gepa").mkdir(exist_ok=True)
        (path / "gepa" / "candidates.json").write_text(json.dumps([SEED, GREW, REPLANNED]))
        (path / "valset.json").write_text(json.dumps(VALSET))
        if state:
            (path / "gepa" / "gepa_state.bin").write_bytes(pickle.dumps({
                "program_candidates": [SEED, GREW, REPLANNED],
                "prog_candidate_val_subscores": [{0: 0.50, 1: 0.40},
                                                 {0: 0.60, 1: 0.44},
                                                 {0: 0.30, 1: 0.30}],
                "prog_candidate_objective_scores": [{"skill": 0.45, "cost": 0.10},
                                                    {"skill": 0.52, "cost": 0.12},
                                                    {"skill": 0.30, "cost": 0.09}],
                "parent_program_for_candidate": [[None], [0], [0]],
                "num_metric_calls_by_discovery": [0, 12, 30]}))
    return path


# ---------------------------------------------------------------------------
# 1. The record on a rollout row.

def test_a_rollout_carries_the_quote_the_fills_and_the_path(runs):
    row = runs.rollout_row("gen12", "candidate", "holdout", rollout(), topic=TOPIC, unit="cents")
    assert row["quote"].adapted == TRADE["quote"]
    assert row["fills"].adapted == TRADE["fills"]
    assert row["path"].adapted == TRADE["path_summary"]
    # The three columns are the record's, unpacked; nothing else moved.
    assert (row["skill"], row["err"], row["unit"]) == (0.33, 0.02, "cents")


def test_a_rollout_with_no_book_carries_none_rather_than_an_empty_object(runs):
    row = runs.rollout_row("gen12", "baseline", "train", rollout(trade=None), topic=TOPIC)
    assert (row["quote"], row["fills"], row["path"]) == (None, None, None)


def test_a_quote_nobody_took_is_an_empty_array_and_not_a_null(runs):
    """Null means no book replayed this window; an empty array means the book
    posted a quote and the tape never reached it. Opposite findings."""
    unhit = {**TRADE, "fills": [], "path_summary": {**TRADE["path_summary"],
                                                    "crossed_bid": False, "crossed_ask": False}}
    row = runs.rollout_row("gen12", "candidate", "holdout", rollout(trade=unhit), topic=TOPIC)
    assert row["fills"].adapted == []
    assert row["path"].adapted["crossed_bid"] is False


def test_the_insert_names_the_new_columns_when_the_database_has_them(runs, tmp_path):
    path = run_dir(tmp_path)
    cur = FakeCursor()
    assert runs.publish_rollouts(cur, "gen12", path, TOPIC) == (1, 0)
    insert = next(sql for sql in cur.statements if sql.startswith("insert into rsi.rollouts"))
    assert "quote, fills, path" in insert
    (only,) = cur.params_of("insert into rsi.rollouts")
    assert len(only) == len(runs.COLUMNS)


def test_a_database_without_010_still_gets_its_rollouts(runs, tmp_path):
    """``columns_of`` is the whole reason this publisher can run on a cron beside
    a migration applied by hand. A column the table lacks, named in an insert,
    fails every row of the run."""
    path = run_dir(tmp_path)
    cur = FakeCursor(columns=PRE_010)
    assert runs.publish_rollouts(cur, "gen12", path, TOPIC) == (1, 0)
    insert = next(sql for sql in cur.statements if sql.startswith("insert into rsi.rollouts"))
    for absent in ("quote", "fills", "path"):
        assert absent not in insert
    (only,) = cur.params_of("insert into rsi.rollouts")
    assert len(only) == len(PRE_010) - 1            # every column but the serial id


# ---------------------------------------------------------------------------
# 2. The search, in two tables.

def test_the_candidates_carry_the_diff_against_the_seed(runs, tmp_path):
    path = run_dir(tmp_path)
    cur = FakeCursor()
    manifest = json.loads((path / "manifest.json").read_text())
    assert runs.publish_search(cur, path, "gen12", TOPIC, manifest) == (3, 6)

    sql = cur.statements
    assert sql[0].startswith("insert into rsi.candidates")
    assert "on conflict (topic, run_id, candidate_idx) do update" in sql[0]
    assert sql[1].startswith("insert into rsi.candidate_scores")
    assert "on conflict (topic, run_id, candidate_idx, instance_id) do update" in sql[1]

    rows = [dict(zip(runs.CANDIDATE_COLUMNS, r))
            for r in cur.params_of("insert into rsi.candidates")]
    assert [r["candidate_idx"] for r in rows] == [0, 1, 2]
    assert [r["parent_idx"] for r in rows] == [None, 0, 0]
    # The column the whole table exists for: what the search is mutating.
    assert [r["changed_components"] for r in rows] == [[], ["context"], ["plan"]]
    # And the one that says whether the prose is just getting longer.
    assert [r["context_chars"] for r in rows] == [len(SEED["context"]), len(GREW["context"]),
                                                  len(SEED["context"])]
    assert [r["accepted"] for r in rows] == [False, True, False]      # best_idx 1, gate said yes
    assert [round(r["valset_mean"], 4) for r in rows] == [0.45, 0.52, 0.30]
    assert [r["discovered_after_calls"] for r in rows] == [0, 12, 30]
    assert rows[1]["objectives"].adapted == {"skill": 0.52, "cost": 0.12}
    assert rows[1]["components"].adapted == GREW
    assert len(set(r["fingerprint"] for r in rows)) == 3


def test_the_scores_are_keyed_by_instance_and_not_by_position(runs, tmp_path):
    path = run_dir(tmp_path)
    cur = FakeCursor()
    manifest = json.loads((path / "manifest.json").read_text())
    runs.publish_search(cur, path, "gen12", TOPIC, manifest)
    scores = [dict(zip(runs.SCORE_COLUMNS, r))
              for r in cur.params_of("insert into rsi.candidate_scores")]
    assert {s["instance_id"] for s in scores} == set(VALSET)
    mine = [s for s in scores if s["candidate_idx"] == 1]
    assert sorted((s["instance_id"], s["score"]) for s in mine) == \
        sorted(zip(sorted(VALSET), [0.60, 0.44]))


def test_a_rejected_generation_accepts_nothing(runs, tmp_path):
    path = run_dir(tmp_path, accepted=False)
    cur = FakeCursor()
    manifest = json.loads((path / "manifest.json").read_text())
    runs.publish_search(cur, path, "gen12", TOPIC, manifest)
    rows = [dict(zip(runs.CANDIDATE_COLUMNS, r))
            for r in cur.params_of("insert into rsi.candidates")]
    assert [r["accepted"] for r in rows] == [False, False, False]


# ---------------------------------------------------------------------------
# 3. Nothing there is a notice, never a failure.

def test_no_gepa_directory_is_a_notice(runs, tmp_path, capsys):
    path = run_dir(tmp_path, gepa=False)
    cur = FakeCursor()
    manifest = json.loads((path / "manifest.json").read_text())
    assert runs.publish_search(cur, path, "gen12", TOPIC, manifest) == (0, 0)
    assert cur.calls == []                                  # not even a probe
    assert "no gepa/ directory" in capsys.readouterr().out


def test_an_unreadable_state_still_publishes_the_candidates(runs, tmp_path):
    """candidates.json is JSON beside the pickle. A state that will not load
    costs the scores and the ancestry, not the diff against the seed."""
    path = run_dir(tmp_path, state=False)
    (path / "gepa" / "gepa_state.bin").write_bytes(b"not a pickle")
    cur = FakeCursor()
    manifest = json.loads((path / "manifest.json").read_text())
    assert runs.publish_search(cur, path, "gen12", TOPIC, manifest) == (3, 0)
    rows = [dict(zip(runs.CANDIDATE_COLUMNS, r))
            for r in cur.params_of("insert into rsi.candidates")]
    assert [r["changed_components"] for r in rows] == [[], ["context"], ["plan"]]
    assert [r["valset_mean"] for r in rows] == [None, None, None]
    assert not any(sql.startswith("insert into rsi.candidate_scores") for sql in cur.statements)


def test_an_empty_gepa_directory_is_a_notice(runs, tmp_path, capsys):
    path = run_dir(tmp_path, state=False)
    (path / "gepa" / "candidates.json").unlink()
    cur = FakeCursor()
    manifest = json.loads((path / "manifest.json").read_text())
    assert runs.publish_search(cur, path, "gen12", TOPIC, manifest) == (0, 0)
    assert "no readable candidates" in capsys.readouterr().out


def test_without_010_the_search_is_a_notice_and_the_run_still_publishes(runs, tmp_path, capsys):
    path = run_dir(tmp_path)
    cur = FakeCursor(columns=PRE_010, tables=("candidates",))   # mid-migration counts as absent
    rollouts, traced, books, cands = runs.publish(cur, path, trading=False, searchable=None)
    assert (rollouts, traced, books, cands) == (1, 0, 0, 0)
    out = capsys.readouterr().out
    assert "010_record.sql" in out
    assert any(sql.startswith("insert into rsi.rollouts") for sql in cur.statements)
    assert not any(sql.startswith("insert into rsi.candidates") for sql in cur.statements)


# ---------------------------------------------------------------------------
# 4. The live publisher's three columns.

def test_a_live_row_carries_the_record_when_the_trader_attached_one():
    pub = load_script("publish_live")
    row = {"at": AT, "ticker": TICKER, "topic": TOPIC, "harness": "horizon-5m",
           "output": {"delta_cents": 1.0}, "mid_now": 0.32, "realised": 0.35,
           "scored": {"skill": 0.3}, "ok": True, "trade": TRADE}
    named = dict(zip(pub.BASE_COLUMNS + pub.EXTRA_COLUMNS, pub.live_row(row, pub.EXTRA_COLUMNS)))
    assert named["quote"].adapted == TRADE["quote"]
    assert named["fills"].adapted == TRADE["fills"]
    assert named["path"].adapted == TRADE["path_summary"]

    del row["trade"]
    named = dict(zip(pub.BASE_COLUMNS + pub.EXTRA_COLUMNS, pub.live_row(row, pub.EXTRA_COLUMNS)))
    assert (named["quote"], named["fills"], named["path"]) == (None, None, None)


def test_the_live_publisher_writes_only_the_columns_the_table_has():
    pub = load_script("publish_live")
    module_values: list[tuple[str, list]] = []

    def values(cur, sql, rows, **_kw):
        module_values.append((" ".join(sql.split()), list(rows)))

    pub.execute_values = values
    row = {"at": AT, "ticker": TICKER, "trade": TRADE}

    cur = FakeCursor(columns=LIVE_PRE_010)            # 008 applied, 010 not
    assert pub.publish(cur, [row]) == 1
    sql, (only,) = module_values[-1]
    assert "quote" not in sql
    assert len(only) == len(pub.BASE_COLUMNS) + len(pub.TOPIC_COLUMNS)

    cur = FakeCursor(columns=LIVE_PRE_010 + pub.RECORD_COLUMNS)      # both applied
    assert pub.publish(cur, [row]) == 1
    sql, (only,) = module_values[-1]
    assert "quote, fills, path" in sql
    assert len(only) == len(pub.BASE_COLUMNS) + len(pub.EXTRA_COLUMNS)


# ---------------------------------------------------------------------------
# 5. The two reading tools.

ROLLOUT_NAMES = ("id", "run_id", "side", "split", "topic", "unit", "fixture", "ticker", "at",
                 "mid_now", "realised", "predicted", "half_width", "err", "naive_error", "skill",
                 "echoed", "unmeasurable", "scored", "ok", "error_text", "cost_usd", "output",
                 "game", "feedback", "quote", "fills", "path")


def rollout_db_row(side: str, *, with_book: bool = True) -> tuple:
    return (1 if side == "baseline" else 2, "gen12", side, "holdout", TOPIC, "cents",
            "KXEPL-26SEP20ARSMCI", TICKER, AT, 0.32, 0.35, 0.33, 0.02, 0.02, 0.03, 0.33,
            False, False, True, True, None, 0.03, {"delta_cents": 1.0},
            {"league": "EPL"}, "scored. Book: open_long 5% -> +120 USD (quote)",
            TRADE["quote"] if with_book else None,
            TRADE["fills"] if with_book else None,
            TRADE["path_summary"] if with_book else None)


TRADE_NAMES = ("topic", "book_id", "instance_id", "side", "instrument", "opened_at",
               "closed_at", "entry_px", "exit_px", "qty", "size_usd", "fees_usd", "pnl_usd",
               "reason", "source")
TRADE_ROW = (TOPIC, "gen12:baseline:holdout", INSTANCE, "long", TICKER, AT,
             "2026-09-20T15:05:00+00:00", 0.3, 0.35, 66666.0, 20000.0, 7.4, 120.0,
             "horizon", "harness")

SPANS = [{"name": "kalshi.game_state", "kind": "tool", "depth": 1, "status": "ok",
          "duration_s": 0.3, "cost_usd": 0.0, "output": "0-0, 12'"},
         {"name": "answer", "kind": "llm", "depth": 0, "status": "ok", "duration_s": 4.1,
          "cost_usd": 0.027, "input": "You forecast the short-term path…"}]


def test_show_window_prints_every_section_of_one_decision(capsys):
    show = load_script("show_window")
    cur = FakeCursor(rows={
        "from rsi.rollouts": (ROLLOUT_NAMES, [rollout_db_row("baseline"),
                                              rollout_db_row("candidate")]),
        "from rsi.traces": (("spans",), [(SPANS,)]),
        "from rsi.trades": (TRADE_NAMES, [TRADE_ROW])})
    rows = show.window_rows(cur, "gen12", INSTANCE)
    assert [r["side"] for r in rows] == ["baseline", "candidate"]
    assert cur.params_of("select * from rsi.rollouts") == ("gen12", TICKER, AT)
    assert cur.params_of("select * from rsi.trades") == (INSTANCE,)
    # The trade belongs to the baseline's book alone, by its book_id.
    assert [len(r["trades"]) for r in rows] == [1, 0]

    show.print_window("gen12", INSTANCE, rows)
    out = capsys.readouterr().out
    for expected in ("window  gen12", "baseline / holdout", "candidate / holdout",
                     "forecast", "skill", "quote", "path", "fills", "trade", "feedback", "trace",
                     "crossed bid yes", "kalshi.game_state", "0.3 / 0.34",
                     "pnl $120.00", "no position was opened on this window"):
        assert expected in out, expected


def test_show_window_says_so_when_no_book_replayed_the_window(capsys):
    show = load_script("show_window")
    cur = FakeCursor(tables=("candidates",),          # 009 not applied either
                     rows={"from rsi.rollouts": (ROLLOUT_NAMES,
                                                 [rollout_db_row("baseline", with_book=False)]),
                           "from rsi.traces": (("spans",), [])})
    show.print_window("gen12", INSTANCE, show.window_rows(cur, "gen12", INSTANCE))
    out = capsys.readouterr().out
    assert "no paper book replayed this window" in out
    assert "not traced" in out
    # A missing table and an untraded window are different answers.
    assert "rsi.trades is not in this database" in out


def test_show_window_reads_the_live_table_too(capsys):
    show = load_script("show_window")
    names = ("at", "ticker", "topic", "unit", "harness", "output", "mid_now", "realised",
             "skill", "scored", "ok", "error_text", "spans", "quote", "fills", "path")
    cur = FakeCursor(rows={"from rsi.live_forecasts": (
        names, [(AT, TICKER, TOPIC, "cents", "horizon-5m", {"delta_cents": 1.0}, 0.32, 0.35,
                 0.3, True, True, None, SPANS, TRADE["quote"], TRADE["fills"],
                 TRADE["path_summary"])]),
        "from rsi.trades": (TRADE_NAMES, [(TOPIC, f"live:{TOPIC}", INSTANCE, "long", TICKER, AT,
                                           None, 0.3, None, 66666.0, 20000.0, 7.4, None,
                                           None, "harness")])})
    rows = show.live_rows(cur, TICKER, AT)
    show.print_live(TICKER, AT, rows)
    out = capsys.readouterr().out
    assert "live    " + TICKER in out
    for expected in ("horizon-5m", "quote", "fills", "trade", "trace",
                     "crossed bid yes, ask yes", "pnl $-"):
        assert expected in out, expected


def test_an_instance_id_is_split_at_the_last_at_sign():
    show = load_script("show_window")
    assert show.split_instance(INSTANCE) == (TICKER, AT)
    with pytest.raises(SystemExit):
        show.split_instance("no-at-sign")


CANDIDATE_DB_ROWS = [
    (TOPIC, 0, None, [], False, 0.4500, 5470, 0, "aaaaaaaaaaaa", {"skill": 0.45}),
    (TOPIC, 1, 0, ["context"], False, 0.4600, 6100, 12, "bbbbbbbbbbbb", {"skill": 0.46}),
    (TOPIC, 2, 0, ["context"], False, 0.4550, 7200, 40, "cccccccccccc", {"skill": 0.45}),
    (TOPIC, 3, 2, ["context"], False, 0.4610, 8757, 88, "dddddddddddd", {"skill": 0.46}),
    (TOPIC, 4, 2, ["plan"], False, 0.4400, 7200, 120, "eeeeeeeeeeee", {"skill": 0.44}),
]


def test_show_search_prints_the_table_and_the_line_that_catches_the_stall(capsys):
    show = load_script("show_search")
    cur = FakeCursor(rows={"from rsi.candidates": (show.COLUMNS, CANDIDATE_DB_ROWS)})
    rows = show.candidate_rows(cur, "gen12")
    assert cur.params_of("select topic") == ("gen12",)
    assert len(rows) == 5

    print(show.render("gen12", rows), end="")
    out = capsys.readouterr().out
    assert f"search  gen12  {TOPIC}  5 candidates" in out
    assert "(seed)" in out and "context" in out and "plan" in out
    assert "5,470" in out and "8,757" in out
    # The one line that would have been read six generations ago.
    assert "3 of 5 candidates changed context only; also plan (1); " \
           "context grew 5,470 -> 8,757 chars" in out


def test_show_search_summarises_a_search_that_changed_nothing():
    show = load_script("show_search")
    seed_only = [(TOPIC, 0, None, [], False, 0.45, 5470, 0, "aaaaaaaaaaaa", None)]
    rows = [dict(zip(show.COLUMNS, r)) for r in seed_only]
    assert show.summarise(rows) == \
        "1 candidates, none of which differs from the seed; context stayed 5,470 chars"
    assert "no candidates published" in show.summarise([])


def test_show_search_can_be_narrowed_to_one_topic():
    show = load_script("show_search")
    cur = FakeCursor(rows={"from rsi.candidates": (show.COLUMNS, CANDIDATE_DB_ROWS[:1])})
    show.candidate_rows(cur, "gen6", "crypto-horizon-1m")
    sql, params = cur.calls[-1]
    assert "and topic = %s" in sql and params == ("gen6", "crypto-horizon-1m")
