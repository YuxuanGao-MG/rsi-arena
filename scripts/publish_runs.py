"""Push run directories into Supabase, so a person can read what the agent did.

A run directory is the record, and it is JSON on someone's laptop. This copies
it somewhere a browser can reach: one row per generation, one per scored window,
and — when the rollouts were written with ``--trace`` — one per trajectory. Since
migration 010 it also copies the search: one row per candidate GEPA proposed and
one per (candidate, instance) score, which is the only place the losers are
queryable.

Traces are stored separately and trimmed. A single window's trace is about
28 KB, most of it forty-five minute bars repeated in the tool answer and again
in the prompt; five thousand windows would be well over a hundred megabytes of
candles nobody reads twice. What a reader wants from a trace is which tools were
called, what came back in summary, and the prompt and answer of the model call
that ended it.

    python scripts/publish_runs.py runs/gen1-floored
    python scripts/publish_runs.py runs/*             # everything
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:                                   # pragma: no cover
    sys.exit("pip install psycopg2-binary")

from rsi_arena.loop.archive import (from_gepa_state, gepa_candidates,     # noqa: E402
                                    gepa_parents, gepa_state, seed_diff)
from rsi_arena.loop.generation import fingerprint_components, qualified     # noqa: E402,F401
from rsi_arena.topics import TOPICS                   # noqa: E402

# The paper books a generation's rollouts traded, published with them. The
# tables arrive with migration 009; ``has_trading_tables`` says whether this
# database has them yet.
from publish_trading import NOT_APPLIED, has_trading_tables, publish_book_file   # noqa: E402

# How the valset order is recovered: ``valset.json`` for a run that wrote one,
# and the train rollouts in their own order for the three that predate it. The
# subscore matrix is positional against it and joins to nothing without it.
from backfill_archive import valset_ids                                         # noqa: E402

#: Tool answers are the bulk of a trace and the least re-read part of it.
TRIM = 1200

#: Every column a rollout row can carry, in insert order. ``topic`` and ``unit``
#: arrive with migration 008 and the last three with 010; ``columns_of`` says
#: which of these the database has, so the same publisher works on either side
#: of both.
COLUMNS = ("run_id", "side", "split", "fixture", "ticker", "at", "mid_now", "realised",
           "predicted", "half_width", "err", "naive_error", "skill", "echoed", "unmeasurable",
           "scored", "cost_usd", "ok", "error_text", "output", "game", "feedback",
           "topic", "unit", "quote", "fills", "path")
#: What the table had before any topic but the first existed.
BASE_COLUMNS = COLUMNS[:22]


def columns_of(cur, table: str = "rollouts") -> set[str]:
    """The columns ``rsi.<table>`` actually has, or the base set if it will not say."""
    try:
        cur.execute("select column_name from information_schema.columns "
                    "where table_schema = 'rsi' and table_name = %s", (table,))
        found = {r[0] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001 - a probe that fails is the old schema
        found = set()
    return found or set(BASE_COLUMNS)


def unit_of(topic: str) -> str:
    """The unit a topic's moves are in, from the spec, or Kalshi's for a topic
    this checkout does not know (an old run published from a newer schema)."""
    spec = TOPICS.get(topic)
    return spec.unit if spec is not None else "cents"


def env_from(path: Path) -> dict[str, str]:
    """Read a .env without letting the shell parse it. Values carry `&` and `#`."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def db_url(explicit: str) -> str:
    if explicit:
        return explicit
    for key in ("SUPABASE_DB_URL", "DATABASE_URL"):
        if os.environ.get(key):
            return os.environ[key]
    for candidate in (Path.home() / "Desktop/OctoMesh/openmesh-arena/.env",):
        if candidate.exists():
            found = env_from(candidate).get("SUPABASE_DB_URL")
            if found:
                return found
    sys.exit("no SUPABASE_DB_URL; pass --db-url or set it in the environment")


