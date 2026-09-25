"""Everything the arena recorded about one window, in one place.

A decision is spread across four tables by the time it is published: the
question and the graded numbers in ``rsi.rollouts``, what the paper book quoted
and filled in the three columns migration 010 added, the position it opened in
``rsi.trades``, and how the answer was reached in ``rsi.traces``. Auditing one
of them meant four queries and a mental join, so nobody audited one.

This is that join, printed: the instance, the forecast, the quote, what the path
did, every fill, the trade, the skill numbers, and the trace spans - for both
sides of a generation, so the same window reads as the baseline saw it and as
the candidate did. Read-only; it writes nothing and it takes no locks beyond a
select.

    python scripts/show_window.py gen12 \
        'KXBUNDESLIGAGAME-26AUG29RBLBMG-BMG@2026-08-29T13:35:00+00:00'
    python scripts/show_window.py --live KXEPL-ARSMCI-ARS 2026-09-20T15:00:00+00:00
    python scripts/show_window.py gen12 '<instance id>' --json | jq .sides[0].quote
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import psycopg2
except ImportError:                                   # pragma: no cover
    psycopg2 = None

from publish_live import db_url                        # noqa: E402
from publish_trading import has_trading_tables         # noqa: E402

#: How much of a span's prompt or answer to show. The publisher already trimmed
#: them to 1,200 characters; this is a terminal, not a trace viewer.
SPAN_TEXT = 220


def split_instance(instance_id: str) -> tuple[str, str]:
    """``(ticker, instant)`` from an instance id, which is ``<ticker>@<instant>``.

    Split at the last ``@``: a Kalshi ticker carries no ``@`` but an ISO instant
    carries colons and plus signs, and splitting at the first one would cut a
    symbol that ever grows one.
    """
    if "@" not in instance_id:
        sys.exit(f"'{instance_id}' is not an instance id; expected <ticker>@<instant>")
    ticker, at = instance_id.rsplit("@", 1)
    return ticker, at


# ---------------------------------------------------------------------------
# Reading.

def rows_of(cur, sql: str, params: tuple) -> list[dict[str, Any]]:
    """Every row as a column-keyed dict.

    ``select *`` and the cursor's own description, rather than a column list:
    this runs against a database that may or may not have migration 010 yet,
    and a reader that names a column the table lacks fails instead of saying
    the column is empty.
    """
    cur.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def window_rows(cur, run_id: str, instance_id: str) -> list[dict[str, Any]]:
    """The rollouts of one window: one per side that was scored on it."""
    ticker, at = split_instance(instance_id)
    rows = rows_of(cur, "select * from rsi.rollouts where run_id = %s and ticker = %s "
                        "and at = %s order by side, split", (run_id, ticker, at))
    trades = trades_of(cur, instance_id)
    for row in rows:
        row["trace"] = trace_of(cur, row.get("id"))
        # A trade's book_id is ``<run id>:<side>:<split>``, which is exactly the
        # three columns of the row it belongs to, so each side gets its own.
        book = f"{run_id}:{row.get('side')}:{row.get('split')}"
        row["trades"] = (None if trades is None else
                         [t for t in trades if t.get("book_id") == book])
    return rows


def live_rows(cur, ticker: str, at: str) -> list[dict[str, Any]]:
    """The live forecast on one ticker at one instant, with what the live book
    did about it. At most one forecast row: the table is keyed on exactly that
    pair."""
    rows = rows_of(cur, "select * from rsi.live_forecasts where ticker = %s and at = %s",
                   (ticker, at))
    trades = trades_of(cur, f"{ticker}@{at}")
    for row in rows:
        row["trades"] = trades
    return rows


def trades_of(cur, instance_id: str) -> list[dict[str, Any]] | None:
    """The positions a paper book opened on this window, or None if this
    database has no ``rsi.trades`` to ask.

    The engine stamps each trade with the instance id of the cycle that opened
    it, which is the id this tool was given, so one equality finds them. None
    and the empty list are different answers: "migration 009 is not applied" is
    not "the harness did not trade".
    """
    if not has_trading_tables(cur):
        return None
    return rows_of(cur, "select * from rsi.trades where instance_id = %s order by opened_at",
                   (instance_id,))


def trace_of(cur, rollout_id: Any) -> list[dict[str, Any]]:
    """The spans of one rollout, or none if it was not traced."""
    if rollout_id is None:
        return []
    cur.execute("select spans from rsi.traces where rollout_id = %s", (rollout_id,))
    found = cur.fetchone()
    spans = found[0] if found else None
    if isinstance(spans, str):
        try:
            spans = json.loads(spans)
        except ValueError:
            return []
    return list(spans or [])


# ---------------------------------------------------------------------------
# Printing.

def num(value: Any, spec: str = "g") -> str:
    """A number, or a dash where the arena recorded nothing."""
    if value is None:
        return "-"
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return str(value)


def as_dict(value: Any) -> dict[str, Any]:
    """A jsonb column as a dict, whatever the driver handed back."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def field(label: str, text: str, out) -> None:
    print(f"  {label:10} {text}", file=out)


