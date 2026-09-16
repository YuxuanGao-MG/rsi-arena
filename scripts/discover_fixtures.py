"""Find settled fixtures that can be replayed, and write a benchmark file.

The question set is the benchmark, and five fixtures is not a question set. The
gate's interval over two held-out matches came out wider than the baseline skill
it was measuring, so nothing the loop found could ever have been promoted. Worse,
a bootstrap over windows treats the thirty-four windows of one match as
thirty-four independent facts when they are one match seen thirty-four times.

A fixture qualifies only if every piece needed to replay it exists: the Kalshi
event has settled, its markets are finalised, the fixture feed knows the match,
and a timeline can be built from timestamped events. Anything missing is
reported rather than skipped silently, because a question set that quietly
shrinks is a moving exam.

    python scripts/discover_fixtures.py --league EPL --limit 40 --out benchmarks/epl.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.kalshi._client import KalshiClient                  # noqa: E402
from rsi_arena.kalshi._gamestate import todays_games               # noqa: E402
from rsi_arena.kalshi._linking import (harvest_team_codes,         # noqa: E402
                                       link_event, names_from_markets)
from rsi_arena.kalshi._taxonomy import COMPETITIONS                # noqa: E402
from rsi_arena.kalshi.replay import match_timeline                 # noqa: E402


def _days_around(date: str) -> list[str]:
    from datetime import date as _date, timedelta

    try:
        d = _date.fromisoformat(date)
    except ValueError:
        return [date]
    return [(d + timedelta(days=n)).isoformat() for n in (0, -1, 1)]


def series_for(league: str) -> str:
    """Kalshi's match-winner series for a league, e.g. EPL -> KXEPLGAME."""
    return f"KX{league.replace('_', '')}GAME"


def resolve(client: KalshiClient, league: str, event: str,
            names: dict[str, str], codes: set[str]) -> tuple[dict | None, str]:
    """A benchmark row for this event, or None and the reason it cannot be one."""
    try:
        markets = client.get(f"/events/{event}")["markets"]
    except Exception as exc:
        return None, f"no markets ({type(exc).__name__})"
    tickers = [m["ticker"] for m in markets if m.get("status") in ("finalized", "settled")]
    if len(tickers) < 2:
        return None, f"only {len(tickers)} settled markets"

    # The feed dates a fixture by its own local day, so a kick-off can land on
    # either side of the UTC date. Both are offered, for the same reason
    # live_games spans them.
    def games_by_date(lg: str, day: str) -> list[dict]:
        out: list[dict] = []
        for probe in _days_around(day):
            try:
                out.extend(todays_games(lg, probe))
            except Exception:
                continue
        return out

    try:
        link = link_event(client, event, league, games_by_date,
                          series_ticker=series_for(league), names=names, codes=codes)
    except Exception as exc:
        return None, f"link failed ({type(exc).__name__}: {exc})"
    if link is None:
        return None, "no fixture matched with enough confidence"
    game = link.game_id
    try:
        line = match_timeline(league, str(game))
    except Exception as exc:
        return None, f"timeline failed ({type(exc).__name__})"
    if line is None:
        return None, "no timeline"
    n = len(line.windows(every_minutes=5))
    if n < 10:
        return None, f"only {n} windows"
    return {"league": league, "game": str(game), "event": event,
            "tickers": sorted(tickers)[:2], "windows": n}, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="EPL", help="comma separated; see kalshi/_taxonomy.py")
    ap.add_argument("--limit", type=int, default=40, help="settled events to examine per league")
    ap.add_argument("--out", default="", help="write a benchmark file here")
    args = ap.parse_args()

    client = KalshiClient()
    rows, rejected = [], []
    for league in [x.strip().upper() for x in args.league.split(",") if x.strip()]:
        if league not in COMPETITIONS:
            print(f"{league}: not a known competition", file=sys.stderr)
            continue
        # One sweep for the whole league: team codes and the names Kalshi prints
        # for them. Scoring a code against a feed name only works when the code
        # abbreviates it, and plenty do not — Liverpool trades as LFC.
        series = series_for(league)
        codes = harvest_team_codes(client, series)
        markets = list(client.paginate("/markets", "markets",
                                       {"series_ticker": series}, max_items=4000))
        names = names_from_markets(markets)
        print(f"{league}: {len(codes)} team codes, {len(names)} names "
              f"from {len(markets)} markets")

        events = list(client.paginate("/events", "events",
                                      {"series_ticker": series_for(league), "status": "settled"},
                                      max_items=args.limit))
        for event in events:
            ticker = event.get("event_ticker", "")
            if not ticker:
                continue
            row, why = resolve(client, league, ticker, names, codes)
            if row:
                rows.append(row)
                print(f"  ok    {ticker:34} game {row['game']:>10}  {row['windows']:>3} windows")
            else:
                rejected.append((ticker, why))
                print(f"  skip  {ticker:34} {why}")

    # Kalshi occasionally lists one match under two event tickers — RENPSG and
    # PSGREN both resolved to game 401876487. Two rows for one match would put
    # the same game on both sides of a fixture split, which is the one leak the
    # split exists to prevent, so the second is dropped rather than tolerated.
    seen: dict[str, str] = {}
    unique = []
    for row in rows:
        first = seen.get(row["game"])
        if first:
            print(f"  dup   {row['event']:34} same match as {first}")
            continue
        seen[row["game"]] = row["event"]
        unique.append(row)
    dropped = len(rows) - len(unique)
    rows = unique

    print(f"\n{len(rows)} usable, {len(rejected)} rejected"
          + (f", {dropped} duplicate matches dropped" if dropped else ""))
    if args.out and rows:
        # `windows` is diagnostic; the benchmark contract is the other four keys.
        payload = [{k: v for k, v in r.items() if k != "windows"} for r in rows]
        Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {args.out}  ({sum(r['windows'] for r in rows)} windows total)")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
