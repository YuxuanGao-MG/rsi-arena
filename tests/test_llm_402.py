"""What a 402 does, driven through the real retry loop.

Nothing tested this, which is why the arithmetic was allowed not to close: four
retries times a fifteen-second sleep cap is sixty seconds of tolerance against a
ninety-second threshold, so the starvation branch was unreachable and the call
fell out as ``LLMError(None, "unreachable")`` with ``starved`` still false. A
provider error is recorded by the runner as silence and pooled by the gate as a
measurement, so a top-up arriving a little late became a verdict.

The two existing tests set ``starved`` by hand and could not have caught it.
"""

from __future__ import annotations

import asyncio

import pytest

from rsi_arena.harness import GenerationBudgetExceeded, OpenRouter


class Reply:
    def __init__(self, status: int, text: str = "{}") -> None:
        self.status_code, self.text = status, text

    def json(self):
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}


class _Open:
    """A semaphore that is always free, so nothing here depends on a loop."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def client(replies, **kw):
    """An OpenRouter whose POST returns a scripted sequence, and never sleeps."""
    c = OpenRouter(api_key="k", cache=False, **kw)
    seq = list(replies)

    class Http:
        async def post(self, *a, **k):
            return seq.pop(0) if seq else Reply(200)

    c._http = lambda: Http()                       # noqa: SLF001
    # Bind by hand rather than stubbing _bind: the client asserts on its own
    # semaphore, and a stub that skips that assertion would be testing a client
    # that cannot exist.
    c._loop = object()                             # noqa: SLF001
    c._client = Http()                             # noqa: SLF001
    c._gate = _Open()                              # noqa: SLF001
    c._bind = lambda: None                         # noqa: SLF001

    async def _nosleep(_):
        return None

    asyncio.sleep_real, asyncio.sleep = asyncio.sleep, _nosleep
    return c, seq


def restore():
    if hasattr(asyncio, "sleep_real"):
        asyncio.sleep = asyncio.sleep_real


def test_a_402_that_clears_does_not_end_the_generation():
    """The common case on a key that tops up when it runs low."""
    c, _ = client([Reply(402), Reply(402), Reply(200)])
    try:
        out = asyncio.run(c.complete([{"role": "user", "content": "x"}], model="m"))
    finally:
        restore()
    assert out is not None
    assert c.starved is False
    assert c.starved_since is None, "a successful call clears the clock"
    assert c.over_budget is False


def test_waiting_for_a_top_up_does_not_burn_the_retry_budget():
    """More 402s than max_retries, all clearing inside the window.

    This is the case that used to exit as LLMError(None, "unreachable") with
    nothing marked, which the runner then wrote down as silence.
    """
    c, _ = client([Reply(402)] * 8 + [Reply(200)], max_retries=4, starve_after_s=90.0)
    try:
        out = asyncio.run(c.complete([{"role": "user", "content": "x"}], model="m"))
    finally:
        restore()
    assert out is not None and c.starved is False


def test_a_402_that_never_clears_is_reported_as_exhaustion():
    """And as exhaustion specifically, not as a provider error scored as silence."""
    c, _ = client([Reply(402)] * 400, max_retries=4, starve_after_s=0.0)
    try:
        with pytest.raises(GenerationBudgetExceeded):
            asyncio.run(c.complete([{"role": "user", "content": "x"}], model="m"))
    finally:
        restore()
    assert c.starved is True and c.over_budget is True


def test_exhaustion_without_a_ceiling_does_not_raise_TypeError():
    """float(None) when budget_usd is unset — reachable from collect_live --max-usd 0."""
    c, _ = client([Reply(402)] * 400, max_retries=1, starve_after_s=0.0)
    c.budget_usd = None
    try:
        with pytest.raises(GenerationBudgetExceeded):
            asyncio.run(c.complete([{"role": "user", "content": "x"}], model="m"))
    finally:
        restore()


def test_a_real_error_still_fails_fast():
    """The 402 path must not make every other status patient."""
    from rsi_arena.harness import LLMError
    c, _ = client([Reply(400, "bad request")], max_retries=4)
    try:
        with pytest.raises(LLMError):
            asyncio.run(c.complete([{"role": "user", "content": "x"}], model="m"))
    finally:
        restore()
    assert c.starved is False