def print_forecast(row: dict[str, Any], out) -> None:
    said = as_dict(row.get("output"))
    field("forecast", json.dumps(said, default=str) if said else "(nothing it said was kept)", out)


def print_skill(row: dict[str, Any], out) -> None:
    unit = row.get("unit") or ""
    flags = [name for name in ("echoed", "unmeasurable", "scored", "ok") if row.get(name)]
    field("skill", f"err {num(row.get('err'), '.4g')} naive {num(row.get('naive_error'), '.4g')} "
                   f"skill {num(row.get('skill'), '+.4f')} {unit}"
                   + (f"  [{', '.join(flags)}]" if flags else ""), out)
    field("prices", f"mid {num(row.get('mid_now'), '.4g')} -> realised "
                    f"{num(row.get('realised'), '.4g')}; predicted "
                    f"{num(row.get('predicted'), '.4g')} half-width "
                    f"{num(row.get('half_width'), '.4g')}", out)
    if row.get("error_text"):
        field("error", str(row["error_text"]), out)


def print_quote(row: dict[str, Any], out) -> None:
    """The quote, the path and the fills - the three columns 010 added.

    A row with no quote says so once rather than printing three dashes: a topic
    that does not trade and a run published before its books existed both look
    like this, and neither is a window worth reading a fill out of.
    """
    quote = as_dict(row.get("quote"))
    path = as_dict(row.get("path"))
    fills = as_list(row.get("fills"))
    if not quote and not path and not fills:
        field("book", "nothing recorded (no paper book replayed this window)", out)
        return
    if quote:
        field("quote", f"{num(quote.get('bid'), '.4g')} / {num(quote.get('ask'), '.4g')}  "
                       f"size {num(quote.get('size_frac'), '.2%')} "
                       f"(${num(quote.get('size_usd'), ',.0f')})  "
                       f"off mid {num(quote.get('mid_now'), '.4g')}  "
                       f"{quote.get('source') or '-'} {quote.get('unit') or ''}".rstrip(), out)
    if path:
        field("path", f"{num(path.get('bars'), '.0f')} bars, high {num(path.get('high'), '.4g')} "
                      f"low {num(path.get('low'), '.4g')} close {num(path.get('close'), '.4g')}; "
                      f"crossed bid {'yes' if path.get('crossed_bid') else 'no'}, "
                      f"ask {'yes' if path.get('crossed_ask') else 'no'}", out)
    field("fills", f"{len(fills)}" + ("" if fills else "  (the quote was never taken)"), out)
    for f in fills:
        f = as_dict(f)
        print(f"    {f.get('side') or '-':5} {num(f.get('px'), '.4g'):>8} x "
              f"{num(f.get('qty'), ',.0f'):>10}  ${num(f.get('notional_usd'), ',.2f'):>12}  "
              f"fees ${num(f.get('fees_usd'), ',.2f')}  {f.get('at') or '-'}  "
              f"{f.get('effect') or ''}".rstrip(), file=out)


def print_trade(row: dict[str, Any], out) -> None:
    """What the window was worth as money: the positions it opened, and the
    engine's own one-line rendering of the cycle, which the feedback carries."""
    trades = row.get("trades")
    if trades is None:
        field("trade", "rsi.trades is not in this database (migration 009)", out)
    elif not trades:
        field("trade", "no position was opened on this window", out)
    else:
        field("trade", f"{len(trades)}", out)
        for t in trades:
            print(f"    {t.get('side') or '-':5} {t.get('instrument') or '-':34} "
                  f"{num(t.get('entry_px'), '.4g'):>8} -> {num(t.get('exit_px'), '.4g'):>8}  "
                  f"${num(t.get('size_usd'), ',.0f'):>10}  "
                  f"pnl ${num(t.get('pnl_usd'), ',.2f')}  fees ${num(t.get('fees_usd'), ',.2f')}  "
                  f"{t.get('reason') or 'open'}  {t.get('source') or ''}".rstrip(), file=out)
    if row.get("feedback"):
        field("feedback", str(row["feedback"]), out)


