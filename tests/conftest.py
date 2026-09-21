"""Fakes: a model that answers from a script, and a history built from a price series."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

from rsi_arena.harness import Completion, Decision
from rsi_arena.kalshi._history import Candle

UTC = timezone.utc


class FakeLLM:
    """Answers by calling ``script(messages, schema, tools)``; counts calls."""

    def __init__(self, script: Callable[..., Any] | None = None, cost: float = 0.001,
                 decisions: Callable[..., Any] | None = None) -> None:
        self.script = script or (lambda messages, schema, tools: "ok")
        self.cost = cost
        self.calls: list[dict[str, Any]] = []
        #: ``decisions(state, questions) -> answers`` for a decisions model.
        #: The default answers every score question with all its mass on the
        #: middle level, every noul with 0.5 and every choice with its first label.
        self.decisions = decisions
        self.decided: list[dict[str, Any]] = []

    async def decide(self, state, questions, *, model) -> Decision:
        self.decided.append({"state": state, "questions": questions, "model": model})
        if self.decisions is not None:
            answers = self.decisions(state, questions)
        else:
            answers = {}
            for key, q in questions.items():
                if q["type"] == "score":
                    n = len(q["criteria"]); mid = (n - 1) // 2
                    answers[key] = {"type": "score", "score": float(mid),
                                    "probabilities": {str(i): (1.0 if i == mid else 0.0) for i in range(n)},
                                    "confidence": 1.0}
                elif q["type"] == "noul":
                    answers[key] = {"type": "noul", "noul": 0.5}
                else:
                    first = next(iter(q["criteria"]))
                    answers[key] = {"type": "choice", "choice": first,
                                    "probabilities": {k: (1.0 if k == first else 0.0) for k in q["criteria"]},
                                    "confidence": 1.0}
        return Decision(answers=answers, cost_usd=self.cost / 1000, model=model)

    async def complete(self, messages, *, model, system=None, schema=None, tools=None,
                       temperature=None, max_tokens=None) -> Completion:
        self.calls.append({"messages": messages, "model": model, "system": system,
                           "schema": schema, "tools": tools})
        answer = self.script(messages, schema, tools)
        if isinstance(answer, dict) and "tool_calls" in answer:
            return Completion(text="", message={"role": "assistant", "content": None,
                                                "tool_calls": answer["tool_calls"]},
                              tool_calls=answer["tool_calls"], cost_usd=self.cost)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return Completion(text=text, message={"role": "assistant", "content": text},
                          cost_usd=self.cost)


def candle(ticker: str, ts: datetime, mid: float, spread: float = 0.02, volume: float = 1.0,
           two_sided: bool = True) -> Candle:
    bid, ask = (mid - spread / 2, mid + spread / 2) if two_sided else (0.0, 1.0)
    return Candle(ticker=ticker, ts=ts, yes_bid_open=bid, yes_bid_close=bid, yes_bid_high=bid,
                  yes_bid_low=bid, yes_ask_open=ask, yes_ask_close=ask, yes_ask_high=ask,
                  yes_ask_low=ask, price_open=mid, price_close=mid, price_high=mid, price_low=mid,
                  price_mean=mid, price_previous=mid, volume=volume, open_interest=100.0)


class FakeHistory:
    """Minute candles from ``series[ticker] = {minute_offset: mid}`` starting at ``t0``."""

    def __init__(self, t0: datetime, series: dict[str, dict[int, float]],
                 dead: set[tuple[str, int]] | None = None) -> None:
        self.t0, self.series, self.dead = t0, series, dead or set()
        self.trade_calls: list[dict[str, Any]] = []

    def _candles(self, ticker: str) -> list[Candle]:
        out = []
        for minute, mid in sorted(self.series.get(ticker, {}).items()):
            ts = self.t0 + timedelta(minutes=minute)
            out.append(candle(ticker, ts, mid, two_sided=(ticker, minute) not in self.dead))
        return out

    def quote_at(self, ticker: str, when: datetime, interval: int = 1) -> Candle | None:
        usable = [c for c in self._candles(ticker) if c.ts <= when]
        return usable[-1] if usable else None

    def price_path(self, ticker: str, start=None, end=None, interval: int = 1,
                   clip_to_close: bool = True) -> list[Candle]:
        return [c for c in self._candles(ticker)
                if (start is None or c.ts >= start) and (end is None or c.ts <= end)]

    def trades(self, ticker: str, start=None, end=None, max_trades=None) -> list[dict]:
        self.trade_calls.append({"ticker": ticker, "start": start, "end": end, "max": max_trades})
        return [{"created_time": (end or self.t0).isoformat(), "yes_price_dollars": "0.50",
                 "count_fp": "3", "taker_side": "yes"}]


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 8, 23, 15, 0, tzinfo=UTC)


@pytest.fixture
def history(t0) -> FakeHistory:
    # Contract A drifts up a cent a minute; contract B is flat.
    return FakeHistory(t0, {"A": {m: 0.40 + 0.01 * m for m in range(0, 40)},
                            "B": {m: 0.50 for m in range(0, 40)}})
