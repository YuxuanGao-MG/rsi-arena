"""What the run is doing right now, said while it is still doing it.

A generation runs for an hour or two and, until this existed, said nothing
until the end: the publish step fired after the verdict, so the reader was an
archive of finished runs and the answer to "is it working?" was reading CI logs.
A training run that is observable only after it finishes is a batch job wearing
a dashboard.

So the loop now keeps one row per run in ``rsi.progress`` current as it moves —
which phase, what has been spent, how many candidates the search has produced —
and the reader polls it. One row, upserted, not an event log: the question the
page asks is "what is happening", not "what has ever happened", and the runs
table already answers the second.

Strictly fail-open. This writes from inside the money path, and a telemetry
insert must never be the reason a paid generation dies: no database URL means
every call is a silent no-op, and a database error is swallowed after one
warning. The heartbeat is a window, not a load-bearing wall.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

#: Progress rows younger than this are "live"; the reader greys out anything
#: older, so a run that died mid-phase reads as stale rather than eternal.
STALE_AFTER_S = 300


class Progress:
    """One updatable row describing the current run. No-op without a database."""

    def __init__(self, run_id: str, topic: str = "", db_url: str | None = None) -> None:
        self.run_id = run_id
        #: Which topic the run is on. Written only if the table has a column for
        #: it: the reader's schema gains one in a later migration, and a
        #: heartbeat must not fail on a database that has not caught up.
        self.topic = topic
        self.url = db_url if db_url is not None else (
            os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL") or "")
        self._conn: Any = None
        self._has_topic: bool | None = None
        self._warned = False
        self._last_write = 0.0
        self.started = time.time()

    # -- the api the loop calls --

    def phase(self, name: str, **detail: Any) -> None:
        """Enter a phase. Always written, even inside the throttle window."""
        self._write(name, detail, force=True)

    def tick(self, name: str, **detail: Any) -> None:
        """Update within a phase. Throttled, because GEPA iterations are chatty
        and one row a few seconds apart is telemetry, not a write amplifier."""
        self._write(name, detail, force=False)

    def done(self, conclusion: str, **detail: Any) -> None:
        self._write("done", {"conclusion": conclusion, **detail}, force=True)
        self._close()

    # -- plumbing --

    def _write(self, phase: str, detail: dict[str, Any], *, force: bool) -> None:
        if not self.url:
            return
        now = time.time()
        if not force and now - self._last_write < 10:
            return
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                if self.topic and self._topic_column(cur):
                    cur.execute(
                        """insert into rsi.progress (run_id, topic, phase, detail, started_at, updated_at)
                           values (%s, %s, %s, %s, to_timestamp(%s), now())
                           on conflict (run_id) do update
                           set topic = excluded.topic, phase = excluded.phase,
                               detail = excluded.detail, updated_at = now()""",
                        (self.run_id, self.topic, phase,
                         json.dumps(detail, default=str), self.started))
                else:
                    cur.execute(
                        """insert into rsi.progress (run_id, phase, detail, started_at, updated_at)
                           values (%s, %s, %s, to_timestamp(%s), now())
                           on conflict (run_id) do update
                           set phase = excluded.phase, detail = excluded.detail,
                               updated_at = now()""",
                        (self.run_id, phase,
                         json.dumps(detail, default=str), self.started))
            conn.commit()
            self._last_write = now
        except Exception as exc:  # noqa: BLE001 - the window must not break the wall
            if not self._warned:
                print(f"progress heartbeat failed and will stay quiet: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                self._warned = True
            self._close()

    def _topic_column(self, cur: Any) -> bool:
        """Whether ``rsi.progress`` has a ``topic`` column. Asked once, and a
        probe that fails reads as "no", which is the write that always works."""
        if self._has_topic is None:
            try:
                cur.execute("select 1 from information_schema.columns "
                            "where table_schema = 'rsi' and table_name = 'progress' "
                            "and column_name = 'topic'")
                self._has_topic = cur.fetchone() is not None
            except Exception:  # noqa: BLE001
                self._has_topic = False
        return self._has_topic

    def _connect(self) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg2
            self._conn = psycopg2.connect(self.url, connect_timeout=5)
        return self._conn

    def _close(self) -> None:
        try:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None
