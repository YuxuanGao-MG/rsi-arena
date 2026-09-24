"""A paper-trading engine for every harness: one simulated book per (topic,
harness), a two-sided quote posted every cycle and filled by the realised
price path, an optional take that crosses the spread, equity marked each
cycle. Topic-agnostic and stdlib only; it must not import from
``rsi_arena.topics`` or ``rsi_arena.loop``. Start with ``costs.py`` (what a
fill costs, per venue, taking and resting), then ``book.py`` (the state),
``policy.py`` (the quote and the take), ``replay.py`` (a benchmark's rollouts
as cycles) and ``live.py`` (a collector's rows, resumed from a file).
"""

from __future__ import annotations

from .book import (MAX_GROSS, MAX_POSITION, MIN_SIZE, START_EQUITY, Book, Mark, PathBar, Position,
                   Posted, QuoteFill, StepResult, Trade)
from .costs import (CRYPTO_PROXY_SPREAD_BPS, EQUITY_HALF_SPREAD_BPS, KALSHI_PROXY_SPREAD,
                    MAKER_BPS_PER_SIDE, TAKER_BPS_PER_SIDE, EquityCosts, Fill, KalshiCosts, PerpCosts,
                    Quote, VenueCosts, costs_named, default_tick, walk_book)
from .live import load_state, rows_to_cycles, save_state, step_live
from .policy import (ACTIONS, DEFAULT_QUOTE_SIZE, GRAMMAR_NOTE, KELLY_FRACTION, TRADING_CONTRACT,
                     Decision, Quoted, decide, default_decision, quote_from, quote_size_of,
                     read_decision, read_quote)
from .replay import (Cycle, TradingSpec, apply_cycle, book_line, cycles_of, delta_from_details, replay_book,
                     with_trade)
from .stats import CYCLES_PER_YEAR, PNL_SCALE_USD, book_stats, equity_stats, pnl_objective

__all__ = [
    "Book", "Position", "Trade", "Mark", "PathBar", "QuoteFill", "Posted", "StepResult",
    "START_EQUITY", "MAX_POSITION", "MAX_GROSS",
    "MIN_SIZE", "Quote", "Fill", "VenueCosts", "KalshiCosts", "PerpCosts", "EquityCosts", "walk_book",
    "costs_named", "default_tick", "KALSHI_PROXY_SPREAD", "CRYPTO_PROXY_SPREAD_BPS",
    "EQUITY_HALF_SPREAD_BPS", "TAKER_BPS_PER_SIDE", "MAKER_BPS_PER_SIDE", "ACTIONS",
    "KELLY_FRACTION", "DEFAULT_QUOTE_SIZE", "TRADING_CONTRACT",
    "GRAMMAR_NOTE", "Decision", "Quoted", "read_decision", "read_quote", "quote_from",
    "quote_size_of", "default_decision", "decide", "Cycle", "TradingSpec",
    "cycles_of", "replay_book", "apply_cycle", "delta_from_details", "book_line", "with_trade",
    "CYCLES_PER_YEAR", "PNL_SCALE_USD", "pnl_objective", "equity_stats", "book_stats", "rows_to_cycles",
    "load_state", "save_state", "step_live",
]
