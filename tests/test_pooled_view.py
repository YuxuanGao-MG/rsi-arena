"""Hold `rsi.run_side_stats` and `web/stats.js::pooled` to the same answers.

The overview's headline number is the pooled skill of a generation's held-out
rollouts. It is computed twice, in two languages, and that is deliberate: the
browser recomputes it from the rollouts so a reader can check it, and the
database sums it so the browser does not have to download 87,380 rows to do so.
Two implementations of one statistic drift, and the drift is invisible - both
sides keep answering a number, just not the same number.

So this runs the migration's own SELECT, lifted from the file rather than
retyped, over rows built to hit every branch, and compares it with a transcript
of the JavaScript. Postgres is not available offline, so the SQL runs on SQLite
with `greatest` supplied as `max`; what is being tested is the arithmetic and
the predicates, which are the parts that drift. The shape of the SQL - one row
per (topic, run_id, side, split), those column names, that grouping - is what
`web/generations.js` depends on, so it is asserted too.

The tick is the thing this test exists for. Migration 011 floored every
basis-point row at 5, the news topic's tick, while crypto's is 2; nothing failed
and every number it produced for crypto was quietly a little generous. 012 reads
the tick from the row's topic, as `web/topics.js::topicOf` does, and this is what
says the two still agree.
"""

from __future__ import annotations

import random
import re
import sqlite3
from pathlib import Path

import pytest

MIGRATION = Path(__file__).resolve().parent.parent / "supabase" / "migrations" / \
    "012_run_side_stats_materialised.sql"

#: Booleans stay booleans: SQLite stores `True` as 1 and Postgres as `true`,
#: while `web/stats.js` refuses a row on `r.ok === false` exactly. A fixture
#: written as `ok=0` passes the SQL branch and fails the JavaScript one, and the
#: test would then be comparing two different questions.

#: Each topic's tick, as `web/topics.js` declares it. Restated here because a
#: Python test cannot import a JavaScript module; `test_ticks_match_the_reader`
#: reads the JavaScript and asserts these are still its numbers, so the
#: restatement cannot go stale silently.
TICKS = {"kalshi-horizon-5m": 0.01, "news-equity-5m": 5.0, "crypto-horizon-1m": 2.0}


# ---------------------------------------------------------------------------
# The two implementations.

def view_sql() -> str:
    """The migration's SELECT, translated for SQLite.

    Lifted from the migration so the test cannot pass against SQL the database
    does not have. Two translations, neither touching a predicate or a sum:
    `greatest` is `max` in SQLite, and the source table is a temporary one.
    """
    text = MIGRATION.read_text()
    body = text.split("create materialized view rsi.run_side_stats as", 1)[1]
    body = body.split(";", 1)[0]
    body = body.replace("greatest(", "max(").replace("rsi.rollouts", "rollouts")
    return body.strip()


def pooled_in_javascript(rows: list[dict]) -> dict:
    """`web/stats.js::pooled`, transcribed statement for statement.

        const refused = r => r.ok === false || r.scored === false;
        if (refused(r)) { refusals += 1; continue; }
        if (r.err == null || r.naive_error == null) continue;
        scored += 1;
        const tick = tickOf(r);
        if (r.naive_error < tick / 100) quiet += 1;
        removed += r.naive_error - r.err;
        benchmark += Math.max(r.naive_error, tick);
    """
    removed = benchmark = 0.0
    scored = quiet = refusals = 0
    for r in rows:
        if r.get("ok") is False or r.get("scored") is False:
            refusals += 1
            continue
        if r.get("err") is None or r.get("naive_error") is None:
            continue
        scored += 1
        tick = TICKS.get(r.get("topic"), 2.0 if r.get("unit") == "bps" else 0.01)
        if r["naive_error"] < tick / 100:
            quiet += 1
        removed += r["naive_error"] - r["err"]
        benchmark += max(r["naive_error"], tick)
    return {"rows_total": len(rows), "scored": scored, "refusals": refusals,
            "quiet": quiet, "removed": removed, "benchmark": benchmark,
            "skill": removed / benchmark if benchmark else None}


