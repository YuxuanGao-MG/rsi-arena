"""One model client, and the completion it returns.

OpenRouter, because the loop wants several model families behind one endpoint.
Every call is cached on disk by the SHA-256 of its request: the benchmark
re-runs the same harness on the same windows many times, and the second run of
an identical request should cost nothing. Pass ``cache=False`` for a genuinely
fresh sample.

Anything with the same ``complete`` signature is an ``LLM``; the tests use a
fake, and the runner does not care.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class LLMError(RuntimeError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"[{status}] {message}" if status else message)
        self.status = status


@dataclass
class Completion:
    text: str
    message: dict[str, Any]                       # the assistant message as returned
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    model: str = ""


class LLM(Protocol):
    async def complete(self, messages: list[dict[str, Any]], *, model: str,
                       system: str | None = None, schema: dict[str, Any] | None = None,
                       tools: list[dict[str, Any]] | None = None,
                       temperature: float | None = None,
                       max_tokens: int | None = None) -> Completion: ...


def parse_json_loose(text: str) -> Any:
    """JSON from a model reply, tolerating code fences and prose around it."""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    start = body.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    depth = 0
    for i in range(start, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(body[start:i + 1])
    raise ValueError(f"unterminated JSON object in reply: {text[:120]!r}")


class OpenRouter:
    def __init__(self, api_key: str | None = None, *, base_url: str | None = None,
                 cache_dir: str | os.PathLike | None = ".cache/llm", cache: bool = True,
                 timeout_s: float = 120.0, max_retries: int = 4, concurrency: int = 8,
                 app_title: str = "RSI Arena") -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.cache_dir = Path(cache_dir) if (cache and cache_dir) else None
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.app_title = app_title
        self._gate = asyncio.Semaphore(concurrency)
        self._client: httpx.AsyncClient | None = None
        self.calls = 0
        self.cache_hits = 0
        self.spent_usd = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "OpenRouter":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _cache_path(self, body: dict[str, Any]) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    async def complete(self, messages: list[dict[str, Any]], *, model: str,
                       system: str | None = None, schema: dict[str, Any] | None = None,
                       tools: list[dict[str, Any]] | None = None,
                       temperature: float | None = None,
                       max_tokens: int | None = None) -> Completion:
        if not self.api_key:
            raise LLMError(None, "OPENROUTER_API_KEY is not set in the environment")
        full = ([{"role": "system", "content": system}] if system else []) + list(messages)
        body: dict[str, Any] = {"model": model, "messages": full, "usage": {"include": True}}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.get("title", "output"), "strict": True, "schema": schema}}
            body["provider"] = {"require_parameters": True}

        path = self._cache_path(body)
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            self.cache_hits += 1
            return self._completion(data, model, cached=True)

        data = await self._post(body)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        completion = self._completion(data, model, cached=False)
        self.calls += 1
        self.spent_usd += completion.cost_usd
        return completion

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Title": self.app_title,
                   "Content-Type": "application/json"}
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                async with self._gate:
                    resp = await self._http().post(f"{self.base_url}/chat/completions",
                                                   json=body, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_retries:
                    raise LLMError(None, f"{type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(delay + random.random() * 0.5)
                delay *= 2
                continue
            if resp.status_code == 200:
                data = resp.json()
                if "error" in data and not data.get("choices"):
                    raise LLMError(data["error"].get("code"), data["error"].get("message", "error"))
                return data
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                await asyncio.sleep(wait + random.random() * 0.5)
                delay *= 2
                continue
            raise LLMError(resp.status_code, resp.text[:500])
        raise LLMError(None, "unreachable")

    @staticmethod
    def _completion(data: dict[str, Any], model: str, *, cached: bool) -> Completion:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        cost = 0.0 if cached else float(usage.get("cost") or 0.0)
        return Completion(text=message.get("content") or "", message=message,
                          tool_calls=list(message.get("tool_calls") or []),
                          cost_usd=cost, usage=usage, cached=cached,
                          model=data.get("model") or model)


class SyncLLM:
    """A synchronous ``prompt -> text`` callable over an :class:`OpenRouter`.

    GEPA's reflection model is a plain callable, and GEPA is synchronous.
    """

    def __init__(self, client: OpenRouter, model: str, *, temperature: float | None = 1.0,
                 max_tokens: int | None = 8000) -> None:
        self.client, self.model = client, model
        self.temperature, self.max_tokens = temperature, max_tokens

    def __call__(self, prompt: str) -> str:
        async def go() -> str:
            out = await self.client.complete([{"role": "user", "content": prompt}],
                                             model=self.model, temperature=self.temperature,
                                             max_tokens=self.max_tokens)
            return out.text
        return run_sync(go())


def run_sync(coro: Any) -> Any:
    """Run a coroutine from synchronous code, even inside a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()