def trim_span(span: dict[str, Any]) -> dict[str, Any]:
    """One step, small enough to keep five thousand of."""
    def short(value: Any) -> Any:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return text if len(text) <= TRIM else text[:TRIM] + f"… [{len(text) - TRIM} more]"

    return {"name": span.get("name"), "kind": span.get("kind"),
            "depth": span.get("depth"), "status": span.get("status"),
            "duration_s": span.get("duration_s"), "cost_usd": span.get("cost_usd"),
            "cached": span.get("cached"), "error": span.get("error"),
            "input": short(span.get("input")) if span.get("input") is not None else None,
            "output": short(span.get("output")) if span.get("output") is not None else None}


def forecast(run: dict, details: dict) -> dict:
    """What the harness said, from the run if it was kept and the score if not.

    Runs written before the answer was kept in the summary have only the graded
    numbers. A delta is the predicted price less the mid, and a half width is
    half the quote — both recoverable, and a reader looking at an old generation
    should see a forecast rather than a dash.
    """
    said = run.get("output")
    if isinstance(said, dict) and said:
        return said
    mid, predicted = details.get("mid_now"), details.get("predicted")
    if mid is None or predicted is None:
        return {}
    half = details.get("half_width")
    return {"delta_cents": round((predicted - mid) * 100, 1),
            "half_width_cents": None if half is None else round(half * 100, 1),
            "reconstructed": True}


def _json_or_none(value: Any) -> Any:
    """``Json(value)`` for anything the record actually carried, else None."""
    return None if value is None else Json(value)


def rollout_row(run_id: str, side: str, split: str, r: dict, *,
                topic: str = "", unit: str = "") -> dict[str, Any]:
    """One rollout as a column-keyed row. ``COLUMNS`` orders it for the insert."""
    inst, out = r["instance"], r["outcome"]
    d, run = out.get("details", {}), (r.get("run") or {})
    # The instance's own context: a match's game state for Kalshi, whatever
    # the instant looked like for a topic that calls it something else.
    context = inst.get("game") or inst.get("context")
    # What the paper book recorded of this window, when a book replayed it.
    # Three columns rather than one blob: "which quotes did the tape never
    # reach" and "how wide was this harness quoting" are the two questions the
    # book's first finding raised, and neither should need a jsonb path through
    # a record to ask. A topic that does not trade, and a run published before
    # its books existed, have no record and leave all three null.
    trade = d.get("trade") if isinstance(d.get("trade"), dict) else {}
    return {
        "run_id": run_id, "side": side, "split": split,
        # The fixture is the unit the split respects, so it is a column rather
        # than something a reader has to parse out of a ticker. Instances say
        # it outright now; older dumps are read the way they always were.
        "fixture": (inst.get("group")
                    or (inst.get("game") or {}).get("game_id")
                    or str(inst.get("ticker") or inst.get("symbol") or "").rsplit("-", 1)[0]),
        # A Kalshi instance is a ticker; a crypto or news instance is a symbol.
        # The column is what the reader keys a window on either way.
        "ticker": inst.get("ticker") or inst.get("symbol") or inst.get("id"), "at": inst["at"],
        "mid_now": d.get("mid_now"), "realised": d.get("realised"),
        "predicted": d.get("predicted"), "half_width": d.get("half_width"),
        "err": d.get("error"), "naive_error": d.get("naive_error"), "skill": d.get("skill"),
        "echoed": d.get("echoed"), "unmeasurable": d.get("unmeasurable"), "scored": d.get("scored"),
        "cost_usd": r.get("cost_usd"), "ok": run.get("ok"), "error_text": run.get("error"),
        "output": Json(forecast(run, d)), "game": Json(context), "feedback": out.get("feedback"),
        "topic": topic, "unit": unit or d.get("unit") or "",
        "quote": _json_or_none(trade.get("quote")),
        # An empty array is a quote nobody wanted, which is a finding; null is a
        # window that never posted one, which is a different finding.
        "fills": _json_or_none(trade.get("fills")),
        "path": _json_or_none(trade.get("path_summary")),
    }


def rollout_rows(run_id: str, side: str, split: str, path: Path, *,
                 topic: str = "", unit: str = "") -> list[tuple[dict[str, Any], list | None]]:
    """``(row, spans)`` per window. ``spans`` is None unless it was traced."""
    if not path.exists():
        return []
    rows: list[tuple[dict[str, Any], list | None]] = []
    for r in json.loads(path.read_text()):
        spans = ((r.get("run") or {}).get("trace")) or None
        rows.append((rollout_row(run_id, side, split, r, topic=topic, unit=unit),
                     [trim_span(x) for x in spans] if spans else None))
    return rows


