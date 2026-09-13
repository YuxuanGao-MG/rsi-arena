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
        """What a game-state tool would have said, had it been asked then."""
        home, away, minute = self.score_at(when)
        elapsed = (when - self.kickoff).total_seconds()
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
    """Answers of frozen tools, on disk. History does not change."""

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


def replay_tools(at: datetime, history: History | None = None,
                 cache: ToolCache | None = None) -> Toolbox:
    """The three replayable tools, bounded at ``at``.

    Same names as a live toolbox would use, so a harness binds against either.
    Anything that reads news or live game state has no frozen form and is
    deliberately absent: a harness that names it fails to load.
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
    ])
