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
import time
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .decisions import Decision, wire_questions

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


def decisions_url(base_url: str) -> str:
    """Where typed questions go. OpenRouter serves them from an alpha route
    beside v1; a gateway that fronts OpenRouter serves them beside its own
    chat route."""
    base = base_url.rstrip("/")
    if base.endswith("openrouter.ai/api/v1"):
        return base[: -len("/v1")] + "/alpha/decisions"
    return base + "/decisions"


class LLMError(RuntimeError):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"[{status}] {message}" if status else message)
        self.status = status


class GenerationBudgetExceeded(LLMError):
    """The whole generation's money is gone, not just this window's.

    ``_Ledger`` bounds one run at a fifth of a dollar. Nothing bounded the
    generation around it, so the ceiling on a search was ``max_metric_calls``
    multiplied by whatever a window happened to cost — and a candidate that
    grows the context roughly doubles that, which has already happened once.
    A run killed by the job timeout after five and a half hours is the same
    money with none of the answer.

    Raised as an ``LLMError`` on purpose: the runner already records one as a
    provider failure and scores the window as silence, so the first window past
    the line ends the generation tidily with a manifest rather than a traceback,
    and every window after it costs nothing to refuse.
    """

    def __init__(self, spent: float, ceiling: float) -> None:
        super().__init__(None, f"generation budget exhausted: spent ${spent:.2f} "
                               f"of ${ceiling:.2f}")
        self.spent, self.ceiling = spent, ceiling


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
    async def decide(self, state: Any, questions: dict[str, Any], *, model: str) -> "Decision":
        ...

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
                 app_title: str = "RSI Arena", budget_usd: float | None = None,
                 starve_after_s: float = 90.0) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.cache_dir = Path(cache_dir) if (cache and cache_dir) else None
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.app_title = app_title
        self.concurrency = concurrency
        # Both of these bind to whichever loop first touches them, and the loop
        # changes underneath us: `run_sync` opens a fresh `asyncio.run` per call,
        # so GEPA's tenth evaluation runs on its tenth loop while the client's
        # sockets and the semaphore still belong to the first. That surfaced as
        # `Event loop is closed` at teardown and would have surfaced as a bound
        # -to-a-different-loop error partway through the first real optimize.
        self._loop: Any = None
        self._gate: asyncio.Semaphore | None = None
        self._client: httpx.AsyncClient | None = None
        self.calls = 0
        self.cache_hits = 0
        self.spent_usd = 0.0
        #: Total this client may spend before it refuses to call out. None is no
        #: ceiling, which is what every run before today had.
        self.budget_usd = budget_usd
        #: Set when the provider says the account has no credit *and* has kept
        #: saying it for long enough that a top-up would have arrived. Distinct
        #: from the ceiling because nobody chose it.
        self.starved = False
        #: When the first unanswered 402 arrived, or None once one succeeds.
        self.starved_since: float | None = None
        #: How long a 402 has to persist before it counts as empty rather than
        #: as a top-up in flight.
        self.starve_after_s = starve_after_s

    @property
    def over_budget(self) -> bool:
        """Spent at or past the ceiling. False when there is no ceiling.

        Checked after the cache lookup, not before: a cached completion costs
        nothing and refusing one would make an exhausted generation report
        differently on a re-run than it did the first time.
        """
        if self.starved:
            return True
        return self.budget_usd is not None and self.spent_usd >= self.budget_usd

    def _bind(self) -> None:
        """Attach to the running loop, discarding anything bound to an older one.

        Dropping a client without awaiting `aclose` leaks its sockets, but the
        loop that owned them is already closed and closing them from here is
        exactly the error this avoids. The OS reclaims them when the process
        ends, and a command is one process.
        """
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._client = httpx.AsyncClient(timeout=self.timeout_s)
        self._gate = asyncio.Semaphore(self.concurrency)

    def _http(self) -> httpx.AsyncClient:
        self._bind()
        assert self._client is not None
        return self._client

    def _limit(self) -> asyncio.Semaphore:
        self._bind()
        assert self._gate is not None
        return self._gate

    async def close(self) -> None:
        """Close the client if this loop owns it; otherwise leave it alone."""
        if self._client is None:
            return
        try:
            mine = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            mine = False
        if mine:
            await self._client.aclose()
        self._client = None
        self._gate = None
        self._loop = None

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

        if self.over_budget:
            raise GenerationBudgetExceeded(self.spent_usd,
                                           float(self.budget_usd or self.spent_usd))
        data = await self._post(body)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        self.starved_since = None          # the top-up landed
        completion = self._completion(data, model, cached=False)
        self.calls += 1
        self.spent_usd += completion.cost_usd
        return completion

    async def decide(self, state: Any, questions: dict[str, Any], *, model: str) -> Decision:
        """Ask a decisions model typed questions about a state. Same cache,
        same ceiling, same retries and the same 402 rule as ``complete``."""
        if not self.api_key:
            raise LLMError(None, "OPENROUTER_API_KEY is not set in the environment")
        body: dict[str, Any] = {"model": model, "state": state, "questions": wire_questions(questions)}
        path = self._cache_path(body)
        if path is not None and path.exists():
            data = json.loads(path.read_text())
            self.cache_hits += 1
            return self._decision(data, model, cached=True)
        if self.over_budget:
            raise GenerationBudgetExceeded(self.spent_usd,
                                           float(self.budget_usd or self.spent_usd))
        data = await self._post(body, url=decisions_url(self.base_url))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        self.starved_since = None
        decision = self._decision(data, model, cached=False)
        self.calls += 1
        self.spent_usd += decision.cost_usd
        return decision

    @staticmethod
    def _decision(data: dict[str, Any], model: str, *, cached: bool) -> Decision:
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise LLMError(None, f"decisions response carried no answers: {str(data)[:300]}")
        usage = data.get("usage") or {}
        return Decision(answers=answers, cost_usd=0.0 if cached else float(usage.get("cost") or 0.0),
                        usage=usage, cached=cached, model=data.get("model") or model)

    async def _post(self, body: dict[str, Any], *, url: str | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "X-Title": self.app_title,
                   "Content-Type": "application/json"}
        delay = 1.0
        attempt = -1
        while True:
            attempt += 1
            if attempt > self.max_retries:
                break
            try:
                async with self._limit():
                    resp = await self._http().post(url or f"{self.base_url}/chat/completions",
                                                   json=body, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_retries:
                    raise LLMError(None, f"{type(exc).__name__}: {exc}") from exc
                await asyncio.sleep(delay + random.random() * 0.5)
                delay *= 2
                continue
            if resp.status_code == 200:
                data = resp.json()
                if "error" in data and not (data.get("choices") or data.get("answers")):
                    raise LLMError(data["error"].get("code"), data["error"].get("message", "error"))
                return data
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                await asyncio.sleep(wait + random.random() * 0.5)
                delay *= 2
                continue
            if resp.status_code == 402:
                # Out of credit — which is usually temporary, and the shape of
                # the funding decides how to treat it.
                #
                # This account tops itself up by thirty dollars whenever it falls
                # below ten, so the balance sits between about ten and forty and
                # a generation costing fifty *will* cross zero once or twice on
                # the way through. A 402 there is a few seconds of waiting, not a
                # verdict. Treating the first one as starvation would abandon a
                # generation that was going to finish, and abandon it in the worst
                # way: the runner records a provider error as silence, so what
                # lands on disk is a full set of rollouts reading as a harness
                # that chose to stay quiet.
                #
                # So wait it out, and only conclude the money is gone when it is
                # still gone after the top-up has had time to arrive.
                # Waiting for a top-up must not consume a retry attempt.
                #
                # It did, and the arithmetic did not close: four retries times a
                # fifteen-second sleep cap is sixty seconds of tolerance against
                # a ninety-second threshold, so the branch below was unreachable
                # from one call's own clock. The call fell out of the loop as
                # `LLMError(None, "unreachable")` with `starved` still false — and
                # a provider error is recorded by the runner as *silence*, pooled
                # by the gate as a real measurement, and reported as evidence.
                # A funding hiccup became a verdict. The balance here sits
                # between ten and forty dollars against a fifty-dollar
                # generation, so this was not hypothetical.
                self.starved_since = self.starved_since or time.monotonic()
                waited = time.monotonic() - self.starved_since
                if waited < self.starve_after_s:
                    await asyncio.sleep(min(15.0, self.starve_after_s - waited))
                    attempt -= 1            # the wait is not a failed attempt
                    continue
                self.starved = True
                raise GenerationBudgetExceeded(self.spent_usd,
                                               float(self.budget_usd or self.spent_usd))
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

