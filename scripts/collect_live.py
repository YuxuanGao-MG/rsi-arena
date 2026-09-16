"""Ask the harness about live markets, and keep every trace.

The replay benchmark scores a harness against a past it cannot see. This does
the other thing: it puts the same harness on matches being played now, keeps
what it said and how it got there, and scores it five minutes later when the
price prints. Nothing here feeds the gate — a live forecast is evidence about
the harness, not a promotion — but it is the only place the arena sees a market
it has not already read the end of.

Identifying the fixture is the part that goes wrong. Kalshi dates an event by
the day it listed the contract and the fixture feed by the day it kicked off,
and those differ often enough that a one-day lookup lost 168 settled fixtures
out of 654. Anything that cannot be identified is reported with the reason and
the candidates it was weighed against, never dropped quietly, and an invocation
that loses more than ``--max-unidentified`` of its open events exits non-zero:
the shape of this failure is a data gap that looks like a quiet evening.

Every invocation carries a spend ceiling, because it runs on a cron beside a
loop that costs thirty-five dollars a generation and it must not outgrow it.

    python scripts/collect_live.py --league EPL,LALIGA --minutes 120 --max-usd 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.harness.llm import OpenRouter                      # noqa: E402
from rsi_arena.harness.spec import Harness                        # noqa: E402
from rsi_arena.kalshi._client import KalshiClient                 # noqa: E402
from rsi_arena.kalshi._gamestate import todays_games              # noqa: E402
from rsi_arena.kalshi._history import History                     # noqa: E402
from rsi_arena.kalshi._linking import (harvest_team_codes,        # noqa: E402
                                       link_event, names_from_markets)
from rsi_arena.harness.runner import Runner                       # noqa: E402
from rsi_arena.kalshi.replay import (HORIZON_MINUTES,             # noqa: E402
                                     fresh_quote, live_tools,
                                     match_timeline, realised_mid)
from rsi_arena.topics.kalshi_horizon import score_output          # noqa: E402

#: Same spread the discovery script uses, for the same measured reason.
DATE_SPREAD = 3


def days_around(day: str) -> list[str]:
    from datetime import date as _date

    d = _date.fromisoformat(day)
    offsets = [0]
    for n in range(1, DATE_SPREAD + 1):
        offsets += [-n, n]
    return [(d + timedelta(days=n)).isoformat() for n in offsets]


#: One day of fixtures, fetched once per sweep.
#:
#: Identification asks for seven days around each event's ticker date, and a
#: league's events cluster on the same handful of days — without this, sixteen
#: LaLiga events cost 112 fetches of about a dozen distinct days.
_DAYS: dict[tuple[str, str], list[dict]] = {}


def fixtures_on(league: str, day: str) -> list[dict]:
    key = (league, day)
    if key not in _DAYS:
        try:
            _DAYS[key] = todays_games(league, day)
        except Exception:
            _DAYS[key] = []
    return _DAYS[key]


def live_events(client: KalshiClient, league: str) -> list[dict]:
    """Open events on this league's match-winner series."""
    series = f"KX{league.replace('_', '')}GAME"
    return list(client.paginate("/events", "events",
                                {"series_ticker": series, "status": "open"},
                                max_items=200))


def identify(client: KalshiClient, league: str, event: str,
             names: dict[str, str], codes: set[str]) -> tuple[str | None, str]:
    """The fixture behind an event, or None and why not.

    Never a silent drop. A market nobody can attach to a match is a market the
    harness would be forecasting blind, and the reason it could not be attached
    is the only thing that makes it fixable.
    """
    seen: list[str] = []

    def by_date(lg: str, day: str) -> list[dict]:
        out: list[dict] = []
        for probe in days_around(day):
            games = fixtures_on(lg, probe)
            out.extend(games)
            seen.extend(f"{g.get('away')} at {g.get('home')} ({probe})" for g in games)
        return out

    try:
        link = link_event(client, event, league, by_date,
                          series_ticker=f"KX{league.replace('_', '')}GAME",
                          names=names, codes=codes)
    except Exception as exc:
        return None, f"link raised {type(exc).__name__}: {exc}"
    if link is None:
        if not seen:
            # by_date is never reached when the ticker will not split into two
            # team codes, so an empty shortlist means the ticker, not the feed.
            return None, ("could not split the ticker into two team codes "
                          f"(series codes: {len(codes)})")
        shortlist = ", ".join(sorted(set(seen))[:6])
        return None, f"no fixture cleared the threshold; weighed against {shortlist}"
    return str(link.game_id), f"{link.method} at {link.confidence:.2f}"


