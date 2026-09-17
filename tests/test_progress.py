"""The heartbeat is a window, not a load-bearing wall.

It writes from inside the money path, so the one property that matters more
than any feature is that it cannot be the reason a paid generation dies.
"""

from __future__ import annotations

from rsi_arena.loop import Progress


def test_no_database_means_every_call_is_a_silent_no_op(monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    beat = Progress("t")
    beat.phase("baseline", train=10)
    beat.tick("search", evaluations=1)
    beat.done("rejected")                      # nothing raises, nothing connects


def test_a_dead_database_warns_once_and_never_raises(capsys):
    beat = Progress("t", db_url="postgresql://nobody:x@127.0.0.1:1/nope?connect_timeout=1")
    for _ in range(3):
        beat.phase("search", spent_usd=1.0)    # forced writes, all failing
    beat.done("rejected")
    err = capsys.readouterr().err
    assert err.count("heartbeat failed") == 1, "one warning, then quiet"


def test_ticks_are_throttled_and_phases_are_not():
    writes = []
    beat = Progress("t", db_url="")            # no-op backend
    # Substitute the write to observe throttling logic without a database.
    beat.url = "x"
    beat._write = lambda phase, detail, force: writes.append((phase, force))  # noqa: SLF001
    beat.tick("search")
    beat.phase("holdout")
    assert writes == [("search", False), ("holdout", True)]
