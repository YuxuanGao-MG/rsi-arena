"""Re-verify the league -> Kalshi series table against the live exchange and feed.

``rsi_arena/kalshi/_series.py`` is true on the day it was written and no longer.
Kalshi adds competitions most weeks, retires others, and occasionally renames a
ticker; ESPN drops a competition from its scoreboard without saying so. Either
way the sweep goes quiet rather than red, which is the failure this repository
keeps re-learning: a wrong series ticker and a night with no football look
identical from here.

So this asks the venues, the same way the table was built:

* every series in Kalshi's ``Sports`` category whose own ``tags`` field says
  soccer — sport from the metadata, never from the ticker, because pattern
  matching the ticker is what hid ``KXUEFANLGAME``;
* which of them carry events that split into two team codes, so a per-match
  moneyline is told apart from a season future by what it prices rather than by
  what it is called;
* for each mapped league, whether its series still exists and still links to the
  fixture feed;
* for each unmapped or seasonal one, whether it has started working.

Nothing is written. The output is the diff against the table, so a person can
make the edit and say why in the commit.

    python scripts/check_leagues.py                  # the whole table, ~5 minutes
    python scripts/check_leagues.py --league UCL,UEL  # just these
    python scripts/check_leagues.py --quick           # no fixture-feed links
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.kalshi import _gamestate as gs                        # noqa: E402
from rsi_arena.kalshi._client import KalshiClient                    # noqa: E402
from rsi_arena.kalshi._linking import (link_event,                   # noqa: E402
                                       names_from_markets, parse_event_ticker)
from rsi_arena.kalshi._series import (NOT_FIXTURES, SEASONAL,        # noqa: E402
                                      SERIES_BY_LEAGUE, UNMAPPED, check_table)
from rsi_arena.kalshi._taxonomy import COMPETITIONS                  # noqa: E402

#: Markets read per series. Enough to see a season's events without paying for
#: every spread line the competition ever listed.
MARKET_SAMPLE = 300
#: Fixtures linked before a league is called healthy. Two is the same evidence
#: the table was built on: one link can be a coincidence of two similar names.
LINKS_WANTED = 2
#: Fixtures tried before giving up on a league. A cup's newest events are often
#: postponements that the feed never carried.
LINKS_TRIED = 8
#: Days either side of a ticker's date to look for the fixture, as everywhere.
DATE_SPREAD = 2


def soccer_series(client: KalshiClient) -> dict[str, dict]:
    """Every series Kalshi itself tags as soccer, by ticker."""
    out: dict[str, dict] = {}
    for s in client.paginate("/series", "series", {"category": "Sports"}, max_items=20000):
        tags = [str(t).strip().lower() for t in (s.get("tags") or [])]
        if "soccer" in tags:
            out[s.get("ticker", "")] = s
    out.pop("", None)
    return out


def team_codes(markets: list[dict]) -> set[str]:
    """The codes a series' market tickers end in, as ``harvest_team_codes`` does."""
    codes: set[str] = set()
    for m in markets:
        tail = m.get("ticker", "").rsplit("-", 1)[-1]
        for candidate in (re.sub(r"\d+$", "", tail), tail):
            if (candidate and candidate.isalnum() and len(candidate) <= 4
                    and candidate not in {"TIE", "YES", "NO"}):
                codes.add(candidate)
    return codes


def fixtures_of(client: KalshiClient, series: str) -> tuple[list[tuple[date, str]], dict[str, str], set[str]]:
    """This series' fixture events newest first, with the names and codes to link them.

    Newest by the date **in the ticker**, not by the ticker as a string:
    ``26SEP`` sorts after ``26OCT`` alphabetically, and sampling a cup that way
    probes the feed on the wrong month and reports a working competition as
    broken. That mistake cost the first pass of this verification four
    competitions.
    """
    markets = list(client.paginate("/markets", "markets",
                                   {"series_ticker": series}, max_items=MARKET_SAMPLE))
    codes = team_codes(markets)
    names = names_from_markets(markets)
    dated: list[tuple[date, str]] = []
    for event in {m.get("event_ticker", "") for m in markets if m.get("event_ticker")}:
        fixture = parse_event_ticker(event, series, codes)
        if fixture and fixture.is_split:
            dated.append((fixture.date, event))
    dated.sort(reverse=True)
    return dated, names, codes