def pooled_in_sql(rows: list[dict]) -> dict[tuple, dict]:
    """The migration's view, per group, over these rows."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""create table rollouts (topic text, run_id text, side text, split text,
                                         err real, naive_error real, unit text,
                                         ok int, scored int)""")
    db.executemany("insert into rollouts values (?,?,?,?,?,?,?,?,?)",
                   [(r.get("topic"), r["run_id"], r["side"], r["split"],
                     r.get("err"), r.get("naive_error"), r.get("unit"),
                     r.get("ok"), r.get("scored")) for r in rows])
    out = {}
    for row in db.execute(view_sql()):
        d = dict(row)
        key = (d.pop("topic"), d.pop("run_id"), d.pop("side"), d.pop("split"))
        d["skill"] = d["removed"] / d["benchmark"] if d["benchmark"] else None
        out[key] = d
    db.close()
    return out


def agree(rows: list[dict]) -> None:
    """Assert both implementations say the same thing about every group."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("topic"), r["run_id"], r["side"], r["split"]), []).append(r)
    from_sql = pooled_in_sql(rows)
    assert set(from_sql) == set(groups), "the view grouped differently than the reader does"
    for key, mine in groups.items():
        want, got = pooled_in_javascript(mine), from_sql[key]
        for field in ("rows_total", "scored", "refusals", "quiet"):
            assert got[field] == want[field], f"{key} {field}: SQL {got[field]} vs JS {want[field]}"
        for field in ("removed", "benchmark"):
            assert got[field] == pytest.approx(want[field], abs=1e-9), \
                f"{key} {field}: SQL {got[field]} vs JS {want[field]}"
        if want["skill"] is None:
            assert got["skill"] is None
        else:
            assert got["skill"] == pytest.approx(want["skill"], abs=1e-12)


# ---------------------------------------------------------------------------

def row(**kw) -> dict:
    base = {"topic": "kalshi-horizon-5m", "run_id": "gen1", "side": "baseline",
            "split": "holdout", "err": 1.0, "naive_error": 2.0, "unit": "cents",
            "ok": True, "scored": True}
    base.update(kw)
    return base


def test_the_view_exists_and_is_materialised():
    """A plain view would recompute per request, which is the bug 012 fixed."""
    text = MIGRATION.read_text()
    assert "create materialized view rsi.run_side_stats" in text
    assert "create unique index run_side_stats_key" in text, \
        "concurrent refresh needs a unique index, or publish_runs blocks readers"


def test_columns_are_the_ones_the_reader_selects():
    """`web/generations.js` names these; a rename here is a 404 there."""
    asked = re.search(r"run_side_stats\?select=([\w,]+)",
                      (Path(__file__).resolve().parent.parent / "web" /
                       "generations.js").read_text())
    assert asked, "the overview no longer selects from run_side_stats"
    produced = set(pooled_in_sql([row()]).popitem()[1]) | {"topic", "run_id", "side", "split"}
    for column in asked.group(1).split(","):
        assert column in produced, f"the reader selects {column}; the view has no such column"


def test_ticks_match_the_reader():
    """The ticks restated in this file and in the SQL are still the reader's."""
    js = (Path(__file__).resolve().parent.parent / "web" / "topics.js").read_text()
    for topic, tick in TICKS.items():
        block = re.search(rf'"{re.escape(topic)}"\s*:\s*\{{.*?tick:\s*([\d.]+)', js, re.S)
        assert block, f"{topic} is no longer in web/topics.js"
        assert float(block.group(1)) == tick, \
            f"{topic}'s tick is {block.group(1)} in the reader and {tick} here"
        assert str(tick) in MIGRATION.read_text(), f"{topic}'s tick is not in the migration"


def test_plain_agreement():
    agree([row(err=0.5, naive_error=3.0), row(err=2.0, naive_error=1.0)])


def test_refusals_are_excluded_and_counted():
    rows = [row(), row(ok=False, err=9.0, naive_error=9.0), row(scored=False, err=9.0)]
    agree(rows)
    got = pooled_in_sql(rows).popitem()[1]
    assert got["refusals"] == 2 and got["scored"] == 1
    assert got["removed"] == pytest.approx(1.0), "a refusal reached the sum"


