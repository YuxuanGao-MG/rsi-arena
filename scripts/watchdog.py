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

UTC = timezone.utc

#: How long a workflow may go without a successful run before that is a fault.
#: Generous on purpose: GitHub's scheduler is best-effort and a single missed
#: firing is normal, while a day of silence is not. The live sweeps run every
#: four hours and last nearly four, so a gap beyond nine hours means two
#: consecutive firings were dropped.
QUIET_HOURS = {
    "loop.yml": 14,
    "live.yml": 9,
    "live-crypto.yml": 9,
    "live-news.yml": 14,
}

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

#: Paths that must never be tracked: they are the ones that grow past what
#: GitHub will accept, and every time they have come back a push has died.
HEAVY = re.compile(r"runs/.*/(rollouts|gepa)/|runs/.*\.rollouts\.json$")


@dataclass
class Report:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def ok(self, name: str, detail: str = "") -> None:
        self.checks.append((name, True, detail))

    def bad(self, name: str, detail: str) -> None:
        self.checks.append((name, False, detail))

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
        return {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                "faults": len(self.faults),
                "checks": [{"name": n, "ok": g, "detail": d} for n, g, d in self.checks]}


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


def check_workflows(rep: Report, repo: str) -> None:
    for wf, hours in QUIET_HOURS.items():
        raw = gh(["run", "list", "--workflow", wf, "--repo", repo, "--limit", "20",
                  "--json", "conclusion,createdAt,status,databaseId"])
        if not raw:
            rep.bad(f"{wf} reachable", "could not list runs")
            continue
        runs = json.loads(raw)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        recent = [r for r in runs if datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) > cutoff]
        good = [r for r in recent if r["conclusion"] == "success"]
        running = [r for r in recent if r["status"] in ("in_progress", "queued")]
        if good or running:
            rep.ok(f"{wf} ran", f"{len(good)} green, {len(running)} running in the last {hours}h")
        else:
            last = runs[0] if runs else None
            when = last["createdAt"][:16].replace("T", " ") if last else "never"
            rep.bad(f"{wf} ran", f"nothing green in {hours}h; newest run {when} "
                                 f"({last['conclusion'] if last else 'none'})")
        failed = [r for r in recent if r["conclusion"] == "failure"]
        if failed:
            ids = ", ".join(str(r["databaseId"]) for r in failed[:3])
            rep.bad(f"{wf} failures", f"{len(failed)} failed in the last {hours}h: {ids}")
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "YuxuanGao-MG/rsi-arena"))
    ap.add_argument("--json", default="", help="also write the verdict here")
    ap.add_argument("--skip-git", action="store_true", help="do not read the remote branch")
    ap.add_argument("--skip-reader", action="store_true", help="do not fetch the deployed page")
    args = ap.parse_args()

    rep = Report()
    check_workflows(rep, args.repo)
    if not args.skip_git:
        check_repo(rep)
    check_database(rep)
    if not args.skip_reader:
        check_reader(rep)

    print(rep.render())
    if args.json:
        Path(args.json).write_text(json.dumps(rep.to_dict(), indent=2))
    return 1 if rep.faults else 0


if __name__ == "__main__":
    raise SystemExit(main())