def print_trace(spans: list[dict[str, Any]], out) -> None:
    if not spans:
        field("trace", "not traced (the run was scored without --trace)", out)
        return
    field("trace", f"{len(spans)} spans", out)
    for span in spans:
        span = as_dict(span)
        head = ("    " + "  " * int(span.get("depth") or 0)
                + f"{span.get('kind') or '?':5} {span.get('name') or '?'}")
        print(f"{head:52} {span.get('status') or '-':8} "
              f"{num(span.get('duration_s'), '.2f'):>6}s ${num(span.get('cost_usd'), '.4f')}"
              + ("  cached" if span.get("cached") else "")
              + (f"  error {span['error']}" if span.get("error") else ""), file=out)
        for name in ("input", "output"):
            text = span.get(name)
            if text:
                text = text if isinstance(text, str) else json.dumps(text, default=str)
                one = " ".join(text.split())
                print(f"      {name:6} {one[:SPAN_TEXT]}"
                      + ("…" if len(one) > SPAN_TEXT else ""), file=out)


def print_window(run_id: str, instance_id: str, rows: list[dict[str, Any]], out=sys.stdout) -> None:
    print(f"window  {run_id}  {instance_id}", file=out)
    if not rows:
        print("  nothing published for that run and instance", file=out)
        return
    first = rows[0]
    print(f"        topic {first.get('topic') or '-'}  unit {first.get('unit') or '-'}  "
          f"fixture {first.get('fixture') or '-'}", file=out)
    context = as_dict(first.get("game"))
    if context:
        print(f"        instance {json.dumps(context, default=str)[:400]}", file=out)
    for row in rows:
        print(f"\n{row.get('side') or '?'} / {row.get('split') or '?'}  "
              f"${num(row.get('cost_usd'), '.4f')}", file=out)
        print_forecast(row, out)
        print_skill(row, out)
        print_quote(row, out)
        print_trade(row, out)
        print_trace(row.get("trace") or [], out)


def print_live(ticker: str, at: str, rows: list[dict[str, Any]], out=sys.stdout) -> None:
    print(f"live    {ticker}  {at}", file=out)
    if not rows:
        print("  nothing published for that ticker and instant", file=out)
        return
    for row in rows:
        print(f"\n{row.get('harness') or '?'}  topic {row.get('topic') or '-'}  "
              f"unit {row.get('unit') or '-'}", file=out)
        print_forecast(row, out)
        # A live row keeps the skill it was graded with and not the error terms
        # behind it; the grader's own numbers live in the paper book.
        field("skill", f"{num(row.get('skill'), '+.4f')}  "
                       f"mid {num(row.get('mid_now'), '.4g')} -> realised "
                       f"{num(row.get('realised'), '.4g')}  "
                       f"[{'scored' if row.get('scored') else 'unscored'}, "
                       f"{'ok' if row.get('ok') else 'failed'}]", out)
        if row.get("error_text"):
            field("error", str(row["error_text"]), out)
        print_quote(row, out)
        print_trade(row, out)
        print_trace(as_list(row.get("spans")), out)


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", nargs=2, metavar=("RUN_ID|TICKER", "INSTANCE_ID|AT"))
    ap.add_argument("--live", action="store_true",
                    help="read rsi.live_forecasts: the arguments are a ticker and an instant")
    ap.add_argument("--json", action="store_true", help="the rows themselves, for a machine")
    ap.add_argument("--db-url", default="")
    return ap


def main(argv: list[str] | None = None) -> int:
    if psycopg2 is None:                                  # pragma: no cover
        sys.exit("pip install psycopg2-binary")
    args = build_parser().parse_args(argv)
    first, second = args.what

    conn = psycopg2.connect(db_url(args.db_url), connect_timeout=30)
    try:
        with conn.cursor() as cur:
            rows = (live_rows(cur, first, second) if args.live
                    else window_rows(cur, first, second))
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"live": args.live, "key": [first, second], "sides": rows},
                         indent=1, default=str))
    elif args.live:
        print_live(first, second, rows)
    else:
        print_window(first, second, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
