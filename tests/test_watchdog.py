"""The hourly check, offline.

The watchdog exists to catch the failures that look like nothing happening, and
it has now been the thing that was broken twice in one day: its first S3 check
crashed on an import and the workflow read the traceback as "one fault found",
so seventeen answers went missing and the run still said success. So the parts
with judgement in them are tested here - what counts as missing, what counts as
slow, and that one broken check cannot silence the others.

Offline like the rest of the suite: no network, no bucket, no database. The
reader check's `urlopen` and the evidence check's `aws` are the seams.
"""

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import watchdog                                                    # noqa: E402

LAYOUT = lambda t, i: f"{watchdog.S3_PREFIX}/{t}/{i}/"             # noqa: E731


# ---------------------------------------------------------------------------
# What counts as missing.

def test_a_generation_in_the_bucket_is_not_missing():
    fresh, stale = watchdog.unseen([("t", "gen1")], {LAYOUT("t", "gen1")}, set(), layout=LAYOUT)
    assert (fresh, stale) == ([], [])


def test_a_generation_absent_and_unlisted_is_a_fault():
    fresh, stale = watchdog.unseen([("t", "gen1")], set(), set(), layout=LAYOUT)
    assert fresh == ["t/gen1"] and stale == []


def test_a_generation_absent_and_listed_is_the_backlog():
    """Reported every hour as a fact; it must not fail the check forever."""
    fresh, stale = watchdog.unseen([("t", "gen1")], set(), {"t/gen1"}, layout=LAYOUT)
    assert fresh == [] and stale == ["t/gen1"]


def test_the_old_location_counts_as_present():
    """`fetch_run.fetch` falls back to it, so a fetch of this run works."""
    runs = [("kalshi-horizon-5m", "gen15@kalshi-jev")]
    holds = {f"{watchdog.S3_PREFIX}/kalshi-horizon-5m/gen15/"}
    layout = lambda t, i: f"{watchdog.S3_PREFIX}/{t}/kalshi-jev/{i.split('@')[0]}/"  # noqa: E731
    fresh, stale = watchdog.unseen(runs, holds, set(), layout=layout)
    assert (fresh, stale) == ([], [])


def test_the_backlog_file_is_parsed_without_its_prose(tmp_path):
    f = tmp_path / "backlog.txt"
    f.write_text("# a heading\n\nt/gen1   # 2026-09-20  local\n   \nt/gen2\n")
    assert watchdog.backlog_of(f) == {"t/gen1", "t/gen2"}


def test_a_missing_backlog_file_is_not_an_error(tmp_path):
    assert watchdog.backlog_of(tmp_path / "nope.txt") == set()


def test_the_shipped_backlog_lists_real_looking_generations():
    """A typo in this file silently forgives a genuine failure."""
    listed = watchdog.backlog_of()
    assert listed, "the backlog file lists nothing; the S3 gap was recorded"
    for entry in listed:
        topic, _, run = entry.partition("/")
        assert run, f"{entry!r} is not <topic>/<run>"
        assert topic in ("kalshi-horizon-5m", "news-equity-5m", "crypto-horizon-1m"), \
            f"{entry!r} names no topic this arena runs"


# ---------------------------------------------------------------------------
# One broken check does not silence the rest.

def test_a_raising_check_becomes_its_own_fault(monkeypatch, capsys):
    monkeypatch.setattr(watchdog, "check_workflows",
                        lambda rep, repo: rep.ok("workflows", "fine"))
    monkeypatch.setattr(watchdog, "check_database",
                        lambda rep: (_ for _ in ()).throw(RuntimeError("no driver")))
    monkeypatch.setattr(watchdog, "check_reader", lambda rep: rep.ok("reader", "fine"))
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--skip-git", "--skip-s3"])
    status = watchdog.main()
    printed = capsys.readouterr().out
    assert status == 1, "a crashed check is a fault"
    assert "the database check itself ran" in printed
    assert "no driver" in printed
    # and the point of the guard: the others still answered
    assert "ok    workflows" in printed and "ok    reader" in printed


def test_the_verdict_carries_what_a_check_found():
    rep = watchdog.Report()
    rep.bad("evidence reaches S3", "46 generations, 4 named here")
    rep.note("missing_from_s3", ["t/gen1", "t/gen2"])
    assert rep.to_dict()["found"]["missing_from_s3"] == ["t/gen1", "t/gen2"]


def test_a_report_with_nothing_found_has_no_found_key():
    rep = watchdog.Report()
    rep.ok("all", "well")
    assert "found" not in rep.to_dict()


# ---------------------------------------------------------------------------
# The reader check.

class Served:
    """A fake `urlopen`: a page, then a scripted answer per query."""

    PAGE = ('<html><script>window.RSI = { url: "https://p.supabase.co", '
            'key: "anon-key" };</script></html>')

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.asked.append(url)
        if not url.startswith("https://p.supabase.co"):
            return self._ok(self.PAGE)
        answer = self.answers.pop(0) if self.answers else ("ok", 0.0)
        kind = answer[0]
        if kind == "error":
            raise urllib.error.HTTPError(url, answer[1], "boom", {}, io.BytesIO(b"timeout"))
        return self._ok("[]")

    @staticmethod
    def _ok(body: str):
        class R:
            status = 200
            def read(self): return body.encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()


def test_a_500_from_the_overview_is_a_fault(monkeypatch):
    served = Served([("error", 500)])
    monkeypatch.setattr(watchdog.urllib.request, "urlopen", served)
    monkeypatch.setattr(watchdog, "READER_TOPICS", ("t",))
    rep = watchdog.Report()
    watchdog.check_reader(rep)
    assert [name for name, ok, _ in rep.faults] == ["t overview loads"]
    assert "500" in rep.faults[0][2]


def test_a_page_without_credentials_is_a_fault(monkeypatch):
    class Bare(Served):
        PAGE = '<html><script>window.RSI = { url: "__SUPA" + "BASE_URL__" };</script></html>'
    monkeypatch.setattr(watchdog.urllib.request, "urlopen", Bare([]))
    rep = watchdog.Report()
    watchdog.check_reader(rep)
    assert rep.faults and "Supabase URL" in rep.faults[0][2]


def test_slowness_is_confirmed_before_it_is_reported(monkeypatch):
    """A single slow reading is weather. A warm cache misled this fix once."""
    monkeypatch.setattr(watchdog, "READER_TOPICS", ("t",))
    # Negative, not zero: a fake answers in about no time at all, and `took > 0.0`
    # is then false on a fast machine, which would pass this test by not testing.
    monkeypatch.setattr(watchdog, "SLOW_SECONDS", {"pooled skill": -1.0})
    served = Served([])
    monkeypatch.setattr(watchdog.urllib.request, "urlopen", served)
    rep = watchdog.Report()
    watchdog.check_reader(rep)
    rest = [u for u in served.asked if "supabase" in u]
    assert sum("run_side_stats" in u for u in rest) == watchdog.CONFIRM, \
        "the aggregate query was not re-read before being called slow"
    assert sum("runs?select" in u for u in rest) == 1, \
        "the runs query is payload-bound and must not be judged on its clock"
