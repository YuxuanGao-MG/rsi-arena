"""Push live forecasts into Supabase, so a person can read what the agent did.

``collect_live.py`` appends one JSON object per forecast to
``runs/live/forecasts.jsonl`` and that file never leaves the runner: the
directory is gitignored and the job is not allowed to commit. This is the only
way the day's work reaches a reader, so it runs after every collection.

Traces are trimmed the same way ``publish_runs.py`` trims them and for the same
reason — a trace is tens of kilobytes of minute bars repeated in the tool answer
and again in the prompt, and what a reader wants from one is which tools were
called, what came back in summary, and the prompt and answer that ended it.

The file is append-only, so a sidecar remembers how far it was read and a re-run
costs one stat call. The sidecar is an optimisation and nothing depends on it:
every row is upserted on ``(ticker, at)``, so publishing the same line twice is
a no-op and losing the sidecar republishes the file rather than duplicating it.

    python scripts/publish_live.py                      # since the last run
    python scripts/publish_live.py --all --dry-run      # everything, no database
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import psycopg2
    from psycopg2.extras import Json, execute_values
except ImportError:                                   # pragma: no cover
    sys.exit("pip install psycopg2-binary")

#: Tool answers are the bulk of a trace and the least re-read part of it.
TRIM = 1200


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
    """One step, small enough to keep a season of."""
    def short(value: Any) -> Any:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return text if len(text) <= TRIM else text[:TRIM] + f"… [{len(text) - TRIM} more]"

    return {"name": span.get("name"), "kind": span.get("kind"),
            "depth": span.get("depth"), "status": span.get("status"),
            "duration_s": span.get("duration_s"), "cost_usd": span.get("cost_usd"),
            "cached": span.get("cached"), "error": span.get("error"),
            "input": short(span.get("input")) if span.get("input") is not None else None,
            "output": short(span.get("output")) if span.get("output") is not None else None}


def why_thin(row: dict) -> str | None:
    """Why this row carries no skill: the run failed, or the market went quiet.

    Two causes in one column on purpose. They answer the same question a reader
    asks of a row with a dash where a number belongs, and the run's own error
    comes first — a harness that never answered is a different problem from a
    market that never printed at the horizon, and only the first is ours.
    """
    return row.get("error") or row.get("unscored_because")


#: The columns every deployment of the table has, in declaration order.
BASE_COLUMNS = ("at", "league", "game_id", "ticker", "mid_now", "realised", "harness",
                "output", "game", "spans", "skill", "scored", "ok", "error_text")

#: The columns migration 008 adds, and where each comes from in a collector's
#: row. Written only when the table has them, so the same script publishes
#: before and after the migration is applied.
DEFAULT_TOPIC = "kalshi-horizon-5m"
TOPIC_COLUMNS = ("topic", "symbol", "venue", "context", "unit")


def live_row(row: dict, extras: tuple[str, ...] = ()) -> tuple:
    """One forecast, in the column order ``rsi.live_forecasts`` declares.

    The first fourteen values are the table as 002 created it; ``extras`` names
    which of 008's columns to append, in order, so a caller that probed the
    table gets exactly the tuple its insert names.
    """
    scored = row.get("scored")
    spans = (row.get("run") or {}).get("trace") or []
    base = (row["at"], row.get("league"), row.get("game_id"), row["ticker"],
            row.get("mid_now"), row.get("realised"), row.get("harness"),
            Json(row.get("output")), Json(row.get("game")),
            Json([trim_span(s) for s in spans]),
            (scored or {}).get("skill"), scored is not None,
            row.get("ok"), why_thin(row))
    more = {
        # A collector that predates topics wrote no topic; it was Kalshi's.
        "topic": row.get("topic") or DEFAULT_TOPIC,
        "symbol": row.get("symbol"),
        "venue": row.get("venue"),
        "context": Json(row.get("context")) if row.get("context") is not None else None,
        "unit": row.get("unit") or "cents",
    }
    return base + tuple(more[name] for name in extras)


def topic_columns(cur) -> tuple[str, ...]:
    """Which of 008's columns this database has.

    Probed rather than assumed: the publisher runs on a cron beside a migration
    someone applies by hand, and the two are not applied in the same minute.
    Naming a column the table lacks fails the whole insert; omitting one it has
    lets its default stand, which for `topic` and `unit` is Kalshi's.
    """
    cur.execute("""
        select column_name from information_schema.columns
         where table_schema = 'rsi' and table_name = 'live_forecasts'
    """)
    present = {name for (name,) in cur.fetchall()}
    return tuple(name for name in TOPIC_COLUMNS if name in present)


def read_rows(path: Path, start: int) -> tuple[list[dict], int]:
    """Objects from ``start`` bytes on, and where reading stopped.

    A collection still running is appending to this file, and the last line may
    be half written when the publisher opens it. Reading stops at the last
    newline rather than guessing, and the sidecar remembers that boundary, so
    the partial line is published whole by the next invocation.
    """
    with path.open("rb") as fh:
        fh.seek(start)
        blob = fh.read()
    cut = blob.rfind(b"\n")
    if cut < 0:
        return [], start
    rows = []
    for line in blob[:cut].splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows, start + cut + 1


def offset_of(sidecar: Path, path: Path) -> int:
    """How far the last run read, or zero if that is no longer believable."""
    if not sidecar.exists():
        return 0
    try:
        mark = int(json.loads(sidecar.read_text())["bytes"])
    except (ValueError, KeyError, TypeError):
        return 0
    # A file shorter than the mark was rotated or truncated, and resuming from
    # the old offset would skip everything written since. Start over; the
    # upsert makes that cost nothing but a little bandwidth.
    return mark if 0 <= mark <= path.stat().st_size else 0


def publish(cur, rows: list[dict]) -> int:
    extras = topic_columns(cur)
    columns = BASE_COLUMNS + extras
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in ("at", "ticker"))
    execute_values(cur, f"""
        insert into rsi.live_forecasts ({", ".join(columns)})
        values %s
        on conflict (ticker, at) do update set {updates}
    """, [live_row(r, extras) for r in rows])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default="runs/live/forecasts.jsonl")
    ap.add_argument("--db-url", default="")
    ap.add_argument("--all", action="store_true", help="ignore the sidecar and republish the file")
    ap.add_argument("--dry-run", action="store_true", help="print the rows; touch no database")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"no {path}; nothing collected")
        return 0
    sidecar = path.with_suffix(path.suffix + ".published")
    start = 0 if args.all else offset_of(sidecar, path)
    rows, end = read_rows(path, start)
    if not rows:
        print(f"nothing new in {path} (read to byte {start} of {path.stat().st_size})")
        return 0

    if args.dry_run:
        for row in rows:
            (at, league, game_id, ticker, mid, realised, harness,
             output, _game, spans, skill, scored, ok, error, topic, symbol, venue,
             _context, unit) = live_row(row, TOPIC_COLUMNS)
            print(f"  {at}  {topic:18} {league or symbol or '':11} {game_id or venue or '':10} "
                  f"{ticker:34} mid {mid}  realised {realised}  skill {skill} {unit}  "
                  f"scored {scored}  ok {ok}")
            print(f"      {harness}: {json.dumps(output.adapted, default=str)[:160]}")
            print(f"      {len(spans.adapted)} spans"
                  + (f", error {error}" if error else ""))
        print(f"\n{len(rows)} rows would be written to rsi.live_forecasts")
        return 0

    conn = psycopg2.connect(db_url(args.db_url), connect_timeout=30)
    conn.autocommit = False
    with conn, conn.cursor() as cur:
        written = publish(cur, rows)
    conn.close()
    # Only after the commit. A sidecar moved ahead of a failed transaction is
    # how a day's forecasts go missing without anything reporting a failure.
    sidecar.write_text(json.dumps({"bytes": end, "rows": len(rows)}))
    print(f"\n{written} live forecasts published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