def book_files(run_dir: Path) -> list[Path]:
    """The replay books a run wrote: ``<run_dir>/books/<side>.<split>.json``."""
    return sorted((run_dir / "books").glob("*.json"))


def publish_books(cur, run_dir: Path, topic: str, *, trading: bool | None = None) -> int:
    """Every book file under the run, each replaced whole. Returns how many.

    ``trading`` is whether the database has 009's tables; ``None`` probes. A
    database without them is not an error - the books stay in the run
    directory, which is committed, and a re-publish after the migration lands
    them - but it is said, once per run, so a reader missing its curves knows
    which migration to apply.
    """
    files = book_files(run_dir)
    if not files:
        return 0
    if trading is None:
        trading = has_trading_tables(cur)
    if not trading:
        print(f"  note  {run_dir.name}: {len(files)} paper books not published; {NOT_APPLIED}")
        return 0
    for path in files:
        publish_book_file(cur, path, topic=topic)
    return len(files)


# ---------------------------------------------------------------------------
# The search: every candidate, and what each scored on what.

#: What migration 010 creates for the search. Both or neither: a database with
#: one of them is mid-migration and gets the same notice as one with none.
CANDIDATE_TABLES = ("candidates", "candidate_scores")
NO_CANDIDATES = ("rsi.candidates / rsi.candidate_scores are not in this database; "
                 "apply supabase/migrations/010_record.sql. The search was not published.")

CANDIDATE_COLUMNS = ("topic", "run_id", "candidate_idx", "fingerprint", "parent_idx",
                     "changed_components", "accepted", "valset_mean", "objectives",
                     "components", "context_chars", "discovered_after_calls")
SCORE_COLUMNS = ("topic", "run_id", "candidate_idx", "instance_id", "score")


def has_candidate_tables(cur) -> bool:
    """Whether migration 010's two search tables are in this database."""
    try:
        cur.execute("select table_name from information_schema.tables "
                    "where table_schema = 'rsi' "
                    "and table_name in ('candidates', 'candidate_scores')")
        found = {r[0] for r in cur.fetchall()}
    except Exception:  # noqa: BLE001 - a probe that fails is a database without them
        return False
    return found >= set(CANDIDATE_TABLES)


def search_model(run_dir: Path, manifest: dict) -> str | None:
    """The model a candidate that does not name one runs on.

    The model is a component, so a candidate that rewrote it carries its own and
    the fingerprint must use that; one that did not inherits the generation's.
    Read out of ``best.json`` as JSON rather than through ``Harness.load``: a
    harness written by an older schema may no longer validate, and a publisher
    must not lose a generation's search over a field it is not reading.
    """
    named = (manifest.get("settings") or {}).get("model")
    if named:
        return str(named)
    best = run_dir / "best.json"
    if not best.exists():
        return None
    try:
        return (json.loads(best.read_text()).get("config") or {}).get("model")
    except (ValueError, OSError):
        return None