async def one(harness: Harness, llm: OpenRouter, league: str, game: str,
              ticker: str, hist: History) -> dict:
    """One forecast on a live market, with the trace that produced it."""
    at = datetime.now(timezone.utc)
    candle = fresh_quote(hist, ticker, at)
    if candle is None or candle.mid is None:
        return {"ticker": ticker, "skipped": "no fresh two-sided quote"}
    line = match_timeline(league, game)
    state = line.state_at(at) if line else {}
    # live_tools, not replay_tools: the disk cache behind the frozen tools has
    # no TTL because settled history cannot change, which is not true of a book
    # that is still trading.
    box = live_tools(at, hist, line=line)

    run = await Runner(llm, box).run(harness, question=ticker,
                                     game=json.dumps(state)[:1200])
    return {"at": at.isoformat(), "league": league, "game_id": game, "ticker": ticker,
            "mid_now": candle.mid, "game": state, "harness": harness.name,
            "output": run.output, "run": run.to_dict(),
            "ok": run.ok, "error": run.error}


def resolve(row: dict, hist: History) -> bool:
    """Score a forecast once the horizon has printed. False while it has not.

    A live forecast is only worth keeping if it eventually meets a number, and
    the number arrives five minutes after the fact. Until then the row stays
    pending; it is never scored against the price it was given.
    """
    at = datetime.fromisoformat(row["at"])
    if datetime.now(timezone.utc) < at + timedelta(minutes=HORIZON_MINUTES + 1):
        return False
    realised = realised_mid(row["ticker"], at, HORIZON_MINUTES, hist)
    if realised is None:
        row["scored"] = None
        row["unscored_because"] = "no quote printed at the horizon"
        return True
    score = score_output(row.get("output"), row["mid_now"], realised)
    row["realised"] = realised
    row["scored"] = {"skill": round(score.skill, 4), "value": round(score.value, 4),
                     "error": round(score.error, 4), "naive_error": round(score.naive_error, 4)}
    return True


def write_resolved(pending: list[dict], hist: History, out: Path) -> list[dict]:
    """Write every forecast whose horizon has printed; keep the rest waiting."""
    still: list[dict] = []
    with out.open("a") as fh:
        for row in pending:
            if resolve(row, hist):
                fh.write(json.dumps(row, default=str) + "\n")
            else:
                still.append(row)
    return still


