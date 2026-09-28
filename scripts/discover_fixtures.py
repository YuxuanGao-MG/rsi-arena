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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.kalshi._client import KalshiClient                  # noqa: E402
from rsi_arena.kalshi._gamestate import todays_games               # noqa: E402
from rsi_arena.kalshi._linking import (harvest_team_codes,         # noqa: E402
                                       link_event, names_from_markets,
                                       parse_event_ticker)
from rsi_arena.kalshi._series import (SWEEP_LEAGUES,               # noqa: E402
                                      series_for as _series_for)
from rsi_arena.kalshi._taxonomy import COMPETITIONS                # noqa: E402
from rsi_arena.kalshi.replay import match_timeline                 # noqa: E402


#: How far either side of the ticker's date to look for the fixture.
#:
#: Not a safety margin — a measured one. Kalshi dates an event by the day it
#: listed the contract, the feed by the day it kicked off, and those are the same
#: day less often than you would think: Sevilla against Valencia trades as
#: 26SEP13 and was played on the 11th. One day either side left 168 settled
#: fixtures unmatched out of 654. Three catches them.
DATE_SPREAD = 3


def _days_around(date: str) -> list[str]:
    """The ticker's date first, then outwards, so the likeliest is tried first."""
    from datetime import date as _date, timedelta

    try:
        d = _date.fromisoformat(date)
    except ValueError:
        return [date]
    offsets = [0]
    for n in range(1, DATE_SPREAD + 1):
        offsets += [-n, n]
    return [(d + timedelta(days=n)).isoformat() for n in offsets]


def series_for(league: str) -> str:
    """Kalshi's match-winner series for a league, e.g. EPL -> KXEPLGAME.

    A verified lookup now, not the ``KX{LEAGUE}GAME`` rule this used to be. The
    rule is right for the eight leagues this script was written against and
    wrong for most of the rest — the UEFA Nations League trades as
    ``KXUEFANLGAME`` — and a wrong series ticker returns an empty listing, which
    reads as a competition with no matches rather than as a bug. See
    ``rsi_arena/kalshi/_series.py``; it remains the fallback for a league nobody
    has verified, and says so when it is used.
    """
    return _series_for(league)


def event_day(event_ticker: str) -> str:
    """The ISO day encoded in an event ticker, or "" when it encodes none."""
    fixture = parse_event_ticker(event_ticker)
    return fixture.date.isoformat() if fixture else ""


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
    ap.add_argument("--league", default="EPL",
                    help="comma separated, or 'all' for every competition with a "
                         "verified series in kalshi/_series.py")
    ap.add_argument("--limit", type=int, default=600,
                    help="settled events to examine per league. Kalshi has about "
                         "3,600 settled soccer fixtures across the majors; the "
                         "first version of this script looked at 40 of them")
    ap.add_argument("--max-per-league", type=int, default=0,
                    help="stop a league once it has contributed this many fixtures "
                         "in this run; 0 is no cap. Seventy competitions at six "
                         "hundred events each is a day of network, and a set that "
                         "is nine tenths one league is not a broader exam. Counted "
                         "per run, not against what --resume already held, so a "
                         "second pass over a later window adds its own share")
    ap.add_argument("--since", default="",
                    help="ignore events dated before this (YYYY-MM-DD), read off "
                         "the event ticker. The question set covers a window, and "
                         "a new competition should be added over the same one")
    ap.add_argument("--until", default="",
                    help="ignore events dated after this (YYYY-MM-DD)")
    ap.add_argument("--workers", type=int, default=6,
                    help="fixtures resolved at once. Each costs two round trips "
                         "and the fixture feed is throttled at four a second")
    ap.add_argument("--out", default="", help="write a benchmark file here")
    ap.add_argument("--resume", action="store_true",
                    help="keep what --out already holds and only look at what is missing")
    args = ap.parse_args()

    client = KalshiClient()
    # Resuming matters at this size: three thousand fixtures is hours of network,
    # and a dropped connection an hour in should not mean starting again.
    already: set[str] = set()
    rows: list[dict] = []
    if args.resume and args.out and Path(args.out).exists():
        rows = json.loads(Path(args.out).read_text())
        already = {r["event"] for r in rows}
        print(f"resuming with {len(rows)} fixtures already found")
    rejected: list[tuple[str, str]] = []
    if args.league.strip().lower() == "all":
        leagues = list(SWEEP_LEAGUES)
    else:
        leagues = [x.strip().upper() for x in args.league.split(",") if x.strip()]
    contributed: dict[str, int] = {}
    for league in leagues:
        if league not in COMPETITIONS:
            print(f"{league}: not a known competition", file=sys.stderr)
            continue
        # One sweep for the whole league: team codes and the names Kalshi prints
        # for them. Scoring a code against a feed name only works when the code
        # abbreviates it, and plenty do not — Liverpool trades as LFC.
        #
        # Guarded, because this now runs over seventy competitions: one league
        # whose catalogue read times out must not take the fixtures every earlier
        # league already paid for down with it.
        series = series_for(league)
        try:
            codes = harvest_team_codes(client, series)
            markets = list(client.paginate("/markets", "markets",
                                           {"series_ticker": series}, max_items=4000))
            names = names_from_markets(markets)
            events = list(client.paginate("/events", "events",
                                          {"series_ticker": series, "status": "settled"},
                                          max_items=args.limit))
        except Exception as exc:
            print(f"{league}: catalogue failed ({type(exc).__name__}: {exc}); skipped",
                  file=sys.stderr)
            continue
        print(f"{league} ({series}): {len(codes)} team codes, {len(names)} names "
              f"from {len(markets)} markets, {len(events)} settled events")

        found = 0
        for event in events:
            ticker = event.get("event_ticker", "")
            if not ticker:
                continue
            if args.max_per_league and found >= args.max_per_league:
                print(f"  {league}: {found} fixtures is this league's share; moving on")
                break
            when = event_day(ticker)
            if when and ((args.since and when < args.since)
                         or (args.until and when > args.until)):
                continue
            if ticker in already:
                continue
            row, why = resolve(client, league, ticker, names, codes)
            if row:
                rows.append(row)
                found += 1
                print(f"  ok    {ticker:34} game {row['game']:>10}  {row['windows']:>3} windows")
            else:
                rejected.append((ticker, why))
                print(f"  skip  {ticker:34} {why}")
        contributed[league] = found

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
    if len(contributed) > 1:
        print("by competition: " + ", ".join(
            f"{lg} {n}" for lg, n in sorted(contributed.items(), key=lambda kv: -kv[1]) if n))
        empty = [lg for lg, n in contributed.items() if not n]
        if empty:
            print(f"nothing from: {', '.join(sorted(empty))}")
    if args.out and rows:
        # `windows` is diagnostic; the benchmark contract is the other four keys.
        payload = [{k: v for k, v in r.items() if k != "windows"} for r in rows]
        Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
        # ``windows`` is only on the rows this run resolved; a resumed row came
        # off disk without it, and reading it unconditionally turned a finished
        # eight-hour sweep into a traceback after the file was already written.
        print(f"wrote {args.out}  ({sum(r.get('windows', 0) for r in rows)} "
              f"windows from this run's fixtures)")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