def search_rows(run_dir: Path, run_id: str, topic: str,
                manifest: dict) -> tuple[list[dict[str, Any]], list[tuple]]:
    """``(candidate rows, score rows)`` for one generation's finished search.

    Read the way ``loop/archive.py`` reads it, through the same helpers: the
    component texts from ``gepa/candidates.json``, the per-instance matrix and
    the ancestry from the state, the valset order from ``valset.json``. Empty
    when there is nothing readable there, which is not an error.
    """
    components = gepa_candidates(run_dir)
    model = search_model(run_dir, manifest)
    entries = from_gepa_state(run_dir, run_dir.name, valset_ids(run_dir),
                              lambda c, m=model: fingerprint_components(c, c.get("model") or m))
    if not components:
        # A run whose candidates.json is missing but whose state loads, which is
        # every generation GEPA wrote before it kept the JSON beside the pickle.
        components = [dict(e.components) for e in entries]
    if not components:
        return [], []

    state = gepa_state(run_dir) or {}
    objectives = state.get("prog_candidate_objective_scores") or []
    parents = gepa_parents(state)
    # Candidate 0 is the seed GEPA started from, which is the archive candidate
    # this generation chose to mine and not necessarily the incumbent.
    seed = components[0]
    search = manifest.get("search") or {}
    best_idx = search.get("best_idx")
    promoted = bool((manifest.get("decision") or {}).get("accepted"))

    rows: list[dict[str, Any]] = []
    scores: list[tuple] = []
    for k, comp in enumerate(components):
        entry = entries[k] if k < len(entries) else None
        subscores = entry.scores if entry else {}
        objective = objectives[k] if k < len(objectives) else None
        rows.append({
            "topic": topic, "run_id": run_id, "candidate_idx": k,
            "fingerprint": fingerprint_components(comp, comp.get("model") or model),
            "parent_idx": parents[k] if k < len(parents) else None,
            "changed_components": seed_diff(comp, seed),
            # Not "won its minibatch" - every filed candidate did that, which is
            # why the column would say nothing. This is the one the generation
            # promoted: GEPA's best and the gate's yes, by index, because the
            # promoted harness's fingerprint is taken over re-serialised
            # components and does not always equal the candidate's.
            "accepted": promoted and k == best_idx,
            "valset_mean": (round(sum(subscores.values()) / len(subscores), 6)
                            if subscores else None),
            "objectives": Json(objective) if isinstance(objective, dict) else None,
            "components": Json(comp),
            "context_chars": len(comp.get("context") or ""),
            "discovered_after_calls": entry.discovered_after_calls if entry else None,
        })
        scores += [(topic, run_id, k, instance, score)
                   for instance, score in sorted(subscores.items())]
    return rows, scores


def publish_search(cur, run_dir: Path, run_id: str, topic: str, manifest: dict, *,
                   searchable: bool | None = None) -> tuple[int, int]:
    """The candidates a generation's search proposed, and their scores. Upserted.

    ``searchable`` is whether the database has 010's tables; ``None`` probes.
    Nothing here may fail a publish. A run with no ``gepa/`` searched nothing or
    did not keep it, a state that will not unpickle is somebody else's schema
    changing under us, and a database without the tables is a migration someone
    has not applied yet - each is a notice, and the rollouts still land.
    """
    if not (run_dir / "gepa").is_dir():
        print(f"  note  {run_dir.name}: no gepa/ directory; no search to publish")
        return 0, 0
    if searchable is None:
        searchable = has_candidate_tables(cur)
    if not searchable:
        print(f"  note  {run_dir.name}: {NO_CANDIDATES}")
        return 0, 0
    rows, scores = search_rows(run_dir, run_id, topic, manifest)
    if not rows:
        print(f"  note  {run_dir.name}: gepa/ holds no readable candidates; "
              f"the search was not published")
        return 0, 0

    updates = ", ".join(f"{c} = excluded.{c}" for c in CANDIDATE_COLUMNS
                        if c not in ("topic", "run_id", "candidate_idx"))
    execute_values(cur, f"""
        insert into rsi.candidates ({", ".join(CANDIDATE_COLUMNS)})
        values %s
        on conflict (topic, run_id, candidate_idx) do update set {updates}
    """, [tuple(row[c] for c in CANDIDATE_COLUMNS) for row in rows])
    if scores:
        execute_values(cur, f"""
            insert into rsi.candidate_scores ({", ".join(SCORE_COLUMNS)})
            values %s
            on conflict (topic, run_id, candidate_idx, instance_id)
              do update set score = excluded.score
        """, scores)
    return len(rows), len(scores)


