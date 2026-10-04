"""Ask, every hour, whether the arena is still working.

Everything here runs unattended: three loops a day per topic, live sweeps
around the clock, a weekly roll, a monthly league check. Each of those already
fails loudly when it fails. What none of them can see is the failure that looks
like nothing happening - a scheduled run GitHub never fired, a sweep that
identified no market, a topic whose generations quietly stopped chaining. Those
have each cost a day before anyone noticed: a Nations League evening with no
forecasts, three generations whose results never reached the repository, a
crypto cron that fired seven times instead of twenty-four.

So this asks the questions a person would ask, on the schedule a person would
not keep:

* did every workflow that should have run in the last few hours actually run,
  and did it end green;
* is the repository still free of the heavy files that stop a push;
* has each topic produced a generation recently, and did the newest one chain
  from the one before it;
* are live forecasts still arriving, and are they being scored;
* is the reader's database still reachable and still being written to;
* is each generation's evidence actually in S3, now that it is not in git;
* and does the reader's own overview still load, for every topic, inside the
  time the database allows a statement.

That last one was added after a failure the rest of this file could not see. On
29 September the overview began answering 500 for two of the three topics: the
query behind it had grown past Supabase's three-second statement timeout as the
rollout table grew, and nothing about the loops, the repository or the database
looked wrong. A person noticed, from a screenshot. So the watchdog now asks the
question a person asks - does the page come up - by issuing the request the
browser issues, with the credentials the page itself is served with.

It writes a verdict and exits non-zero when something is wrong, so the workflow
around it can open one issue rather than a person reading dashboards. Nothing
here changes anything: a watchdog that repairs things hides the thing it was
meant to reveal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

UTC = timezone.utc

#: The workflows whose silence is a fault. How long they may be silent is not
#: written here any more: it is read from each workflow's own cron.
#:
#: It used to be a number per workflow, and the number was wrong twice. Nine
#: hours for a live sweep assumed a four-hourly cron that had been changed. And
#: fourteen hours for live-news assumed it ran every day, when its cron is
#: weekdays 13:35 to 20:05 UTC - so every Saturday, by about lunchtime, the
#: watchdog opened an issue saying the news sweep had stopped. A check that
#: cries wolf every weekend is a check people learn to close without reading.
WATCHED = ("loop.yml", "live.yml", "live-crypto.yml", "live-news.yml")

#: How many scheduled firings may be dropped before that is a fault. GitHub's
#: scheduler is best-effort and drops single firings routinely; two in a row is
#: not weather. The count is of *scheduled* firings, so a weekend costs a
#: weekday-only workflow nothing.
MISSES_ALLOWED = 2

#: A topic that has not finished a generation in this long has stopped
#: evolving, whatever the workflow's own status says.
GENERATION_HOURS = 20

#: The deployed reader. The check reads its served HTML for the Supabase URL and
#: anon key, so the watchdog needs no credentials of its own and tests exactly
#: the pair the browser is given.
READER = os.environ.get("RSI_READER_URL", "https://rsi.up.railway.app")

#: Topics whose overview must load. Not derived from the database: a topic that
#: has stopped publishing should still have a page, and reading the list from the
#: rows would make it vanish from the check at the same moment it broke.
READER_TOPICS = ("kalshi-horizon-5m", "crypto-horizon-1m", "news-equity-5m")

#: The columns the overview actually selects, from `web/generations.js`. Asking
#: for fewer would make the check pass on a query the page does not issue: these
#: carry the jsonb records and are most of the half-megabyte a topic's overview
#: downloads.
RUN_COLUMNS = ("id,topic,created,parent,accepted,reasons,incumbent_fp,candidate_fp,"
               "baseline,candidate,decision,search,llm,split")

#: Supabase cancels a statement at three seconds, so a query approaching that is
#: worth saying out loud before it crosses. How close is measured only where the
#: measurement means something: an aggregate returning five kilobytes spends its
#: time in the database, while the runs query ships half a megabyte of jsonb and
#: its wall clock is mostly transfer. Warning on the latter would mean an issue
#: every hour a runner had a slow link, so it is only required to work.
SLOW_SECONDS = {"pooled skill": 2.0}

#: A single slow reading is weather. This was learnt the other way round on
#: 29 September, when a warm cache made a fix look like it had worked; a lone
#: measurement is not evidence in either direction, so slowness is confirmed by
#: a second attempt before it becomes a fault.
CONFIRM = 2

#: Columns a migration added for something to write, with how recent a row must
#: be for its emptiness to count. A column that is null on every row is either a
#: feature that never shipped or a pipeline step that silently stopped, and
#: neither announces itself: `rsi.live_forecasts.quote`, `fills` and `path` were
#: null on every row from the day migration 010 created them until 4 October,
#: because the paper trader ran after the publisher and never wrote the record
#: back. Three months, no error, and the contract says every window must post a
#: two-sided quote.
#:
#: Only columns something is *supposed* to fill belong here. A column that is
#: legitimately sparse would make this noise.
WRITTEN_COLUMNS = {
    ("live_forecasts", "quote"): 24,
    ("live_forecasts", "fills"): 24,
    ("live_forecasts", "path"): 24,
}

#: Where the trajectories live, and what each topic is called there.
#:
#: The layout is `fetch_run.py`'s, imported rather than restated so the check
#: cannot drift from the thing it checks: `rsi-arena/<topic>/[<lineage>/]<run>/`,
#: the lineage said only where it disambiguates - which is Kalshi, whose two
#: lineages share generation numbers. A run's
#: rollouts and GEPA state are no longer committed - they are what pushed three
#: runs past GitHub's file limit - so S3 is the only copy after the run artifact
#: expires in fourteen days. "Not in git" is only safe if "in S3" is true, and
#: `loop.yml` uploads on a best effort of three attempts. This is the other half
#: of that promise.
S3_PREFIX = "rsi-arena"

#: Generations whose trajectories were never uploaded because the bucket did not
#: work yet: the IAM user had no policy for a week and the old sync step
#: swallowed the AccessDenied. They are listed rather than forgiven by a date,
#: because a date would also forgive the next real failure that happened to fall
#: on the wrong side of it. Each line is `<topic>/<run>`; `#` comments.
#:
#: The local copies are the only ones, so this file shrinks as they are uploaded.
#: It is not a suppression list: the count is printed every hour.
S3_BACKLOG = Path(__file__).resolve().parent / "s3_backlog.txt"

#: How long after a generation is recorded its evidence may still be missing.
#: The upload happens in the same job, so this is slack for a retried sync and a
#: clock skew, not for a person to get round to it.
S3_GRACE_HOURS = 6

#: Paths that must never be tracked: they are the ones that grow past what
#: GitHub will accept, and every time they have come back a push has died.
HEAVY = re.compile(r"runs/.*/(rollouts|gepa)/|runs/.*\.rollouts\.json$")


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    #: Anything a check found that is too long to print but worth keeping - the
    #: full list behind a "46 generations" summary, say. Goes to the JSON
    #: verdict, which is uploaded as an artifact, so acting on a finding does
    #: not mean re-running the check to see the rest of it.
    data: dict = field(default_factory=dict)

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append((name, True, detail))

    def bad(self, name: str, detail: str) -> None:
        self.checks.append((name, False, detail))

    def note(self, key: str, value) -> None:
        self.data[key] = value

    @property
    def faults(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]

    def render(self) -> str:
        lines = []
        for name, good, detail in self.checks:
            lines.append(f"  {'ok  ' if good else 'FAIL'}  {name}" + (f" - {detail}" if detail else ""))
        lines.append("")
        lines.append(f"{len(self.checks) - len(self.faults)} ok, {len(self.faults)} failing")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        out = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
               "faults": len(self.faults),
               "checks": [{"name": n, "ok": g, "detail": d} for n, g, d in self.checks]}
        if self.data:
            out["found"] = self.data
        return out


def gh(args: list[str]) -> str:
    """`gh` output, or "" when the call fails.

    A watchdog that dies because its own tooling hiccuped reports nothing,
    which is the state it exists to detect. Failures become empty answers and
    the check that wanted them says so.
    """
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def cron_fields(spec: str) -> list[set[int]] | None:
    """A cron line as five sets of allowed values, or None if it is beyond us.

    Enough of the syntax for the crons this repository writes: ``*``, lists,
    ranges, and ``*/n``. Anything else returns None and the caller falls back to
    asking only that the workflow ran recently at all, rather than guessing.
    """
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    parts = spec.split()
    if len(parts) != 5:
        return None
    out = []
    for part, (lo, hi) in zip(parts, bounds):
        allowed: set[int] = set()
        for piece in part.split(","):
            step = 1
            if "/" in piece:
                piece, _, raw = piece.partition("/")
                if not raw.isdigit():
                    return None
                step = int(raw)
            if piece == "*":
                a, b = lo, hi
            elif "-" in piece:
                x, _, z = piece.partition("-")
                if not (x.isdigit() and z.isdigit()):
                    return None
                a, b = int(x), int(z)
            elif piece.isdigit():
                a = b = int(piece)
            else:
                return None
            allowed |= set(range(a, b + 1, step))
        if not allowed:
            return None
        out.append(allowed)
    return out


def crons_of(workflow: str) -> list[list[set[int]]]:
    """Every schedule in a workflow file, parsed. Empty when there is none."""
    path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / workflow
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        hit = re.search(r"-\s*cron:\s*[\"']([^\"']+)[\"']", line)
        if hit:
            fields = cron_fields(hit.group(1).strip())
            if fields is None:
                return []            # one unparsed line and the set is not trustworthy
            out.append(fields)
    return out


def firings_before(crons: list[list[set[int]]], when: datetime, count: int,
                   horizon_days: int = 21) -> list[datetime]:
    """The last ``count`` times any of these crons should have fired before ``when``.

    Walked minute by minute, which is thirty thousand cheap comparisons for a
    three-week horizon and needs no dependency. GitHub reads cron in UTC, and a
    day-of-month and day-of-week that are both restricted mean *either*, as in
    POSIX; none of this repository's crons do that, and the rule is applied
    anyway so that one added later is not silently misread.
    """
    found: list[datetime] = []
    t = when.replace(second=0, microsecond=0)
    for _ in range(horizon_days * 24 * 60):
        t -= timedelta(minutes=1)
        for mins, hours, dom, month, dow in crons:
            if t.minute not in mins or t.hour not in hours or t.month not in month:
                continue
            day_ok = (t.day in dom) if len(dom) == 31 or len(dow) == 7 else \
                     (t.day in dom or t.isoweekday() % 7 in dow)
            if len(dom) == 31 and len(dow) < 7:
                day_ok = t.isoweekday() % 7 in dow
            elif len(dow) == 7:
                day_ok = t.day in dom
            if day_ok:
                found.append(t)
                break
        if len(found) >= count:
            break
    return found


def check_workflows(rep: Report, repo: str) -> None:
    for wf in WATCHED:
        raw = gh(["run", "list", "--workflow", wf, "--repo", repo, "--limit", "30",
                  "--json", "conclusion,createdAt,status,databaseId"])
        if not raw:
            rep.bad(f"{wf} reachable", "could not list runs")
            continue
        runs = json.loads(raw)
        when = lambda r: datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        crons = crons_of(wf)
        now = datetime.now(UTC)
        due = firings_before(crons, now, MISSES_ALLOWED) if crons else []
        if due:
            # The oldest of the last few scheduled firings. Anything green or
            # still going since then means the schedule is being honoured.
            cutoff, said = due[-1], f"since {due[-1]:%d %b %H:%M}Z ({len(due)} due)"
        else:
            # No cron, or one this cannot read: fall back on a flat day.
            cutoff, said = now - timedelta(hours=24), "in 24h"
        recent = [r for r in runs if when(r) > cutoff]
        good = [r for r in recent if r["conclusion"] == "success"]
        running = [r for r in recent if r["status"] in ("in_progress", "queued")]
        if good or running:
            rep.ok(f"{wf} ran", f"{len(good)} green, {len(running)} running {said}")
        else:
            last = runs[0] if runs else None
            seen = f"{when(last):%d %b %H:%M}Z ({last['conclusion']})" if last else "never"
            rep.bad(f"{wf} ran", f"{MISSES_ALLOWED} scheduled firings {said} produced "
                                 f"nothing green; newest run {seen}")
        # A failure is a failure whenever it happened recently, on its own clock.
        failed = [r for r in runs if r["conclusion"] == "failure"
                  and when(r) > now - timedelta(hours=14)]
        if failed:
            ids = ", ".join(str(r["databaseId"]) for r in failed[:3])
            rep.bad(f"{wf} failures", f"{len(failed)} failed in the last 14h: {ids}")
        else:
            rep.ok(f"{wf} failures", "none")


def check_repo(rep: Report) -> None:
    """The heavy files, on the branch the workflows actually push to."""
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], timeout=120, capture_output=True)
        out = subprocess.run(["git", "ls-tree", "-r", "-l", "--name-only", "origin/main"],
                             capture_output=True, text=True, timeout=120)
        tracked = [p for p in out.stdout.splitlines() if HEAVY.search(p)]
    except Exception as exc:
        rep.bad("repository readable", f"{type(exc).__name__}: {exc}")
        return
    if tracked:
        rep.bad("evidence stays out of git", f"{len(tracked)} heavy files tracked on main, "
                                             f"e.g. {tracked[0]}")
    else:
        rep.ok("evidence stays out of git", "no rollouts or gepa tracked")
    try:
        big = subprocess.run(["git", "ls-tree", "-r", "-l", "origin/main"],
                             capture_output=True, text=True, timeout=120)
        over = [(int(l.split()[3]), l.split(maxsplit=4)[4])
                for l in big.stdout.splitlines() if l.split()[3].isdigit() and int(l.split()[3]) > 90e6]
    except Exception:
        over = []
    if over:
        rep.bad("nothing near GitHub's limit", f"{len(over)} file(s) over 90 MB, "
                                               f"largest {max(over)[0] / 1e6:.0f} MB")
    else:
        rep.ok("nothing near GitHub's limit", "")


def check_database(rep: Report) -> None:
    url = os.environ.get("SUPABASE_DB_URL", "")
    if not url:
        rep.ok("database", "no SUPABASE_DB_URL; skipped")
        return
    try:
        import psycopg2
    except ImportError:
        rep.ok("database", "psycopg2 not installed; skipped")
        return
    try:
        conn = psycopg2.connect(url, connect_timeout=20)
        cur = conn.cursor()
    except Exception as exc:
        rep.bad("database reachable", f"{type(exc).__name__}: {str(exc)[:80]}")
        return
    try:
        cur.execute("""select topic, max(created) from rsi.runs group by 1""")
        now = datetime.now(UTC)
        for topic, last in cur.fetchall():
            age = (now - last).total_seconds() / 3600 if last else 1e9
            if age > GENERATION_HOURS:
                rep.bad(f"{topic} is evolving", f"newest generation {age:.0f}h old")
            else:
                rep.ok(f"{topic} is evolving", f"newest generation {age:.0f}h old")
        cur.execute("""select topic, count(*), count(*) filter (where scored)
                         from rsi.live_forecasts where at > now() - interval '12 hours' group by 1""")
        live = {t: (n, s) for t, n, s in cur.fetchall()}
        for topic in ("kalshi-horizon-5m", "crypto-horizon-1m"):
            n, s = live.get(topic, (0, 0))
            if n == 0:
                rep.bad(f"{topic} is forecasting", "no live forecast in 12h")
            elif s == 0:
                rep.bad(f"{topic} is forecasting", f"{n} forecasts in 12h, none scored")
            else:
                rep.ok(f"{topic} is forecasting", f"{n} in 12h, {s} scored")
        # A column nothing writes. Checked on recent rows only: the three that
        # prompted this were empty for three months and the backfill is not
        # worth it, so what matters is that they are filled *now*.
        for (table, column), hours in sorted(WRITTEN_COLUMNS.items()):
            try:
                cur.execute(f"""select count(*), count({column}) from rsi.{table}
                                 where at > now() - interval '{int(hours)} hours'""")
                total, filled = cur.fetchone()
            except Exception as exc:                      # noqa: BLE001
                conn.rollback()
                rep.ok(f"{table}.{column} is written", f"could not check: {str(exc)[:60]}")
                continue
            where = f"{table}.{column} is written"
            if not total:
                rep.ok(where, f"no {table} rows in {hours}h to judge by")
            elif filled:
                rep.ok(where, f"{filled} of {total} rows in {hours}h")
            else:
                rep.bad(where, f"null on all {total} rows in the last {hours}h; "
                               f"whatever fills it has stopped or never started")

        # The overview reads a materialised view, so it is only as true as its
        # last refresh. `publish_runs.py` refreshes it after writing rollouts;
        # if that ever silently fails, the reader shows a stale generation and
        # looks perfectly healthy doing it. A run with held-out rollouts and no
        # row in the view is exactly that failure.
        cur.execute("""select 1 from pg_matviews
                        where schemaname = 'rsi' and matviewname = 'run_side_stats'""")
        if not cur.fetchone():
            rep.ok("pooled statistic is current", "run_side_stats not present; skipped")
        else:
            cur.execute("""select count(distinct r.topic || ':' || r.run_id)
                             from rsi.rollouts r
                             left join rsi.run_side_stats s
                               on s.topic = r.topic and s.run_id = r.run_id
                              and s.side = r.side and s.split = r.split
                            where s.run_id is null""")
            missing = cur.fetchone()[0]
            if missing:
                rep.bad("pooled statistic is current",
                        f"{missing} run(s) have rollouts the overview cannot see; "
                        f"refresh materialized view concurrently rsi.run_side_stats")
            else:
                rep.ok("pooled statistic is current", "every run is summed")
    except Exception as exc:
        rep.bad("database queries", f"{type(exc).__name__}: {str(exc)[:80]}")
    finally:
        conn.close()


def reader_credentials(rep: Report) -> tuple[str, str] | None:
    """The Supabase URL and anon key the deployed page is served with.

    Read from the page rather than from the watchdog's own environment, because
    the failure being checked for is the page's: a reader pointed at the wrong
    project, or served without a key, would pass a check that used secrets from
    somewhere else.
    """
    try:
        with urllib.request.urlopen(f"{READER}/", timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
            code = resp.status
    except Exception as exc:
        rep.bad("reader serves its page", f"{type(exc).__name__}: {str(exc)[:80]}")
        return None
    if code != 200:
        rep.bad("reader serves its page", f"HTTP {code}")
        return None
    url = re.search(r'url:\s*"([^"]+)"', html)
    key = re.search(r'key:\s*"([^"]+)"', html)
    if not url or not key or "__SUPA" in url.group(1) or "__SUPA" in key.group(1):
        rep.bad("reader is configured", "the served page has no Supabase URL or anon key")
        return None
    rep.ok("reader serves its page", f"{len(html) // 1024} KB")
    return url.group(1).rstrip("/"), key.group(1)


def check_reader(rep: Report) -> None:
    """Issue the overview's own queries, as the page's anon role, and time them.

    The two requests every topic's overview makes: the runs, and the pooled
    statistic per run and side. Both are read with the page's key through
    PostgREST, so a 500 here is the 500 a visitor sees, and a slow 200 is the
    warning that the next month of rows will turn it into one.
    """
    creds = reader_credentials(rep)
    if not creds:
        return
    base, key = creds
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}

    def fetch(query: str) -> tuple[float, int, str]:
        """Seconds, bytes, and what went wrong - empty when nothing did."""
        # `rsi_`, as the reader prefixes it: the schema is private and every
        # table reaches the browser through a public view of that name.
        req = urllib.request.Request(f"{base}/rest/v1/rsi_{query}", headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                size = len(resp.read())
            return time.monotonic() - started, size, ""
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:120]
            return time.monotonic() - started, 0, f"HTTP {exc.code}: {body}"
        except Exception as exc:
            return time.monotonic() - started, 0, f"{type(exc).__name__}: {str(exc)[:70]}"

    for topic in READER_TOPICS:
        notes, faults = [], []
        for what, query in (
            ("runs", f"runs?select={RUN_COLUMNS}&topic=eq.{topic}"
                     f"&order=created.desc&limit=200"),
            ("pooled skill", f"run_side_stats?select=run_id,side,removed,benchmark"
                             f"&split=eq.holdout&topic=eq.{topic}&limit=1000"),
        ):
            took, size, broke = fetch(query)
            budget = SLOW_SECONDS.get(what)
            if not broke and budget and took > budget:
                # Confirmed, not assumed: re-read before calling it slow.
                for _ in range(CONFIRM - 1):
                    took, size, broke = fetch(query)
                    if broke or took <= budget:
                        break
            if broke:
                faults.append(f"{what} {broke} after {took:.1f}s")
                break
            if budget and took > budget:
                faults.append(f"{what} took {took:.1f}s of the 3s a statement gets, twice over")
            notes.append(f"{what} {took:.2f}s/{size / 1024:.0f}KB")
        name = f"{topic} overview loads"
        if faults:
            rep.bad(name, "; ".join(faults))
        else:
            rep.ok(name, ", ".join(notes))


def prefixes_under(bucket: str, folder: str, *, runner=subprocess.run) -> tuple[set[str], str]:
    """The immediate sub-prefixes of ``folder``, and an error message or "".

    ``aws s3 ls`` on a prefix that holds nothing exits 1 and prints nothing at
    all, which is not a failure - it is the answer "empty", and the first version
    of this check reported it as "cannot list" the moment the new kalshi-jev
    prefix existed in the code and not yet in the bucket. A real problem -
    AccessDenied, NoSuchBucket, no credentials - says so on stderr.
    """
    out = runner(["aws", "s3", "ls", f"s3://{bucket}/{folder}"],
                 capture_output=True, text=True, timeout=120)
    stdout, stderr = (out.stdout or ""), (out.stderr or "").strip()
    if out.returncode != 0 and stderr:
        return set(), f"cannot list s3://{bucket}/{folder}: {stderr[:100]}"
    return ({folder + line.split("PRE", 1)[1].strip()
             for line in stdout.splitlines() if "PRE" in line}, "")


def backlog_of(path: Path = None) -> set[str]:
    """The generations `s3_backlog.txt` accounts for. Comments and blanks out."""
    path = path or S3_BACKLOG
    if not path.exists():
        return set()
    listed = {line.split("#", 1)[0].strip() for line in path.read_text().splitlines()}
    listed.discard("")
    return listed


def unseen(runs, holds: set[str], known: set[str], *, layout) -> tuple[list[str], list[str]]:
    """Which recorded generations have no trajectories, split old from new.

    ``runs`` are ``(topic, run_id)`` pairs, ``holds`` the prefixes the bucket
    actually has, ``known`` the backlog, and ``layout`` the key builder - passed
    in rather than imported so this can be read and tested without a bucket.

    A generation present under its pre-30-September key counts as present: that
    is where `fetch_run.fetch` looks second, so a fetch of it works.
    """
    missing = sorted({f"{t}/{i}" for t, i in runs
                      if layout(t, i) not in holds
                      and f"{S3_PREFIX}/{t}/{i.split('@', 1)[0]}/" not in holds})
    return [m for m in missing if m not in known], [m for m in missing if m in known]


def check_evidence(rep: Report) -> None:
    """Every recorded generation's trajectories, in the bucket.

    Read from the database rather than from a local checkout: the runs that
    matter are the ones the loop produced on a runner, which this machine has
    never seen. A generation younger than the grace period is not yet a fault,
    because the sync retries and the clocks differ.

    Skipped, not failed, without credentials or a bucket: the repository has run
    without them and must keep being able to.
    """
    from fetch_run import PREFIX, within         # the layout, from its owner

    bucket = os.environ.get("TRACE_BUCKET", "")
    if not bucket:
        rep.ok("evidence reaches S3", "no TRACE_BUCKET here; skipped")
        return
    # Not "is AWS_ACCESS_KEY_ID set": that is how Actions holds a credential and
    # not how a laptop does, so the check skipped itself on every machine where
    # `aws` was configured the ordinary way, and said "no credentials" while
    # `aws s3 ls` worked in the next shell. Ask the CLI instead.
    url = os.environ.get("SUPABASE_DB_URL", "")
    if not url:
        rep.ok("evidence reaches S3", "no SUPABASE_DB_URL to list generations; skipped")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=20)
        with conn, conn.cursor() as cur:
            cur.execute("""select topic, id, created from rsi.runs
                            where created < now() - interval '%s hours'
                              and created > now() - interval '60 days'
                            order by created desc""", (S3_GRACE_HOURS,))
            runs = cur.fetchall()
        conn.close()
    except Exception as exc:
        rep.bad("evidence reaches S3", f"could not list generations: {type(exc).__name__}: {str(exc)[:70]}")
        return
    if not runs:
        rep.ok("evidence reaches S3", "no generation old enough to have been uploaded")
        return

    # One listing per directory that expected keys actually live in - three
    # topics and, for Kalshi, its lineage - rather than one call per run.
    want = {(t, f"{PREFIX}/{t}/{within(t, i)}/") for t, i, _ in runs}
    holds: set[str] = set()
    listed: dict[str, int] = {}
    for folder in sorted({key.rsplit("/", 2)[0] + "/" for _, key in want}):
        found, problem = prefixes_under(bucket, folder)
        if problem:
            # No credential at all is a skip, not a fault: this repository has
            # run without one and must keep being able to. A credential that
            # exists and is refused is a fault.
            if re.search(r"credential|security token|AccessKeyId|Unable to locate", problem, re.I):
                rep.ok("evidence reaches S3", f"no usable AWS credential here; skipped ({problem[-60:]})")
                return
            rep.bad("evidence reaches S3", problem)
            return
        listed[folder] = len(found)
        holds |= found
    # Which folders were asked and what each held. Two readings of this bucket
    # disagreed on 30 September and the verdict did not say enough to tell which
    # was wrong, so it says it now.
    rep.note("listed", listed)
    rep.note("prefixes_in_s3", sorted(holds))

    fresh, stale = unseen([(t, i) for t, i, _ in runs], holds, backlog_of(),
                          layout=lambda t, i: f"{PREFIX}/{t}/{within(t, i)}/")
    if fresh or stale:
        rep.note("missing_from_s3", sorted(fresh + stale))
    if stale:
        # Said every hour, as a fact rather than a fault: these have one copy
        # each, on one machine, and that is worth seeing until it is not true.
        # What the file says they are, not what they were when it was written:
        # twenty of these were on one machine until 2 October and are now in the
        # bucket, and the rest have no copy at all.
        rep.ok("evidence backlog", f"{len(stale)} generation(s) have no trajectories "
                                   f"and are accounted for in {S3_BACKLOG.name}")
    if fresh:
        # The full list is in the JSON verdict rather than truncated into a
        # sentence, so acting on this does not mean re-running the check.
        rep.bad("evidence reaches S3",
                f"{len(fresh)} generation(s) have no trajectories under "
                f"s3://{bucket}/{PREFIX}/: {', '.join(fresh[:4])}"
                + (" ..." if len(fresh) > 4 else ""))
    else:
        rep.ok("evidence reaches S3",
               f"{len(runs) - len(stale)} of {len(runs)} generation(s) present"
               if stale else f"{len(runs)} generation(s) all present")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "YuxuanGao-MG/rsi-arena"))
    ap.add_argument("--json", default="", help="also write the verdict here")
    ap.add_argument("--skip-git", action="store_true", help="do not read the remote branch")
    ap.add_argument("--skip-reader", action="store_true", help="do not fetch the deployed page")
    ap.add_argument("--skip-s3", action="store_true", help="do not list the trace bucket")
    args = ap.parse_args()

    # Each check inside a guard. One of them raising used to end the run with a
    # traceback and no verdict: the workflow read the non-zero exit as "a fault
    # was found", opened an issue containing the traceback, and the seventeen
    # answers the other checks would have given were simply absent. A watchdog
    # that goes blind on one bad check is worse than one check fewer, so an
    # exception becomes that check's own fault and the rest still report.
    rep = Report()
    for name, run in (("workflows", lambda: check_workflows(rep, args.repo)),
                      ("repository", None if args.skip_git else lambda: check_repo(rep)),
                      ("database", lambda: check_database(rep)),
                      ("reader", None if args.skip_reader else lambda: check_reader(rep)),
                      ("evidence", None if args.skip_s3 else lambda: check_evidence(rep))):
        if run is None:
            continue
        try:
            run()
        except Exception as exc:                          # noqa: BLE001 - deliberate
            rep.bad(f"the {name} check itself ran",
                    f"{type(exc).__name__}: {str(exc)[:120]}")

    print(rep.render())
    if args.json:
        Path(args.json).write_text(json.dumps(rep.to_dict(), indent=2))
    return 1 if rep.faults else 0


if __name__ == "__main__":
    raise SystemExit(main())
