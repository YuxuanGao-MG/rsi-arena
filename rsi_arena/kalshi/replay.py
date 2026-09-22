"""Put a harness back at a past instant and let it forecast into a known future.

A finished match has everything a five-minute forecast needs to be scored:
Kalshi serves the whole candlestick life of every market, and the fixture feed
publishes each goal and card of a completed game with the clock it happened on.
So an instant can be chosen, the market as of that instant reconstructed, and
the price five minutes later looked up, immediately and repeatably.

**Point-in-time is the whole contract.** Every read here is bounded at the
instant asked for. A leak, a candle from a minute later or a goal that had not
happened yet, silently turns the benchmark into a measure of hindsight.

Ported from ``topics/kalshi/eval/_replay.py`` in seantao97/rsi-arena, with a
disk cache added: history is immutable, so a tool answer at an instant never
changes and need not be fetched twice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..harness.tools import FunctionTool, Toolbox, ToolResult
from . import _gamestate as gs
from ._history import MINUTE, History
from ._taxonomy import resolve_league

HORIZON_MINUTES = 5

SCORING_KINDS = frozenset({"goal", "penalty---scored", "own-goal", "penalty-goal", "goal-penalty"})


@dataclass(frozen=True)
class MatchEvent:
    seconds: float
    kind: str
    team: str
    text: str

    @property
    def minute(self) -> int:
        return int(self.seconds // 60)

    @property
    def scored(self) -> bool:
        return self.kind in SCORING_KINDS


@dataclass
class MatchTimeline:
    """A finished match, reduced to what changes a price."""

    game_id: str
    league: str
    home: str
    away: str
    kickoff: datetime
    events: list[MatchEvent] = field(default_factory=list)

    def score_at(self, when: datetime) -> tuple[int, int, int]:
        elapsed = (when - self.kickoff).total_seconds()
        minute = max(0, int(elapsed // 60))
        home = away = 0
        for event in self.events:
            if not event.scored or event.seconds > elapsed:
                continue
            if event.team == self.home:
                home += 1
            else:
                away += 1
        return home, away, minute

    def final_score(self) -> tuple[int, int]:
        return self.score_at(self.kickoff + timedelta(days=1))[:2]

    def state_at(self, when: datetime) -> dict[str, Any]:
        """What a game-state tool would have said, had it been asked then.

        Three states, not two. ``score_at`` clamps the minute at zero, so before
        kickoff this used to report a match in progress at 0' — and a harness
        asked about Brentford against Chelsea two days early was told "kickoff
        just happened, 0-0" and reasoned from it, in those words, in a driver it
        wrote down. The replay benchmark never saw it because ``windows`` starts
        five minutes after kickoff; live collection sees almost nothing else,
        because most open markets are pre-match.
        """
        home, away, minute = self.score_at(when)
        elapsed = (when - self.kickoff).total_seconds()
        if elapsed < 0:
            # No clock and no period, because there is no clock and no period.
            # A key that is absent is read as absent; a key that says 0' is read
            # as a fact.
            return {
                "game_id": self.game_id, "league": self.league,
                "status": "scheduled", "home": self.home, "away": self.away,
                "home_score": None, "away_score": None,
                "minutes_to_kickoff": int(-elapsed // 60),
                "kickoff": self.kickoff.isoformat(),
                "recent_events": [],
            }
        recent = [e for e in self.events if e.seconds <= elapsed and elapsed - e.seconds <= 600]
        return {
            "game_id": self.game_id, "league": self.league,
            "status": "in_progress" if minute <= 100 else "final",
            "home": self.home, "away": self.away,
            "home_score": home, "away_score": away,
            "period": "1" if minute < 45 else "2", "clock": f"{minute}'",
            "recent_events": [f"{e.minute}' {e.kind}: {e.text[:90]}" for e in recent[-4:]],
        }

    def windows(self, every_minutes: int = 5, skip_first: int = 5,
                until_minute: int = 88) -> list[datetime]:
        """Instants worth forecasting from. Stops before the whistle."""
        out, minute = [], skip_first
        while minute <= until_minute:
            out.append(self.kickoff + timedelta(minutes=minute))
            minute += every_minutes
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"game_id": self.game_id, "league": self.league, "home": self.home,
                "away": self.away, "kickoff": self.kickoff.isoformat(),
                "events": [e.__dict__ for e in self.events]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MatchTimeline":
        return cls(game_id=d["game_id"], league=d["league"], home=d["home"], away=d["away"],
                   kickoff=datetime.fromisoformat(d["kickoff"]),
                   events=[MatchEvent(**e) for e in d.get("events", [])])


def match_timeline(league: str, game_id: str) -> MatchTimeline | None:
    """Reconstruct a finished match from the fixture feed's own record."""
    path = gs.ESPN_PATHS.get(resolve_league(league) or league.upper())
    if not path:
        return None
    sport, competition = path
    try:
        data = gs._get(f"{gs.ESPN_API}/{sport}/{competition}/summary?event={game_id}", throttle=True)
    except Exception:
        return None
    competitions = (data.get("header") or {}).get("competitions") or []
    if not competitions:
        return None
    comp = competitions[0]
    teams = {c.get("homeAway"): (c.get("team") or {}).get("displayName", "")
             for c in comp.get("competitors", [])}
    try:
        kickoff = datetime.fromisoformat(comp["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
    except (KeyError, ValueError):
        return None
    events: list[MatchEvent] = []
    for raw in data.get("keyEvents") or []:
        clock = (raw.get("clock") or {}).get("value")
        kind = ((raw.get("type") or {}).get("type") or "").lower()
        if clock is None or not kind:
            continue
        events.append(MatchEvent(seconds=float(clock), kind=kind,
                                 team=((raw.get("team") or {}).get("displayName") or ""),
                                 text=(raw.get("text") or "")))
    return MatchTimeline(game_id=str(game_id), league=league.upper(), home=teams.get("home", ""),
                         away=teams.get("away", ""), kickoff=kickoff,
                         events=sorted(events, key=lambda e: e.seconds))


#: Kalshi emits a candle only for minutes that saw activity. A candle older than
#: this before the instant asked for is a dead market, not a quote.
MAX_STALE_S = 180


def fresh_quote(history: History, ticker: str, at: datetime, max_stale_s: int = MAX_STALE_S):
    """The last candle at or before ``at``, but only if it is recent and two-sided."""
    candle = history.quote_at(ticker, at, MINUTE)
    if candle is None or not candle.two_sided or candle.mid is None:
        return None
    if (at - candle.ts).total_seconds() > max_stale_s:
        return None
    return candle


def realised_mid(ticker: str, at: datetime, minutes: int = HORIZON_MINUTES,
                 history: History | None = None, max_stale_s: int = MAX_STALE_S) -> float | None:
    """The mid the market printed ``minutes`` after ``at``, or None without a fresh two-sided book."""
    candle = fresh_quote(history or History(), ticker, at + timedelta(minutes=minutes), max_stale_s)
    return None if candle is None else candle.mid


class ToolCache:
    """Answers of frozen tools, on disk. History does not change.

    Keyed on ``{tool, at, **args}`` with no TTL and no eviction, which is sound
    exactly as far as that sentence is: a settled match's candles at a past
    instant are the same candles forever. On a market still trading it is false,
    and nothing in the key says so. Live callers take :data:`NO_CACHE`.
    """

    def __init__(self, root: str | Path | None) -> None:
        self.root = Path(root) if root else None

    def get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        path = self._path(key)
        if path is not None and path.exists():
            return json.loads(path.read_text())
        return None

    def put(self, key: dict[str, Any], value: dict[str, Any]) -> None:
        path = self._path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, default=str))

    def _path(self, key: dict[str, Any]) -> Path | None:
        if self.root is None:
            return None
        digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        return self.root / key.get("tool", "tool") / f"{digest}.json"


