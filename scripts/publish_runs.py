"""Push run directories into Supabase, so a person can read what the agent did.

A run directory is the record, and it is JSON on someone's laptop. This copies
it somewhere a browser can reach: one row per generation, one per scored window,
and — when the rollouts were written with ``--trace`` — one per trajectory.

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

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:                                   # pragma: no cover
    sys.exit("pip install psycopg2-binary")

from rsi_arena.topics import TOPICS                   # noqa: E402

#: Tool answers are the bulk of a trace and the least re-read part of it.
TRIM = 1200

#: Every column a rollout row can carry, in insert order. The last two arrive
#: with migration 008; ``columns_of`` says which of these the database has, so
#: the same publisher works on either side of it.
COLUMNS = ("run_id", "side", "split", "fixture", "ticker", "at", "mid_now", "realised",
           "predicted", "half_width", "err", "naive_error", "skill", "echoed", "unmeasurable",
           "scored", "cost_usd", "ok", "error_text", "output", "game", "feedback",
           "topic", "unit")
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


def rollout_row(run_id: str, side: str, split: str, r: dict, *,
                topic: str = "", unit: str = "") -> dict[str, Any]:
    """One rollout as a column-keyed row. ``COLUMNS`` orders it for the insert."""
    inst, out = r["instance"], r["outcome"]
    d, run = out.get("details", {}), (r.get("run") or {})
    # The instance's own context: a match's game state for Kalshi, whatever
    # the instant looked like for a topic that calls it something else.
    context = inst.get("game") or inst.get("context")
    return {
        "run_id": run_id, "side": side, "split": split,
        # The fixture is the unit the split respects, so it is a column rather
        # than something a reader has to parse out of a ticker. Instances say
        # it outright now; older dumps are read the way they always were.
        "fixture": (inst.get("group")
                    or (inst.get("game") or {}).get("game_id")
                    or inst["ticker"].rsplit("-", 1)[0]),
        "ticker": inst["ticker"], "at": inst["at"],
        "mid_now": d.get("mid_now"), "realised": d.get("realised"),
        "predicted": d.get("predicted"), "half_width": d.get("half_width"),
        "err": d.get("error"), "naive_error": d.get("naive_error"), "skill": d.get("skill"),
        "echoed": d.get("echoed"), "unmeasurable": d.get("unmeasurable"), "scored": d.get("scored"),
        "cost_usd": r.get("cost_usd"), "ok": run.get("ok"), "error_text": run.get("error"),
        "output": Json(forecast(run, d)), "game": Json(context), "feedback": out.get("feedback"),
        "topic": topic, "unit": unit or d.get("unit") or "",
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


def publish(cur, run_dir: Path) -> tuple[int, int]:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    run_id = run_dir.name
    cur.execute("""
        insert into rsi.runs (id, topic, created, parent, incumbent, incumbent_fp,
                              candidate_fp, accepted, reasons, baseline, candidate,
                              decision, search, llm, split)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (id) do update set
            accepted = excluded.accepted, reasons = excluded.reasons,
            baseline = excluded.baseline, candidate = excluded.candidate,
            decision = excluded.decision, search = excluded.search,
            llm = excluded.llm, split = excluded.split
    """, (run_id, manifest["topic"], manifest.get("created"), manifest.get("parent"),
          manifest.get("incumbent"), manifest.get("incumbent_fingerprint"),
          manifest.get("candidate_fingerprint"),
          bool(manifest.get("decision", {}).get("accepted")),
          manifest.get("decision", {}).get("reasons") or [],
          Json(manifest.get("baseline")), Json(manifest.get("candidate")),
          Json(manifest.get("decision")), Json(manifest.get("search")),
          Json(manifest.get("llm")), Json(manifest.get("split"))))

    topic = manifest.get("topic") or ""
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
    total = 0
    with conn, conn.cursor() as cur:
        for raw in args.run_dirs:
            path = Path(raw)
            if not (path / "manifest.json").exists():
                print(f"  skip  {path}: no manifest.json")
                continue
            rollouts, traced = publish(cur, path)
            total += rollouts
            print(f"  ok    {path.name:18} {rollouts:>5} rollouts, {traced:>5} traced")
    conn.close()
    print(f"\n{total} rollouts published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