def links(client: KalshiClient, league: str, series: str, slug: str | None = None) -> tuple[int, int, str]:
    """How many of this series' newest fixtures the feed can be joined to."""
    dated, names, codes = fixtures_of(client, series)
    if not dated:
        return 0, 0, "no event splits into two team codes"
    seen: dict[str, list[dict]] = {}

    def by_date(lg: str, day: str) -> list[dict]:
        out: list[dict] = []
        start = date.fromisoformat(day)
        for n in [0] + [x for n in range(1, DATE_SPREAD + 1) for x in (-n, n)]:
            probe = (start + timedelta(days=n)).isoformat()
            if probe not in seen:
                try:
                    seen[probe] = gs.todays_games(lg, probe, espn_slug=slug)
                except Exception:
                    seen[probe] = []
            out.extend(seen[probe])
        return out

    found = 0
    for _day, event in dated[:LINKS_TRIED]:
        try:
            link = link_event(client, event, league, by_date,
                              series_ticker=series, names=names, codes=codes)
        except Exception:
            link = None
        if link:
            found += 1
        if found >= LINKS_WANTED:
            break
    served = any(seen.values())
    why = "" if found else ("the fixture feed served no card on any probed day"
                            if not served else "the feed answered but nothing matched")
    return found, len(dated), why


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", default="", help="comma separated; default is the whole table")
    ap.add_argument("--quick", action="store_true",
                    help="check Kalshi only: the series exist and still price fixtures")
    ap.add_argument("--json", default="", help="also write the findings here")
    args = ap.parse_args()

    client = KalshiClient()
    problems = check_table()
    for line in problems:
        print(f"::error title=table inconsistent::{line}")

    print("asking Kalshi which series it tags as soccer ...")
    catalogue = soccer_series(client)
    print(f"{len(catalogue)} soccer series listed\n")

    only = {x.strip().upper() for x in args.league.split(",") if x.strip()}
    findings: dict[str, list[dict]] = {"dead": [], "broken": [], "promotable": [],
                                       "new": [], "healthy": []}

    print("-- mapped leagues")
    for league, series in SERIES_BY_LEAGUE.items():
        if only and league not in only:
            continue
        if series not in catalogue:
            print(f"  DEAD      {league:16} {series} is no longer a soccer series on Kalshi")
            findings["dead"].append({"league": league, "series": series})
            continue
        if args.quick:
            print(f"  ok        {league:16} {series}")
            continue
        found, total, why = links(client, league, series)
        if found:
            print(f"  ok        {league:16} {series:26} {found} of {total} newest fixtures linked")
            findings["healthy"].append({"league": league, "series": series})
        else:
            print(f"  BROKEN    {league:16} {series:26} {why}")
            findings["broken"].append({"league": league, "series": series, "why": why})

    print("\n-- unmapped leagues, retried")
    for league, (series, why) in UNMAPPED.items():
        if only and league not in only:
            continue
        if series not in catalogue:
            print(f"  gone      {league:16} {series} is no longer listed; the entry can go")
            findings["dead"].append({"league": league, "series": series})
            continue
        if args.quick:
            continue
        found, total, fresh = links(client, league, series)
        if found:
            print(f"  PROMOTE   {league:16} {series:26} now links ({found} of {total}); was: {why}")
            findings["promotable"].append({"league": league, "series": series})
        else:
            print(f"  still no  {league:16} {series:26} {fresh or why}")

    print("\n-- seasonal leagues, retried")
    for league, (series, slug) in SEASONAL.items():
        if only and league not in only:
            continue
        if series not in catalogue:
            print(f"  gone      {league:16} {series} is no longer listed")
            findings["dead"].append({"league": league, "series": series})
            continue
        if args.quick:
            continue
        found, total, why = links(client, league, series, slug=slug)
        if found:
            print(f"  PROMOTE   {league:16} {series:26} in season and linking "
                  f"({found} of {total}) through {slug}")
            findings["promotable"].append({"league": league, "series": series, "slug": slug})
        else:
            print(f"  waiting   {league:16} {series:26} {why or 'no fixtures listed'}")

    # Series Kalshi lists that this repository has never had an opinion about.
    # The filter is what they price, not what they are called: two or three
    # markets an event, outcomes that are clubs, event tickers that carry a date.
    print("\n-- soccer series the table does not mention")
    known = (set(SERIES_BY_LEAGUE.values()) | {s for s, _ in UNMAPPED.values()}
             | {s for s, _ in SEASONAL.values()} | set(NOT_FIXTURES))
    for ticker, meta in sorted(catalogue.items()):
        if ticker in known or not ticker.endswith("GAME"):
            continue
        findings["new"].append({"series": ticker, "title": meta.get("title", "")})
        print(f"  NEW       {ticker:26} {meta.get('title', '')}")
    if not findings["new"]:
        print("  none")

    routed = [lg for lg in SERIES_BY_LEAGUE if lg not in COMPETITIONS]
    if routed:
        print(f"\n::error title=unrouted league::{', '.join(routed)} have a series "
              f"but no ESPN competition")

    print(f"\n{len(findings['healthy'])} healthy, {len(findings['broken'])} broken, "
          f"{len(findings['dead'])} dead, {len(findings['promotable'])} promotable, "
          f"{len(findings['new'])} unmentioned")
    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2) + "\n")
        print(f"wrote {args.json}")
    # Drift is news, not failure: the table is edited by a person who can say
    # why. Only an internally inconsistent table fails, because that one is a
    # bug in this repository rather than a change at a venue.
    return 1 if problems or routed else 0


if __name__ == "__main__":
    raise SystemExit(main())
