"""What a generation's search actually proposed, and what it actually changed.

Six Kalshi generations ran before anyone could answer this. The search proposed
all four components every time - in gen6 the model twelve times, the context
nine, the plan nine, the tools eight - and every candidate that survived its
minibatch and got filed differed from the seed in ``context`` alone, while that
context grew from 5,470 to 8,757 characters. The evidence was in a pickle in a
run directory and the conclusion was therefore invisible: six generations of
"no improvement" that were really one generation of "the search only ever
rewrites the prose, and the prose only ever gets longer".

This is that table, one query from the reader: index, parent, which components
differ from the seed, how long the context got, the valset mean GEPA selects on,
and whether the gate promoted it - with the one line of summary that would have
caught the stall. Read-only.

    python scripts/show_search.py gen12
    python scripts/show_search.py gen6@crypto-horizon-1m --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import psycopg2
except ImportError:                                   # pragma: no cover
    psycopg2 = None

from publish_live import db_url                        # noqa: E402

COLUMNS = ("topic", "candidate_idx", "parent_idx", "changed_components", "accepted",
           "valset_mean", "context_chars", "discovered_after_calls", "fingerprint",
           "objectives")


def candidate_rows(cur, run_id: str, topic: str = "") -> list[dict[str, Any]]:
    """Every candidate of one generation, in the order the search found them."""
    sql = (f"select {', '.join(COLUMNS)} from rsi.candidates where run_id = %s"
           + (" and topic = %s" if topic else "")
           + " order by topic, candidate_idx")
    cur.execute(sql, (run_id, topic) if topic else (run_id,))
    return [dict(zip(COLUMNS, row)) for row in cur.fetchall()]


def changed_of(row: dict[str, Any]) -> tuple[str, ...]:
    """The diff against the seed, as a tuple. A Postgres text[] arrives as a
    list; a row written before the column existed arrives as None."""
    found = row.get("changed_components")
    return tuple(found) if isinstance(found, (list, tuple)) else ()


def summarise(rows: list[dict[str, Any]]) -> str:
    """The line that would have caught the stall.

    Two facts, because either alone is misleading: what the search mutated
    (a search that only rewrites one component is not searching four) and what
    happened to the length of the thing it rewrote (a context that only grows is
    a search adding words, not finding a better instruction).
    """
    if not rows:
        return "no candidates published for that run"
    total = len(rows)
    counts = Counter(changed_of(row) for row in rows)
    mutated = {sig: n for sig, n in counts.items() if sig}
    if not mutated:
        head = f"{total} candidates, none of which differs from the seed"
    else:
        sig, n = max(mutated.items(), key=lambda kv: (kv[1], -len(kv[0])))
        head = f"{n} of {total} candidates changed {' + '.join(sig)} only"
        rest = Counter(c for s, k in mutated.items() if s != sig for c in s for _ in range(k))
        if rest:
            head += "; also " + ", ".join(f"{c} ({k})" for c, k in sorted(rest.items()))

    chars = [row.get("context_chars") for row in rows if row.get("context_chars") is not None]
    if not chars:
        return head
    seed = next((row.get("context_chars") for row in rows if row.get("candidate_idx") == 0), None)
    seed = chars[0] if seed is None else seed
    widest = max(chars)
    if widest > seed:
        return f"{head}; context grew {seed:,} -> {widest:,} chars"
    if widest < seed:
        return f"{head}; context shrank {seed:,} -> {min(chars):,} chars"
    return f"{head}; context stayed {seed:,} chars"


def render(run_id: str, rows: list[dict[str, Any]]) -> str:
    """The table, one block per topic that published this run id."""
    if not rows:
        return f"search  {run_id}\n  {summarise(rows)}"
    out: list[str] = []
    for topic in dict.fromkeys(row.get("topic") or "" for row in rows):
        mine = [row for row in rows if (row.get("topic") or "") == topic]
        out.append(f"search  {run_id}  {topic}  {len(mine)} candidates")
        head = (f"  {'idx':>3} {'parent':>6}  {'changed':24} {'context':>8} "
                f"{'valset':>8} {'found@':>7}  accepted")
        out += [head, "  " + "-" * (len(head) - 2)]
        for row in mine:
            changed = ", ".join(changed_of(row)) or ("(seed)" if row.get("candidate_idx") == 0
                                                     else "(identical to the seed)")
            parent = row.get("parent_idx")
            mean = row.get("valset_mean")
            out.append(f"  {row.get('candidate_idx'):>3} "
                       f"{'-' if parent is None else parent:>6}  {changed[:24]:24} "
                       f"{(row.get('context_chars') or 0):>8,} "
                       f"{'-' if mean is None else format(mean, '+.4f'):>8} "
                       f"{(row.get('discovered_after_calls') or 0):>7}  "
                       f"{'yes' if row.get('accepted') else 'no'}")
        out.append("")
        out.append("  " + summarise(mine))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_id", help="a generation's published id, e.g. gen12 or gen6@news-equity-5m")
    ap.add_argument("--topic", default="", help="only this topic's candidates")
    ap.add_argument("--json", action="store_true", help="the rows themselves, for a machine")
    ap.add_argument("--db-url", default="")
    return ap


def main(argv: list[str] | None = None) -> int:
    if psycopg2 is None:                                  # pragma: no cover
        sys.exit("pip install psycopg2-binary")
    args = build_parser().parse_args(argv)

    conn = psycopg2.connect(db_url(args.db_url), connect_timeout=30)
    try:
        with conn.cursor() as cur:
            rows = candidate_rows(cur, args.run_id, args.topic)
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"run_id": args.run_id, "candidates": rows,
                          "summary": summarise(rows)}, indent=1, default=str))
    else:
        print(render(args.run_id, rows), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