#: Where a GitHub Actions job writes what a person will read. Empty elsewhere,
#: and everything here still goes to stdout either way.
def report(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", default="EPL,LALIGA,SERIEA,BUNDESLIGA,LIGUE1,MLS,LIGAMX,EREDIVISIE")
    ap.add_argument("--harness", default="harnesses/horizon-5m.json")
    ap.add_argument("--minutes", type=int, default=120, help="how long to keep collecting")
    ap.add_argument("--poll", type=int, default=300, help="seconds between sweeps")
    ap.add_argument("--max-contracts", type=int, default=6, help="markets per sweep")
    ap.add_argument("--max-usd", type=float, default=3.0,
                    help="ceiling on this invocation's model spend; 0 removes it")
    ap.add_argument("--max-unidentified", type=float, default=0.10,
                    help="fail if more than this fraction of open events found no fixture")
    ap.add_argument("--out", default="runs/live/forecasts.jsonl")
    args = ap.parse_args()

    client, hist = KalshiClient(), History()
    harness = Harness.load(args.harness)
    # A ceiling per invocation, because this runs on a cron and nothing else
    # stops it. The evolution loop is thirty-five dollars a generation twice a
    # day and is the thing worth spending on; live collection is evidence
    # gathered alongside it and must not quietly outgrow it. Past the line the
    # client refuses to call out, the run is scored as silence, and the sweep
    # ends rather than filling the file with provider errors.
    llm = OpenRouter(budget_usd=args.max_usd or None)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    leagues = [x.strip().upper() for x in args.league.split(",") if x.strip()]
    catalogue: dict[str, tuple[set[str], dict[str, str]]] = {}
    deadline = time.time() + args.minutes * 60
    unidentified: dict[str, str] = {}
    seen_events: set[str] = set()
    pending: list[dict] = []
    made = 0

    try:
        while time.time() < deadline:
            # Cheap to refill and wrong to keep: a sweep an hour later has
            # fixtures the last one had not been told about.
            _DAYS.clear()
            targets: list[tuple[str, str, str]] = []
            for league in leagues:
                if league not in catalogue:
                    series = f"KX{league.replace('_', '')}GAME"
                    markets = list(client.paginate("/markets", "markets",
                                                   {"series_ticker": series}, max_items=4000))
                    catalogue[league] = (harvest_team_codes(client, series),
                                         names_from_markets(markets))
                codes, names = catalogue[league]
                for event in live_events(client, league):
                    ticker = event.get("event_ticker", "")
                    if not ticker:
                        continue
                    seen_events.add(ticker)
                    game, why = identify(client, league, ticker, names, codes)
                    if game is None:
                        if ticker not in unidentified:
                            unidentified[ticker] = why
                            print(f"  UNIDENTIFIED  {ticker}: {why}", file=sys.stderr)
                        continue
                    try:
                        markets = client.get(f"/events/{ticker}")["markets"]
                    except Exception:
                        continue
                    for m in markets[:2]:
                        if m.get("status") == "active":
                            targets.append((league, game, m["ticker"]))

            if not targets:
                print(f"  nothing live across {', '.join(leagues)}; waiting")
            for league, game, ticker in targets[:args.max_contracts]:
                if llm.over_budget:
                    break
                row = await one(harness, llm, league, game, ticker, hist)
                if row.get("skipped"):
                    continue
                pending.append(row)
                made += 1
                said = (row.get("output") or {}).get("delta_cents")
                print(f"  {ticker} at {row['mid_now']:.3f} -> {said:+.1f}c"
                      if said is not None else f"  {ticker}: no forecast")

            pending = write_resolved(pending, hist, out)
            if llm.over_budget:
                print(f"  ${llm.spent_usd:.2f} spent of ${args.max_usd:.2f}; stopping early")
                break
            await asyncio.sleep(args.poll)

        # The last sweep's forecasts have not met their horizon yet. Wait it out
        # rather than throw away work already paid for.
        while pending:
            await asyncio.sleep(30)
            pending = write_resolved(pending, hist, out)
    finally:
        await llm.close()

    linked = len(seen_events) - len(unidentified)
    missed = len(unidentified) / len(seen_events) if seen_events else 0.0
    print(f"\n{made} forecasts written to {out}, ${llm.spent_usd:.2f} spent")
    print(f"{linked}/{len(seen_events)} open events identified")

    lines = ["### Live collection",
             f"- {made} forecasts across {', '.join(leagues)}",
             f"- ${llm.spent_usd:.2f} spent" + (f" of ${args.max_usd:.2f}" if args.max_usd else ""),
             f"- identified {linked}/{len(seen_events)} open events ({missed:.0%} missed)"]
    for ticker, why in unidentified.items():
        # On stdout, where Actions reads its annotations: an event nobody could
        # attach to a match is a market the harness would forecast blind, and a
        # day where linking degrades has to be visible as something other than a
        # thinner file than yesterday's.
        print(f"::warning title=unidentified event::{ticker}: {why}")
        lines.append(f"  - `{ticker}` — {why}")
    report(lines)

    if seen_events and missed > args.max_unidentified:
        # Measured at 112 of 112 across eight leagues, so this is a regression
        # alarm and not an expectation. The failure it exists to catch is the
        # quiet one: a ticker format changes, a third of the events stop
        # linking, and the collection keeps running and looks merely slow.
        print(f"::error title=identification degraded::{len(unidentified)} of "
              f"{len(seen_events)} open events found no fixture ({missed:.0%}, "
              f"ceiling {args.max_unidentified:.0%})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
