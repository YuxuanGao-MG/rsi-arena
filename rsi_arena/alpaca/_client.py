"""Alpaca Market Data client: auth from the environment, pagination, a
per-minute limiter, and a 429 that waits as long as it is told to.

Read endpoints under ``https://data.alpaca.markets`` take the two key headers
and nothing else. A free account gets the IEX feed with years of one-minute
history and the Benzinga news feed back to 2015, at 200 requests a minute.
Everything here is ``httpx``; nothing is fetched at construction, and the
keys are read from ``APCA_API_KEY_ID`` / ``APCA_API_SECRET_KEY`` the first
time a request needs them, so a task can be built, listed and tested on a
machine with no keys at all.

Verified against the published API shapes (2026-09):

- ``GET /v2/stocks/bars?symbols=AAPL&timeframe=1Min&start&end&limit=10000
  &feed=iex&adjustment=raw&sort=asc[&page_token]`` ->
  ``{"bars": {"AAPL": [{"t","o","h","l","c","v","n","vw"}]}, "next_page_token"}``;
  ``t`` is the bar's OPEN.
- ``GET /v1beta1/news?symbols=AAPL,TSLA&start&end&limit=50&sort=asc
  &include_content=false[&page_token]`` ->
  ``{"news": [{"id","headline","summary","content","created_at","updated_at",
  "symbols","source","url","author"}], "next_page_token"}``.
- ``GET /v2/stocks/{symbol}/trades?start&end&limit&feed=iex&sort=desc`` ->
  ``{"trades": [{"t","p","s",...}], "next_page_token"}``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterator

import httpx

DATA_BASE = "https://data.alpaca.markets"
KEY_ENV, SECRET_ENV = "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"

#: Requests a minute on the free data plan.
PER_MINUTE = 200


class MissingCredentials(RuntimeError):
    """No keys in the environment. Raised at the first request, never at construction."""


class RateLimiter:
    """Token bucket over a per-minute budget. The same shape as Kalshi's,
    refilled at a minute's rate rather than a second's."""

    def __init__(self, per_minute: int = PER_MINUTE, safety: float = 0.9) -> None:
        self.capacity = max(1.0, per_minute * safety)
        self.rate = self.capacity / 60.0            # tokens a second
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, cost: int = 1) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                wait = (cost - self._tokens) / self.rate
            time.sleep(wait)


class AlpacaData:
    """The data API, read only.

    ``transport`` is for tests: an ``httpx.MockTransport`` makes the whole
    client exercisable offline, pagination and retries included. ``sleep``
    is injectable for the same reason.
    """

    def __init__(self, key_id: str | None = None, secret: str | None = None, *,
                 base: str = DATA_BASE, timeout: float = 30.0, max_retries: int = 4,
                 per_minute: int = PER_MINUTE, transport: Any = None,
                 sleep: Any = time.sleep) -> None:
        self._key_id, self._secret = key_id, secret
        self.base, self.timeout, self.max_retries = base, timeout, max_retries
        self._limiter = RateLimiter(per_minute)
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.Client | None = None
        self.requests = 0                          # made, for reports and tests

    # -- credentials, read late ---------------------------------------------

    def credentials(self) -> tuple[str, str]:
        key = self._key_id or os.environ.get(KEY_ENV, "")
        secret = self._secret or os.environ.get(SECRET_ENV, "")
        if not key or not secret:
            raise MissingCredentials(
                f"no Alpaca keys: set {KEY_ENV} and {SECRET_ENV} (a free account gives the IEX "
                f"feed and the news feed)")
        return key, secret

    @property
    def has_credentials(self) -> bool:
        try:
            self.credentials()
        except MissingCredentials:
            return False
        return True

    def _headers(self) -> dict[str, str]:
        key, secret = self.credentials()
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                "Accept": "application/json", "User-Agent": "rsi-arena/1.0"}

    def _session(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base, timeout=self.timeout,
                                        transport=self._transport)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- transport -------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One page. Retries a 429 for as long as ``Retry-After`` says, and a
        5xx with backoff; anything else raises."""
        query = {k: v for k, v in (params or {}).items() if v is not None}
        headers = self._headers()
        for attempt in range(self.max_retries + 1):
            self._limiter.take()
            self.requests += 1
            try:
                resp = self._session().get(path, params=query, headers=headers)
            except httpx.TransportError:
                if attempt < self.max_retries:
                    self._sleep(min(2 ** attempt * 0.5, 8.0))
                    continue
                raise
            if resp.status_code == 429 and attempt < self.max_retries:
                self._sleep(_retry_after(resp))
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                self._sleep(min(2 ** attempt * 0.5, 8.0))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"exhausted retries for {path}")

    def pages(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Every page, following ``next_page_token`` until it is absent."""
        query = dict(params or {})
        while True:
            page = self.get(path, query)
            yield page
            token = page.get("next_page_token")
            if not token:
                return
            query["page_token"] = token

    def collect(self, path: str, key: str, params: dict[str, Any] | None = None, *,
                max_items: int | None = None) -> Any:
        """``key`` from every page, merged: a list stays a list, and a dict of
        lists (``bars`` keyed by symbol) merges per symbol. ``max_items``
        stops paging once a list has that many."""
        merged: Any = None
        for page in self.pages(path, params):
            chunk = page.get(key)
            if chunk is None:
                continue
            if isinstance(chunk, dict):
                merged = merged if isinstance(merged, dict) else {}
                for symbol, rows in chunk.items():
                    merged.setdefault(symbol, []).extend(rows or [])
            else:
                merged = merged if isinstance(merged, list) else []
                merged.extend(chunk)
                if max_items is not None and len(merged) >= max_items:
                    return merged[:max_items]
        if merged is None:
            return {} if key == "bars" else []
        return merged


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    try:
        return max(0.0, min(60.0, float(raw)))
    except (TypeError, ValueError):
        return 2.0


__all__ = ["AlpacaData", "MissingCredentials", "RateLimiter", "DATA_BASE", "KEY_ENV",
           "SECRET_ENV", "PER_MINUTE"]
