"""Kalshi and fixture data, and tools frozen at a past instant.

The underscore modules are the data layer copied from
``seantao97/rsi-arena/topics/kalshi/tools`` (Apache-2.0, authored by
YuxuanGao-MG). They depend on the standard library only. ``replay`` is the
part written here: the three replayable tools and the match timeline.
"""

from __future__ import annotations

from ._client import KalshiClient
from ._credentials import Credentials, load as load_credentials
from ._fees import breakeven, clv, edge, fee, kelly, maker_fee, taker_fee
from ._gamestate import GameState, game_state, todays_games
from ._history import DAY, HOUR, MINUTE, Candle, History
from ._quotes import OrderBook, Quote, Quotes
from ._taxonomy import COMPETITIONS, resolve_league
from .replay import (HORIZON_MINUTES, MAX_STALE_S, NO_CACHE, MatchEvent, MatchTimeline,
                     ToolCache, fresh_quote, live_tools, match_timeline, realised_mid,
                     replay_tools)

__all__ = [
    "KalshiClient", "Credentials", "load_credentials",
    "breakeven", "clv", "edge", "fee", "kelly", "maker_fee", "taker_fee",
    "GameState", "game_state", "todays_games",
    "DAY", "HOUR", "MINUTE", "Candle", "History",
    "OrderBook", "Quote", "Quotes", "COMPETITIONS", "resolve_league",
    "HORIZON_MINUTES", "MAX_STALE_S", "NO_CACHE", "MatchEvent", "MatchTimeline", "ToolCache",
    "fresh_quote", "live_tools", "match_timeline", "realised_mid", "replay_tools",
]
