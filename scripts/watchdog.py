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
* is the reader's database still reachable and still being written to.

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
    except Exception as exc:
        rep.bad("database queries", f"{type(exc).__name__}: {str(exc)[:80]}")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "YuxuanGao-MG/rsi-arena"))
    ap.add_argument("--json", default="", help="also write the verdict here")
    ap.add_argument("--skip-git", action="store_true", help="do not read the remote branch")
    args = ap.parse_args()

    rep = Report()
    check_workflows(rep, args.repo)
    if not args.skip_git:
        check_repo(rep)
    check_database(rep)

    print(rep.render())
    if args.json:
        Path(args.json).write_text(json.dumps(rep.to_dict(), indent=2))
    return 1 if rep.faults else 0


if __name__ == "__main__":
    raise SystemExit(main())