def publish(cur, run_dir: Path, *, trading: bool | None = None,
            searchable: bool | None = None) -> tuple[int, int, int, int]:
    """One run: its row, its rollouts and traces, its paper books, its search.

    Returns (rollouts, traced, books, candidates).
    """
    manifest = json.loads((run_dir / "manifest.json").read_text())
    run_id = qualified(run_dir, manifest["topic"])
    cur.execute("""
        insert into rsi.runs (id, topic, created, parent, incumbent, incumbent_fp,
                              candidate_fp, accepted, reasons, baseline, candidate,
                              decision, search, llm, split)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (id) do update set
            topic = excluded.topic, parent = excluded.parent,
            accepted = excluded.accepted, reasons = excluded.reasons,
            baseline = excluded.baseline, candidate = excluded.candidate,
            decision = excluded.decision, search = excluded.search,
            llm = excluded.llm, split = excluded.split
    """, (run_id, manifest["topic"], manifest.get("created"),
          qualified(manifest.get("parent"), manifest["topic"]),
          manifest.get("incumbent"), manifest.get("incumbent_fingerprint"),
          manifest.get("candidate_fingerprint"),
          bool(manifest.get("decision", {}).get("accepted")),
          manifest.get("decision", {}).get("reasons") or [],
          Json(manifest.get("baseline")), Json(manifest.get("candidate")),
          Json(manifest.get("decision")), Json(manifest.get("search")),
          Json(manifest.get("llm")), Json(manifest.get("split"))))

    topic = manifest.get("topic") or ""
    rollouts, traced = publish_rollouts(cur, run_id, run_dir, topic)
    books = publish_books(cur, run_dir, topic, trading=trading)
    candidates, _scores = publish_search(cur, run_dir, run_id, topic, manifest,
                                         searchable=searchable)
    return rollouts, traced, books, candidates


def publish_rollouts(cur, run_id: str, run_dir: Path, topic: str) -> tuple[int, int]:
    """The run's rollouts, replaced whole, and the traces of those that have one."""
    rows: list[tuple[dict[str, Any], list | None]] = []
    for side in ("baseline", "candidate"):
        for split in ("train", "holdout"):
            rows += rollout_rows(run_id, side, split,
                                 run_dir / "rollouts" / f"{side}.{split}.json",
                                 topic=topic, unit=unit_of(topic))
    if not rows:
        return 0, 0

    # Re-publishing a run replaces its rollouts rather than merging, so a record
    # is never half one run and half another.
    cur.execute("delete from rsi.rollouts where run_id = %s", (run_id,))
    # Only the columns the database has. ``topic`` and ``unit`` land with
    # migration 008, and a publisher that named them before then would fail
    # every run on the way to a reader that had not changed yet.
    have = columns_of(cur)
    cols = [c for c in COLUMNS if c in have]
    # Returning the ids in insert order lets the traces be matched back without
    # a second query keyed on a composite that timestamps make fragile.
    ids = execute_values(cur, f"""
        insert into rsi.rollouts ({", ".join(cols)})
        values %s returning id
    """, [tuple(row[c] for c in cols) for row, _ in rows], fetch=True)

    traces = [(rid[0], Json(spans)) for rid, (_, spans) in zip(ids, rows) if spans]
    if traces:
        execute_values(cur, "insert into rsi.traces (rollout_id, spans) values %s "
                            "on conflict (rollout_id) do update set spans = excluded.spans",
                       traces)
    return len(rows), len(traces)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--db-url", default="")
    args = ap.parse_args()

    conn = psycopg2.connect(db_url(args.db_url), connect_timeout=30)
    conn.autocommit = False
    total = books_total = cands_total = 0
    with conn, conn.cursor() as cur:
        # Probed once, not per run: the answer does not change mid-invocation.
        trading = has_trading_tables(cur)
        if not trading:
            print(f"  note  {NOT_APPLIED}")
        searchable = has_candidate_tables(cur)
        if not searchable:
            print(f"  note  {NO_CANDIDATES}")
        for raw in args.run_dirs:
            path = Path(raw)
            if not (path / "manifest.json").exists():
                print(f"  skip  {path}: no manifest.json")
                continue
            rollouts, traced, books, cands = publish(cur, path, trading=trading,
                                                     searchable=searchable)
            total += rollouts
            books_total += books
            cands_total += cands
            print(f"  ok    {path.name:18} {rollouts:>5} rollouts, {traced:>5} traced, "
                  f"{books:>2} books, {cands:>3} candidates")
    conn.close()
    print(f"\n{total} rollouts, {books_total} paper books, "
          f"{cands_total} candidates published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
