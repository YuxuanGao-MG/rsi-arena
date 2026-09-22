"""Alpaca market data - IEX minute bars, prints and Benzinga news - and the
tools frozen at the second a story broke.

The underscore modules are the data layer: a read-only ``httpx`` client that
reads its keys late, bars with the completed-bar rule written once, news
with the edited-after flag, and the New York session. ``replay`` is the
frozen toolbox, the same discipline as ``kalshi/replay.py`` on a venue that
prints in dollars and is scored in basis points.
"""

from __future__ import annotations

from ._bars import (BAR_S, HORIZON_MINUTES, MAX_STALE_S, AlpacaBars, Bar, BarStore, bar_at_or_before,
                    known_bars, last_complete_bar, parse_instant, rfc3339, stale_seconds)
from ._client import DATA_BASE, KEY_ENV, PER_MINUTE, SECRET_ENV, AlpacaData, MissingCredentials
from ._news import AlpacaNews, NewsItem
from ._session import NY, in_window, is_weekday, ny_date, prior_weekdays, session, session_close, session_open, to_ny
from .replay import LOOKBACK_SESSIONS, TICK_BPS, live_tools, replay_tools

__all__ = [
    "AlpacaData", "MissingCredentials", "DATA_BASE", "KEY_ENV", "SECRET_ENV", "PER_MINUTE",
    "Bar", "BarStore", "AlpacaBars", "known_bars", "last_complete_bar", "bar_at_or_before",
    "stale_seconds", "parse_instant", "rfc3339", "BAR_S", "MAX_STALE_S", "HORIZON_MINUTES",
    "AlpacaNews", "NewsItem",
    "NY", "session", "in_window", "is_weekday", "ny_date", "prior_weekdays", "session_open",
    "session_close", "to_ny",
    "replay_tools", "live_tools", "TICK_BPS", "LOOKBACK_SESSIONS",
]