class _NoCache(ToolCache):
    """A cache that refuses to remember, for a market that is still moving.

    The live collector reached the same tools through the plain cache and was
    safe only by accident: ``at`` is ``datetime.now()`` to the microsecond, so
    two sweeps never landed on one key. Nobody chose that, nothing tested it,
    and one caller rounding its instant to the minute would have served a
    ten-minute-old book to a five-minute forecast. Refusing here is cheap;
    finding that bug in the traces afterwards would not be.
    """

    def __init__(self) -> None:
        super().__init__(None)

    def get(self, key: dict[str, Any]) -> dict[str, Any] | None:
        return None

    def put(self, key: dict[str, Any], value: dict[str, Any]) -> None:
        return None


#: Pass this rather than ``None`` when the instant is now. ``None`` also caches
#: nothing, but it reads as an omission; this reads as a decision.
NO_CACHE = _NoCache()


def live_tools(at: datetime, history: History | None = None, *,
               line: "MatchTimeline | None" = None) -> Toolbox:
    """:func:`replay_tools` at an instant that is happening, never cached.

    Same box the benchmark grades against — the point of live collection is that
    it is the same harness with the same primitives, differing only in that the
    future is not on disk yet.
    """
    return replay_tools(at, history, NO_CACHE, line=line)


def replay_tools(at: datetime, history: History | None = None,
                 cache: ToolCache | None = None,
                 line: "MatchTimeline | None" = None) -> Toolbox:
    """Every tool that can be replayed honestly, bounded at ``at``.

    Same names as a live toolbox would use, so a harness binds against either.
    Anything that reads news or live game state has no frozen form and is
    deliberately absent: a harness that names it fails to load.

    Seventeen of the forty live tools, and the other twenty-three are absent for
    three different reasons worth keeping straight.

    **Five would answer with the future.** `market_settlement` is the answer this
    benchmark scores against. `todays_fixtures`, `active_leagues`, `live_markets`
    and `find_game_for_market` resolve against the real today rather than the
    replayed one, so at any past instant they describe a world that had not
    happened yet. `similar_situations` reads how other contracts resolved, which
    includes matches played after this window.

    **Four read data that was never recorded.** Order book depth has no history
    at all — candlesticks carry the best bid and ask, not the book behind them —
    so `order_book` and `orderbook_imbalance` cannot be reconstructed at any
    price. `game_context` reads injuries and form as they stand now, and
    `sportsbook_line` a book's price now; neither feed keeps what it said an
    hour ago. `my_positions` is a live portfolio.

    **Two are external services**: `web_research` and `team_news` ask the web,
    and the web has since read the result.

    Game state is here when a timeline is supplied, and it is as replayable as
    the book: a timeline is timestamped events, so what the score was at an
    instant is a lookup rather than a guess. Without one those tools are absent
    rather than wrong, and a harness that names them fails to load.

    Several of these read history and stop at the instant. Others are
    arithmetic — fees, de-vigging, sizing, the mid a quote implies — and have no
    clock in them at all, so freezing is a matter of definition rather than
    care. They are here because a search over three tools is barely a search:
    the arena's premise is that a harness composes primitives, and until now it
    had almost nothing to compose.

    A last family is derived: the counting, subtraction and date maths a
    decisions model cannot do, done over the same frozen reads and handed back
    as numbers with a sentence. ``state_summary`` composes the others into one
    paragraph, which is the state such a model reads best.
    """
    hist = history or History()
    cache = cache or ToolCache(None)
    stamp = at.astimezone(timezone.utc).isoformat()

    def cached(tool: str, args: dict[str, Any], compute) -> ToolResult:
        key = {"tool": tool, "at": stamp, **args}
        hit = cache.get(key)
        if hit is not None:
            return ToolResult(ok=hit["ok"], text=hit["text"], data=hit["data"], error=hit.get("error"))
        out = compute()
        cache.put(key, {"ok": out.ok, "text": out.text, "data": out.data, "error": out.error})
        return out

    def quote(ticker: str) -> ToolResult:
        def compute() -> ToolResult:
            candle = hist.quote_at(ticker, at, MINUTE)
            if candle is None:
                return ToolResult.failed("no quote at that instant")
            out = {"ticker": ticker, "yes_bid": candle.yes_bid_close, "yes_ask": candle.yes_ask_close,
                   "mid": candle.mid, "spread": candle.spread, "last": candle.last,
                   "volume": candle.volume, "status": "active" if candle.two_sided else "no_book"}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("market_quote", {"ticker": ticker}, compute)

    def path(ticker: str, hours_back: float = 0.75, hourly: bool = False) -> ToolResult:
        hours = max(0.1, float(hours_back))
        def compute() -> ToolResult:
            start = at - timedelta(hours=hours)
            bars = [{"ts": c.ts.isoformat(), "mid": c.mid, "last": c.last, "volume": c.volume}
                    for c in hist.price_path(ticker, start, at, MINUTE) if c.ts <= at]
            return ToolResult(ok=True, text=json.dumps(bars, default=str), data={"bars": bars})
        return cached("candlesticks", {"ticker": ticker, "hours_back": hours}, compute)

    def tape(ticker: str, limit: int = 12) -> ToolResult:
        n = int(limit)
        def compute() -> ToolResult:
            # Bounded by the API's max_ts, never by trimming afterwards: trades
            # come newest-first over the market's whole life, and trimming would
            # hand back prints from after the instant, the exact leak to avoid.
            recent = hist.trades(ticker, start=at - timedelta(hours=1), end=at, max_trades=max(n, 50))
            trades = [{"ts": t.get("created_time"), "yes_price": t.get("yes_price_dollars"),
                       "count": t.get("count_fp"), "taker_side": t.get("taker_side")}
                      for t in recent[:n]]
            return ToolResult(ok=True, text=json.dumps(trades, default=str), data={"trades": trades})
        return cached("previous_trades", {"ticker": ticker, "limit": n}, compute)

    # -- arithmetic: no I/O, no clock, so nothing to freeze ------------------

    def fees(price: float, contracts: int = 100) -> ToolResult:
        from ._fees import breakeven, maker_fee, taker_fee

        p = max(0.01, min(0.99, float(price)))
        n = max(1, int(contracts))
        out = {"price": p, "contracts": n,
               "taker_fee_usd": round(taker_fee(p, n), 4),
               "maker_fee_usd": round(maker_fee(p, n), 4),
               "round_trip_usd": round(taker_fee(p, n) * 2, 4),
               "breakeven_move": round(breakeven(p), 4)}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    def edge(probability: float, yes_price: float, bankroll: float = 1000.0) -> ToolResult:
        from ._fees import edge as net_edge, kelly

        prob, price = float(probability), float(yes_price)
        if not (0 <= prob <= 1 and 0 < price < 1):
            return ToolResult.failed("probability and yes_price must be between 0 and 1")
        after = net_edge(prob, price)
        fraction = kelly(prob, price)
        out = {"probability": prob, "yes_price": price,
               "edge_after_fees": round(after, 5),
               "kelly_fraction": round(fraction, 5),
               "stake_usd": round(max(0.0, fraction) * float(bankroll), 2),
               "worth_taking": after > 0}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    def devig(american_odds: list, method: str = "proportional") -> ToolResult:
        from ._implied import fair_probabilities, overround

        try:
            odds = [float(x) for x in american_odds]
        except (TypeError, ValueError):
            return ToolResult.failed("american_odds must be numbers, e.g. [-150, 320]")
        if len(odds) < 2:
            return ToolResult.failed("a book needs at least two outcomes to de-vig")
        try:
            fair = fair_probabilities(odds, method=method)
        except ValueError as exc:
            # A model guessing a method name should read what the names are, not
            # a traceback. "multiplicative" was this author's guess and wrong.
            return ToolResult.failed(f"{exc}; use 'proportional' or 'power'")
        out = {"american_odds": odds, "fair": [round(x, 5) for x in fair],
               "overround": round(overround(odds), 5), "method": method}
        return ToolResult(ok=True, text=json.dumps(out), data=out)

    def against_book(kalshi_price: float, book_odds: list, index: int = 0) -> ToolResult:
        from ._implied import kalshi_vs_book as compare

        try:
            odds = [float(x) for x in book_odds]
        except (TypeError, ValueError):
            return ToolResult.failed("book_odds must be numbers, e.g. [-150, 320]")
        if len(odds) < 2:
            return ToolResult.failed("a book needs at least two outcomes")
        if not 0 < float(kalshi_price) < 1:
            return ToolResult.failed("kalshi_price is a probability between 0 and 1")
        try:
            out = compare(float(kalshi_price), odds, index=int(index))
        except (ValueError, IndexError) as exc:
            return ToolResult.failed(f"{type(exc).__name__}: {exc}")
        return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)

    # -- history, stopped at the instant -------------------------------------

    def at_time(ticker: str, when: str) -> ToolResult:
        """The book at an earlier instant. Never later than this window's."""
        def compute() -> ToolResult:
            try:
                asked = datetime.fromisoformat(str(when).replace("Z", "+00:00"))
            except ValueError:
                return ToolResult.failed(f"{when!r} is not an ISO instant")
            if asked.tzinfo is None:
                asked = asked.replace(tzinfo=timezone.utc)
            # The one place a harness could reach forward by asking, so the
            # answer is clamped rather than refused: a plan that asks for a time
            # past the window gets the window, and gets told.
            capped = min(asked, at)
            candle = hist.quote_at(ticker, capped, MINUTE)
            if candle is None:
                return ToolResult.failed(f"no quote on {ticker} at {capped.isoformat()}")
            out = {"ticker": ticker, "asked_for": asked.isoformat(),
                   "answered_at": capped.isoformat(), "clamped": capped < asked,
                   "yes_bid": candle.yes_bid_close, "yes_ask": candle.yes_ask_close,
                   "mid": candle.mid, "last": candle.last, "volume": candle.volume}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("market_at_time", {"ticker": ticker, "when": str(when)}, compute)

    def volume(ticker: str, hours_back: float = 3.0) -> ToolResult:
        """Where the contract traded, by price, over a window ending now."""
        def compute() -> ToolResult:
            start = at - timedelta(hours=max(0.1, float(hours_back)))
            bars = [c for c in hist.price_path(ticker, start, at, MINUTE) if c.ts <= at]
            traded = [c for c in bars if c.volume]
            if not traded:
                return ToolResult.failed(f"nothing traded on {ticker} in that window")
            total = sum(c.volume for c in traded)
            buckets: dict[str, float] = {}
            for c in traded:
                if c.mid is None:
                    continue
                buckets[f"{round(c.mid, 2):.2f}"] = buckets.get(f"{round(c.mid, 2):.2f}", 0.0) + c.volume
            heaviest = max(buckets.items(), key=lambda kv: kv[1], default=("", 0.0))
            out = {"ticker": ticker, "hours_back": float(hours_back), "volume": round(total, 2),
                   "bars_traded": len(traded), "by_price": {k: round(v, 2) for k, v in
                                                            sorted(buckets.items())},
                   "heaviest_price": heaviest[0], "heaviest_volume": round(heaviest[1], 2)}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("volume_profile", {"ticker": ticker, "hours_back": float(hours_back)}, compute)

    # -- the match, as the timeline recorded it ------------------------------

    def game_now() -> ToolResult:
        if line is None:
            return ToolResult.failed("no timeline for this window")
        out = line.state_at(at)
        return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)

    def since_goal() -> ToolResult:
        """How long the match has been quiet. Almost everything that moves a
        soccer price is a goal, so this is the closest thing to a clock on the
        risk."""
        if line is None:
            return ToolResult.failed("no timeline for this window")
        elapsed = (at - line.kickoff).total_seconds()
        scored = [e for e in line.events if e.seconds <= elapsed and e.scored]
        minute = int(elapsed // 60)
        out = {"minute": minute, "goals": len(scored),
               "last_goal_minute": scored[-1].minute if scored else None,
               "minutes_since": (minute - scored[-1].minute) if scored else None,
               "score": f"{line.away_score_at(at)}-{line.home_score_at(at)}"
                        if hasattr(line, "away_score_at") else None}
        return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)

    def plays(limit: int = 6) -> ToolResult:
        if line is None:
            return ToolResult.failed("no timeline for this window")
        elapsed = (at - line.kickoff).total_seconds()
        seen = [e for e in line.events if e.seconds <= elapsed]
        rows = [{"minute": e.minute, "kind": e.kind, "text": e.text[:120]}
                for e in seen[-int(limit):]]
        return ToolResult(ok=True, text=json.dumps(rows, default=str), data={"events": rows})

    # -- more of the book's history ------------------------------------------

    def velocity(ticker: str) -> ToolResult:
        """How fast this contract is moving, against its own recent normal."""
        def compute() -> ToolResult:
            bars = [c for c in hist.price_path(ticker, at - timedelta(hours=1), at, MINUTE)
                    if c.ts <= at and c.mid is not None]
            if len(bars) < 4:
                return ToolResult.failed(f"too few bars on {ticker} in the last hour")
            mids = [c.mid for c in bars]
            steps = [abs(b - a) for a, b in zip(mids, mids[1:])]
            typical = sorted(steps)[len(steps) // 2] if steps else 0.0
            def move(n: int) -> float:
                return (mids[-1] - mids[-min(n + 1, len(mids))]) * 100
            out = {"ticker": ticker, "mid": mids[-1], "bars": len(bars),
                   "move_1m": round(move(1), 2), "move_3m": round(move(3), 2),
                   "move_5m": round(move(5), 2),
                   "typical_minute_move_cents": round(typical * 100, 2),
                   "verdict": ("running" if abs(move(5)) > 400 * typical + 1
                               else "moving" if abs(move(5)) > 200 * typical + 0.5
                               else "still")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("price_velocity", {"ticker": ticker}, compute)

    def shock(ticker: str, minutes_back: int = 30,
              threshold_cents: float = 5.0) -> ToolResult:
        """Jumps in the recent path, and how much of each came back."""
        def compute() -> ToolResult:
            start = at - timedelta(minutes=max(2, int(minutes_back)))
            bars = [c for c in hist.price_path(ticker, start, at, MINUTE)
                    if c.ts <= at and c.mid is not None]
            if len(bars) < 3:
                return ToolResult.failed(f"too few bars on {ticker} in that window")
            latest = bars[-1].mid
            jumps = []
            for before, after in zip(bars, bars[1:]):
                move = (after.mid - before.mid) * 100
                if abs(move) < float(threshold_cents):
                    continue
                retraced = 0.0 if move == 0 else max(0.0, min(1.0, (after.mid - latest) / (after.mid - before.mid)))
                jumps.append({"at": after.ts.isoformat(), "move_cents": round(move, 1),
                              "from": before.mid, "to": after.mid,
                              "retraced": round(retraced, 3)})
            if not jumps:
                return ToolResult(ok=True, text=f"no move over {threshold_cents}c on {ticker}",
                                  data={"ticker": ticker, "jumps": [], "mid": latest})
            jumps.sort(key=lambda j: -abs(j["move_cents"]))
            out = {"ticker": ticker, "mid": latest, "jumps": jumps[:6],
                   "largest": jumps[0]}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("market_shock", {"ticker": ticker, "minutes_back": int(minutes_back),
                                       "threshold_cents": float(threshold_cents)}, compute)

    def rules(ticker: str) -> ToolResult:
        """What actually settles this contract.

        Published before the match, so reading it at any instant is reading
        something already fixed — which is what makes it replayable at all.
        """
        def compute() -> ToolResult:
            try:
                terms = hist.rules(ticker)
            except Exception as exc:
                return ToolResult.failed(f"no rules for {ticker}: {type(exc).__name__}")
            if not terms:
                return ToolResult.failed(f"{ticker} publishes no settlement terms")
            return ToolResult(ok=True, text=json.dumps(terms, default=str)[:1500], data=terms)
        return cached("market_rules", {"ticker": ticker}, compute)

    def countdown(ticker: str) -> ToolResult:
        """How long this contract has left. The close time is published in
        advance, so it is as knowable at the instant as afterwards."""
        def compute() -> ToolResult:
            try:
                terms = hist.rules(ticker) or {}
            except Exception as exc:
                return ToolResult.failed(f"no market {ticker}: {type(exc).__name__}")
            close = terms.get("close_time")
            if not close:
                return ToolResult.failed(f"{ticker} publishes no close time")
            try:
                closes = datetime.fromisoformat(str(close).replace("Z", "+00:00"))
            except ValueError:
                return ToolResult.failed(f"{close!r} is not an instant")
            out = {"ticker": ticker, "close_time": closes.isoformat(),
                   "minutes_left": round((closes - at).total_seconds() / 60, 1),
                   "settles_on": str(terms.get("rules_primary") or "")[:300]}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("settlement_countdown", {"ticker": ticker}, compute)

    def siblings(ticker: str) -> ToolResult:
        """The other contracts on this fixture, priced at this instant.

        Their prices have to be consistent with each other — two sides of one
        match cannot both be likely — and an inconsistency is the one edge that
        needs no view on the game at all.
        """
        def compute() -> ToolResult:
            event = ticker.rsplit("-", 1)[0]
            try:
                card = hist.event_history(event, start=at - timedelta(minutes=5), end=at,
                                          interval=MINUTE)
            except Exception as exc:
                return ToolResult.failed(f"no card for {event}: {type(exc).__name__}")
            rows = []
            for other, candles in sorted(card.items()):
                usable = [c for c in candles if c.ts <= at and c.mid is not None]
                if usable:
                    last = usable[-1]
                    rows.append({"ticker": other, "mid": last.mid,
                                 "yes_bid": last.yes_bid_close, "yes_ask": last.yes_ask_close})
            if not rows:
                return ToolResult.failed(f"nothing on {event} was quoted at that instant")
            total = sum(r["mid"] for r in rows)
            out = {"event": event, "markets": rows, "sum_of_mids": round(total, 4),
                   "overround": round(total - 1.0, 4),
                   "note": ("prices sum above one, so the book carries margin"
                            if total > 1.01 else
                            "prices sum below one, which is a gap rather than a margin"
                            if total < 0.99 else "prices are coherent")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("coherence_check", {"ticker": ticker}, compute)

    # -- derived: the arithmetic a decisions model cannot do ------------------
    #
    # Jev answers typed questions about a state and does no counting, no
    # subtraction and no date maths. Handing it forty minute bars is handing
    # it nothing; handing it "moved 3c in the last five minutes, buyers on the
    # tape, 12' since the goal" is a state. Each of these reads the same frozen
    # history the primitives read, bounded at the instant the same way, and
    # gives back numbers and a sentence.

    def bars_before(ticker: str, hours: float):
        return [c for c in hist.price_path(ticker, at - timedelta(hours=hours), at, MINUTE)
                if c.ts <= at and c.mid is not None]

    def clock() -> ToolResult:
        """Minutes played and left, from the timeline's kickoff and the instant."""
        if line is None:
            return ToolResult.failed("no timeline for this window")

        def compute() -> ToolResult:
            elapsed = (at - line.kickoff).total_seconds()
            if elapsed < 0:
                out = {"status": "scheduled", "minutes_to_kickoff": int(-elapsed // 60)}
                return ToolResult(ok=True, text=json.dumps(out), data=out)
            minute = int(elapsed // 60)
            scored = [e for e in line.events if e.scored and e.seconds <= elapsed]
            since = (minute - scored[-1].minute) if scored else None
            out = {"minutes_played": minute, "minutes_left_to_90": max(0, 90 - minute),
                   "period": "1" if minute < 45 else "2",
                   "minutes_since_score_change": since,
                   "stoppage_near": minute >= 85,
                   "verdict": (f"{minute}' played, {max(0, 90 - minute)} to the ninetieth"
                               + (f", {since} since the last goal" if since is not None
                                  else ", no goal yet")
                               + (", stoppage near" if minute >= 85 else ""))}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("game_clock", {"game": line.game_id}, compute)

    def base_rate(ticker: str) -> ToolResult:
        """What five-minute moves have looked like at this price, from this contract's own past."""
        def compute() -> ToolResult:
            now = hist.quote_at(ticker, at, MINUTE)
            if now is None or now.mid is None:
                return ToolResult.failed(f"no quote on {ticker} at that instant")
            bucket = _bucket(now.mid)
            # A window starts strictly before the instant and may end on it:
            # the bar at ``at`` is known, the one after it is the answer.
            bars = bars_before(ticker, 3.0)
            horizon = timedelta(minutes=HORIZON_MINUTES)
            moves: list[float] = []
            for i, c in enumerate(bars):
                target = c.ts + horizon
                if c.ts >= at or target > at or _bucket(c.mid) != bucket:
                    continue
                # The last bar at or before the horizon, never one after it.
                later = None
                for candidate in bars[i:]:
                    if candidate.ts > target:
                        break
                    later = candidate
                if later is None or later is c:
                    continue
                moves.append((later.mid - c.mid) * 100)
            if not moves:
                out = {"ticker": ticker, "bucket": bucket, "samples": 0, "mean_cents": None,
                       "p10": None, "p50": None, "p90": None, "moved_share": None,
                       "verdict": f"no five-minute history at {bucket} on this contract"}
                return ToolResult(ok=True, text=json.dumps(out), data=out)
            moves.sort()
            moved = sum(1 for m in moves if abs(m) >= 1.0) / len(moves)
            out = {"ticker": ticker, "bucket": bucket, "samples": len(moves),
                   "mean_cents": round(sum(moves) / len(moves), 2),
                   "p10": round(_quantile(moves, 0.1), 2), "p50": round(_quantile(moves, 0.5), 2),
                   "p90": round(_quantile(moves, 0.9), 2), "moved_share": round(moved, 3),
                   "verdict": (f"{'active' if moved >= 0.5 else 'quiet'}: {moved:.0%} of "
                               f"{len(moves)} five-minute windows at {bucket} moved a cent or more")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("move_base_rate", {"ticker": ticker}, compute)

    def imbalance(ticker: str, minutes_back: int = 10) -> ToolResult:
        """Who has been hitting the book: contracts taken on the yes side against the no side."""
        back = max(1, int(minutes_back))

        def compute() -> ToolResult:
            since = at - timedelta(minutes=back)
            prints = hist.trades(ticker, start=since, end=at, max_trades=200)
            rows = []
            for t in prints:
                ts = _instant(t.get("created_time"))
                # Bounded by the API's end, and checked again here: a print
                # from after the instant is the exact leak this box exists
                # to refuse, and a fake or a gateway may not honour the bound.
                if ts is not None and (ts > at or ts < since):
                    continue
                try:
                    count = float(t.get("count_fp") or t.get("count") or 0)
                except (TypeError, ValueError):
                    count = 0.0
                rows.append((str(t.get("taker_side") or ""), count))
            yes = sum(n for side, n in rows if side == "yes")
            no = sum(n for side, n in rows if side == "no")
            total = yes + no
            ratio = (yes - no) / total if total else 0.0
            verdict = "buyers" if ratio > 0.2 else "sellers" if ratio < -0.2 else "balanced"
            out = {"ticker": ticker, "minutes_back": back, "prints": len(rows),
                   "yes_taken": round(yes, 2), "no_taken": round(no, 2),
                   "imbalance": round(ratio, 3),
                   "largest_print": round(max((n for _, n in rows), default=0.0), 2),
                   "verdict": (f"{verdict}: {len(rows)} prints in {back}m, {yes:.0f} yes against "
                               f"{no:.0f} no")}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("tape_imbalance", {"ticker": ticker, "minutes_back": back}, compute)

    def absorption(ticker: str) -> ToolResult:
        """How much of the last goal the price has taken in, and whether it is still moving."""
        if line is None:
            return ToolResult.failed("no timeline for this window")

        def compute() -> ToolResult:
            elapsed = (at - line.kickoff).total_seconds()
            scored = [e for e in line.events if e.scored and e.seconds <= elapsed]
            if not scored:
                out = {"ticker": ticker, "note": "no goal yet"}
                return ToolResult(ok=True, text=json.dumps(out), data=out)
            goal = scored[-1]
            when = min(line.kickoff + timedelta(seconds=goal.seconds), at)
            then = hist.quote_at(ticker, when, MINUTE)
            now = hist.quote_at(ticker, at, MINUTE)
            if now is None or now.mid is None:
                return ToolResult.failed(f"no quote on {ticker} at that instant")
            mid_then = then.mid if then is not None else None
            recent = bars_before(ticker, 0.25)[-4:]
            drift = (recent[-1].mid - recent[0].mid) * 100 if len(recent) >= 2 else 0.0
            move = (now.mid - mid_then) * 100 if mid_then is not None else None
            out = {"ticker": ticker, "goal_minute": goal.minute, "goal_by": goal.team,
                   "minutes_since": int(elapsed // 60) - goal.minute,
                   "mid_at_goal": mid_then, "mid_now": now.mid,
                   "move_since_cents": round(move, 2) if move is not None else None,
                   "drift_last_3_bars_cents": round(drift, 2),
                   "still_drifting": abs(drift) >= 1.0,
                   "verdict": ((f"moved {move:+.1f}c since the {goal.minute}' goal"
                                if move is not None else f"no quote at the {goal.minute}' goal")
                               + (", still drifting" if abs(drift) >= 1.0 else ", settled"))}
            return ToolResult(ok=True, text=json.dumps(out, default=str), data=out)
        return cached("goal_absorption", {"ticker": ticker, "game": line.game_id}, compute)

    def summary(ticker: str) -> ToolResult:
        """One paragraph, the shape a decisions model reads best. Built through
        the other tools rather than from fresh reads, so it shares their cache
        and cannot disagree with what they said."""
        def compute() -> ToolResult:
            q = quote(ticker)
            if not q.ok:
                return ToolResult.failed(q.error or "no quote")
            book = q.data
            bars = bars_before(ticker, 0.25)[-5:]
            parts = [f"{ticker} mid {_cents(book['mid'])}c, bid {_cents(book['yes_bid'])} ask "
                     f"{_cents(book['yes_ask'])} ({book['status']})."]
            if len(bars) >= 2:
                path = "/".join(_cents(c.mid) for c in bars)
                parts.append(f"Last {len(bars)} bars {path}c, net {(bars[-1].mid - bars[0].mid) * 100:+.1f}c.")
            tape_now = imbalance(ticker)
            if tape_now.ok:
                parts.append(f"Tape: {tape_now.data['verdict']}.")
            if line is not None:
                clk = clock()
                if clk.ok and "verdict" in clk.data:
                    parts.append(f"Clock: {clk.data['verdict']}.")
                goal = absorption(ticker)
                if goal.ok and "verdict" in goal.data:
                    parts.append(f"Goal: {goal.data['verdict']}.")
            rate = base_rate(ticker)
            if rate.ok:
                parts.append(f"Base rate: {rate.data['verdict']}.")
            text = " ".join(parts)
            if len(text) > 600:
                text = text[:597].rstrip() + "..."
            out = {"ticker": ticker, "summary": text, "mid": book["mid"]}
            return ToolResult(ok=True, text=text, data=out)
        return cached("state_summary", {"ticker": ticker, "game": line.game_id if line else None},
                      compute)

    return Toolbox([
        FunctionTool(name="market_quote",
                     description="The book on one contract as of now: bid, ask, mid, spread, last, volume.",
                     parameters={"type": "object", "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=quote),
        FunctionTool(name="candlesticks",
                     description="Minute bars for one contract up to now. hours_back defaults to 0.75.",
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"},
                                                "hours_back": {"type": "number"},
                                                "hourly": {"type": "boolean"}},
                                 "required": ["ticker"]},
                     fn=path),
        FunctionTool(name="previous_trades",
                     description="The print tape for one contract, newest first, from just before now.",
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"},
                                                "limit": {"type": "integer"}},
                                 "required": ["ticker"]},
                     fn=tape),
        FunctionTool(name="market_at_time",
                     description=("The book on one contract at an earlier instant. Times after "
                                  "now are answered with now and flagged, never refused."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"},
                                                "when": {"type": "string",
                                                         "description": "ISO 8601 UTC"}},
                                 "required": ["ticker", "when"]},
                     fn=at_time),
        FunctionTool(name="volume_profile",
                     description=("Where one contract traded, by price, over the hours before "
                                  "now. Shows the price the book keeps coming back to."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"},
                                                "hours_back": {"type": "number"}},
                                 "required": ["ticker"]},
                     fn=volume),
        FunctionTool(name="trading_fees",
                     description=("What a trade costs at a price: taker, maker, the round trip, "
                                  "and how far the price must move to break even. The fee peaks "
                                  "at 0.50 and a round trip is two to three cents."),
                     parameters={"type": "object",
                                 "properties": {"price": {"type": "number"},
                                                "contracts": {"type": "integer"}},
                                 "required": ["price"]},
                     fn=fees),
        FunctionTool(name="price_the_edge",
                     description=("A probability and a price into an edge after fees, a Kelly "
                                  "fraction and a stake. Says whether anything is left."),
                     parameters={"type": "object",
                                 "properties": {"probability": {"type": "number"},
                                                "yes_price": {"type": "number"},
                                                "bankroll": {"type": "number"}},
                                 "required": ["probability", "yes_price"]},
                     fn=edge),
        FunctionTool(name="devig_odds",
                     description=("American odds into probabilities that sum to one, with the "
                                  "book's margin reported separately."),
                     parameters={"type": "object",
                                 "properties": {"american_odds": {"type": "array",
                                                                  "items": {"type": "number"}},
                                                "method": {"type": "string"}},
                                 "required": ["american_odds"]},
                     fn=devig),
        FunctionTool(name="kalshi_vs_book",
                     description=("One Kalshi price against a de-vigged sportsbook line: the "
                                  "gap, and which side it favours."),
                     parameters={"type": "object",
                                 "properties": {"kalshi_price": {"type": "number"},
                                                "book_odds": {"type": "array",
                                                              "items": {"type": "number"}},
                                                "index": {"type": "integer"}},
                                 "required": ["kalshi_price", "book_odds"]},
                     fn=against_book),
        FunctionTool(name="price_velocity",
                     description=("How fast this contract is moving, against its own normal "
                                  "over the last hour. Says still, moving or running."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=velocity),
        FunctionTool(name="market_shock",
                     description=("Jumps in the recent path and how much of each came back. "
                                  "A jump that holds reads as information; one that snaps "
                                  "back was a thin book."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"},
                                                "minutes_back": {"type": "integer"},
                                                "threshold_cents": {"type": "number"}},
                                 "required": ["ticker"]},
                     fn=shock),
        FunctionTool(name="market_rules",
                     description=("What settles this contract, as the exchange wrote it. "
                                  "Published before the match, so it is fixed."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=rules),
        FunctionTool(name="settlement_countdown",
                     description=("How many minutes this contract has left, and what decides "
                                  "it. A 'will happen' contract decays toward no as that "
                                  "number falls."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=countdown),
        FunctionTool(name="coherence_check",
                     description=("Every contract on this fixture, priced at this instant, "
                                  "with what their prices sum to. Two sides of one match "
                                  "cannot both be likely, and a gap needs no view on the game."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=siblings),
        FunctionTool(name="move_base_rate",
                     description=("How five-minute moves at this price have looked on this "
                                  "contract over the last three hours: count, mean, p10/p50/p90 "
                                  "in cents, the share that moved a cent or more, and whether "
                                  "that reads quiet or active."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=base_rate),
        FunctionTool(name="tape_imbalance",
                     description=("Who has been hitting the book over the last minutes: "
                                  "contracts taken yes against no, an imbalance from -1 to 1, "
                                  "the print count and the largest print. Says buyers, "
                                  "sellers or balanced."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"},
                                                "minutes_back": {"type": "integer"}},
                                 "required": ["ticker"]},
                     fn=imbalance),
        FunctionTool(name="state_summary",
                     description=("One paragraph of the whole situation: the book, the last "
                                  "five bars, the tape, the clock and the goal if there is a "
                                  "timeline, and the base rate. Under 600 characters; the "
                                  "state a decisions model reads best."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=summary),
    ] + ([] if line is None else [
        FunctionTool(name="game_clock",
                     description=("Minutes played and left to ninety, the period, minutes since "
                                  "the score last changed, and whether stoppage is near. The "
                                  "date maths done, for a model that cannot do it."),
                     parameters={"type": "object", "properties": {}},
                     fn=clock),
        FunctionTool(name="goal_absorption",
                     description=("How far the price has moved since the last goal and whether "
                                  "it is still drifting over the last three bars, or a note "
                                  "that there has been no goal."),
                     parameters={"type": "object",
                                 "properties": {"ticker": {"type": "string"}},
                                 "required": ["ticker"]},
                     fn=absorption),
        FunctionTool(name="game_state",
                     description=("Score, period and clock as of now, with the events of the "
                                  "last ten minutes."),
                     parameters={"type": "object", "properties": {}},
                     fn=game_now),
        FunctionTool(name="minutes_since_goal",
                     description=("How long the match has been quiet. Almost everything that "
                                  "moves a soccer price is a goal, so this is the closest "
                                  "thing to a clock on the risk."),
                     parameters={"type": "object", "properties": {}},
                     fn=since_goal),
        FunctionTool(name="recent_plays",
                     description="The match events so far, newest last.",
                     parameters={"type": "object",
                                 "properties": {"limit": {"type": "integer"}}},
                     fn=plays),
    ]))


#: Price buckets for the base rate. A contract near a cent or near a dollar
#: has almost nowhere to go, and one in the middle has everywhere, so a
#: move's typical size is a function of where the price sits.
_BUCKETS: tuple[tuple[float, str], ...] = ((0.10, "<10c"), (0.30, "10-30c"), (0.70, "30-70c"),
                                           (0.90, "70-90c"))


def _bucket(mid: float) -> str:
    for edge, label in _BUCKETS:
        if mid < edge:
            return label
    return ">90c"


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    pos = q * (len(sorted_values) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _instant(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _cents(price: float | None) -> str:
    return "?" if price is None else f"{price * 100:.0f}"