def test_nulls_are_unscored_not_refusals():
    rows = [row(), row(err=None), row(naive_error=None)]
    got = pooled_in_sql(rows).popitem()[1]
    assert (got["rows_total"], got["scored"], got["refusals"]) == (3, 1, 0)
    agree(rows)


def test_missing_ok_and_scored_count_as_forecasts():
    """`coalesce(ok, true)` and `r.ok === false`: a null is not a refusal."""
    rows = [row(ok=None, scored=None)]
    assert pooled_in_sql(rows).popitem()[1]["scored"] == 1
    agree(rows)


def test_the_floor_is_the_topics_own_tick():
    """The bug 011 carried: crypto floored at 5 instead of 2."""
    quiet_bps = row(topic="crypto-horizon-1m", unit="bps", err=0.5, naive_error=1.0)
    got = pooled_in_sql([quiet_bps]).popitem()[1]
    assert got["benchmark"] == pytest.approx(2.0), \
        "a quiet crypto window is floored at crypto's tick, not at news'"
    assert pooled_in_sql([row(topic="news-equity-5m", unit="bps", err=0.5, naive_error=1.0)]
                         ).popitem()[1]["benchmark"] == pytest.approx(5.0)
    agree([quiet_bps, row(topic="news-equity-5m", unit="bps", err=1.0, naive_error=7.0)])


def test_a_row_without_a_topic_falls_back_on_its_unit():
    """As `topicOf` does: bps without a topic is crypto, anything else Kalshi."""
    agree([row(topic=None, unit="bps", err=1.0, naive_error=1.0),
           row(topic=None, unit="cents", err=0.001, naive_error=0.001)])


def test_quiet_is_a_hundredth_of_a_tick():
    rows = [row(naive_error=0.00009, err=0.0), row(naive_error=0.02, err=0.0),
            row(topic="crypto-horizon-1m", unit="bps", naive_error=0.019, err=0.0),
            row(topic="crypto-horizon-1m", unit="bps", naive_error=0.021, err=0.0)]
    agree(rows)
    kalshi = pooled_in_sql(rows)[("kalshi-horizon-5m", "gen1", "baseline", "holdout")]
    crypto = pooled_in_sql(rows)[("crypto-horizon-1m", "gen1", "baseline", "holdout")]
    assert kalshi["quiet"] == 1 and crypto["quiet"] == 1


def test_a_group_with_nothing_scored_has_no_skill():
    rows = [row(ok=False), row(scored=False)]
    assert pooled_in_sql(rows).popitem()[1]["skill"] is None
    agree(rows)


def test_sides_splits_and_topics_do_not_mix():
    rows = [row(side="baseline"), row(side="candidate", err=0.0),
            row(split="train", err=0.0), row(run_id="gen2", err=0.0),
            row(topic="news-equity-5m", unit="bps", err=1.0, naive_error=6.0)]
    got = pooled_in_sql(rows)
    assert len(got) == 5, "two groups were pooled together"
    agree(rows)


def test_agreement_on_many_random_rows():
    """The branches in combination, which is where a mismatch actually hides."""
    rnd = random.Random(20260930)
    rows = []
    for _ in range(800):
        topic = rnd.choice([*TICKS, None])
        unit = "cents" if topic == "kalshi-horizon-5m" else rnd.choice(["bps", "cents"])
        naive = rnd.choice([0.0, 1e-5, 0.005, 0.5, 3.0, 12.0, 40.0])
        rows.append(row(
            topic=topic, unit=unit,
            run_id=f"gen{rnd.randint(1, 4)}", side=rnd.choice(["baseline", "candidate"]),
            split=rnd.choice(["train", "holdout"]),
            naive_error=naive,
            err=rnd.choice([None, 0.0, naive, naive * rnd.random(), naive * 2]),
            ok=rnd.choice([True, True, True, False, None]),
            scored=rnd.choice([True, True, True, False, None])))
    agree(rows)
