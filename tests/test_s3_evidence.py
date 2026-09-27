"""The split between what the repository keeps and what S3 holds, enforced.

Four things are checked here, and each of them is a thing that has already gone
wrong or would have:

* the size guard names the offending file, because the push-time error named it
  and nobody read the push-time error;
* ``.gitignore`` actually excludes ``runs/**/rollouts/`` and ``runs/**/gepa/`` at
  every depth - the rule it replaced was ``runs/*/gepa/...``, which matched
  nothing under ``runs/<topic>/<gen>/`` and left 26,995 files tracked;
* ``fetch_run`` builds the key ``loop.yml`` uploads to, with its trailing slash,
  because ``s3://b/.../gen1`` without one syncs ``gen10`` as well;
* ``s3_usage`` adds up by ``<topic>/<run>``, and ``backfill_books --fetch`` asks
  for the run it was given and not for some other one.

Offline throughout: no credentials, no boto3, no ``aws`` binary. Every S3 call
goes through an injected runner or lister.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    """A script under ``scripts/`` as a module. They are entry points, not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_script("check_commit_size")
fetch_run = load_script("fetch_run")
s3_usage = load_script("s3_usage")


class FakeRun:
    """What ``subprocess.run`` returns, and what was asked of it."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return self


def write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    return path


MB = 1048576


# ---------------------------------------------------------------------------
# The guard.

def test_a_file_over_the_limit_fails_and_is_named(tmp_path):
    big = write(tmp_path / "rollouts" / "candidate.holdout.json", 102 * MB)
    write(tmp_path / "manifest.json", 2048)

    found = guard.oversize([str(tmp_path)], limit_mb=90, skip_ignored=False)
    assert [p for p, _ in found] == [big]

    message = guard.report(found, limit_mb=90)
    assert "candidate.holdout.json" in message
    assert "102.00 MB" in message
    assert "nothing was staged" in message


def test_a_file_under_the_limit_passes(tmp_path):
    write(tmp_path / "rollouts" / "baseline.train.json", 20 * MB)
    assert guard.oversize([str(tmp_path)], limit_mb=90, skip_ignored=False) == []
    assert guard.main([str(tmp_path), "--limit-mb", "90", "--include-ignored"]) == 0


def test_main_exits_non_zero_and_prints_the_name(tmp_path, capsys):
    write(tmp_path / "huge.json", 95 * MB)
    assert guard.main([str(tmp_path), "--limit-mb", "90", "--include-ignored"]) == 1
    assert "huge.json" in capsys.readouterr().err


def test_the_message_says_where_the_s3_copy_is(tmp_path):
    run_dir = tmp_path / "runs" / "crypto-horizon-1m" / "gen8"
    big = write(run_dir / "rollouts" / "candidate.holdout.json", 101 * MB)
    message = guard.report([(big, big.stat().st_size)], bucket="rsi-arena-traces",
                           topic="crypto-horizon-1m", run_dir=str(run_dir))
    assert ("s3://rsi-arena-traces/rsi-arena/crypto-horizon-1m/gen8/"
            "rollouts/candidate.holdout.json") in message


def test_a_file_outside_the_run_directory_makes_no_s3_claim(tmp_path):
    big = write(tmp_path / "benchmarks" / "windows" / "huge.json", 91 * MB)
    message = guard.report([(big, big.stat().st_size)], bucket="b", topic="t",
                           run_dir=str(tmp_path / "runs" / "t" / "gen1"))
    assert "no S3 copy is known" in message


def test_files_git_already_ignores_are_not_reported(tmp_path):
    """The guard is for a new heavy thing nobody has a rule for."""
    big = write(tmp_path / "rollouts" / "candidate.holdout.json", 102 * MB)
    runner = FakeRun(stdout=f"{big}\n")
    assert guard.oversize([str(tmp_path)], limit_mb=90, runner=runner) == []
    assert runner.calls == [["git", "check-ignore", "--stdin"]]


def test_no_git_means_stricter_not_weaker(tmp_path):
    """Without a repository every file is a candidate; failing open the other way
    would make the guard silent exactly where it cannot be checked."""
    big = write(tmp_path / "huge.json", 95 * MB)

    def explode(*a, **kw):
        raise OSError("no git here")

    assert [p for p, _ in guard.oversize([str(tmp_path)], limit_mb=90, runner=explode)] == [big]


def test_the_default_limit_leaves_room_under_githubs():
    assert guard.LIMIT_MB < guard.GITHUB_MB


# ---------------------------------------------------------------------------
# .gitignore says what we think it says.

HEAVY = [
    "runs/gen1/rollouts/baseline.holdout.json",
    "runs/gen12/gepa/candidates.json",
    "runs/crypto-horizon-1m/gen8/rollouts/candidate.holdout.json",
    "runs/crypto-horizon-1m/gen8/gepa/gepa_state.bin",
    "runs/crypto-horizon-1m/gen8/gepa/generated_best_outputs_valset/task_0/iter_0_prog_0.json",
    "runs/kalshi-jev/gen5/rollouts/candidate.holdout.json",
    "runs/news-equity-5m/gen3/gepa/run_log.json",
]

KEPT = [
    "runs/archive.json",
    "runs/archive.crypto-horizon-1m.json",
    "runs/scoreboard.crypto-horizon-1m.json",
    "runs/crypto-horizon-1m/gen8/manifest.json",
    "runs/crypto-horizon-1m/gen8/best.json",
    "runs/crypto-horizon-1m/gen8/valset.json",
    "runs/crypto-horizon-1m/gen8/books/candidate.holdout.json",
    "runs/gen12/manifest.json",
    "runs/kalshi-jev/gen5/books/baseline.train.json",
]


def check_ignore(paths: list[str]) -> set[str]:
    """Which of these the repository's own .gitignore excludes.

    Asks git rather than re-implementing the pattern language: the bug being
    guarded against was a misreading of what ``runs/*/gepa/`` matches, and a
    hand-rolled matcher would reproduce the misreading.
    """
    done = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                          cwd=ROOT, input="\n".join(paths) + "\n",
                          capture_output=True, text=True)
    return {line.strip() for line in done.stdout.splitlines() if line.strip()}


@pytest.mark.parametrize("path", HEAVY)
def test_gitignore_excludes_the_heavy_parts(path):
    assert path in check_ignore(HEAVY), f"{path} should be ignored but is not"


@pytest.mark.parametrize("path", KEPT)
def test_gitignore_keeps_what_a_later_run_reads(path):
    assert path not in check_ignore(KEPT), f"{path} is ignored and a later run needs it"


def test_the_rule_is_written_for_every_depth():
    """``runs/*/gepa/`` matched nothing under ``runs/<topic>/<gen>/gepa/`` and
    26,995 files were tracked because of it. ``**`` is the whole fix."""
    text = (ROOT / ".gitignore").read_text()
    assert "runs/**/rollouts/" in text
    assert "runs/**/gepa/" in text


# ---------------------------------------------------------------------------
# fetch_run: the key is the one loop.yml writes to.

def test_the_key_is_the_one_the_loop_uploads_to():
    assert fetch_run.key_for("crypto-horizon-1m", "gen8") == "rsi-arena/crypto-horizon-1m/gen8/"
    assert (fetch_run.key_for("crypto-horizon-1m", "gen8", "rollouts")
            == "rsi-arena/crypto-horizon-1m/gen8/rollouts/")
    assert fetch_run.key_for("kalshi-horizon-5m", "gen12", "gepa") == \
        "rsi-arena/kalshi-horizon-5m/gen12/gepa/"


def test_every_key_ends_in_a_slash():
    """Without it a sync of gen1 pulls gen10 down with it."""
    for what in fetch_run.WHAT:
        assert fetch_run.key_for("t", "gen1", what).endswith("/")


def test_an_unknown_what_is_refused():
    with pytest.raises(ValueError, match="rollouts, gepa, all"):
        fetch_run.key_for("t", "gen1", "books")


def test_the_uri_carries_the_bucket():
    assert (fetch_run.uri_for("rsi-arena-traces", "crypto-horizon-1m", "gen8", "rollouts")
            == "s3://rsi-arena-traces/rsi-arena/crypto-horizon-1m/gen8/rollouts/")


def test_the_destination_defaults_to_the_topics_runs_directory():
    assert fetch_run.dest_for("crypto-horizon-1m", "gen8") == \
        Path("runs/crypto-horizon-1m/gen8")
    assert fetch_run.dest_for("crypto-horizon-1m", "gen8", "rollouts") == \
        Path("runs/crypto-horizon-1m/gen8/rollouts")


def test_dest_names_the_run_directory_not_the_subtree():
    """So --what all and --what rollouts put the same file in the same place."""
    assert fetch_run.dest_for("t", "gen8", "rollouts", "runs/gen12") == Path("runs/gen12/rollouts")
    assert fetch_run.dest_for("t", "gen8", "all", "runs/gen12") == Path("runs/gen12")


def test_no_bucket_is_a_refusal_not_a_guess(monkeypatch):
    monkeypatch.delenv("TRACE_BUCKET", raising=False)
    with pytest.raises(SystemExit, match="no bucket"):
        fetch_run.bucket_of()
    monkeypatch.setenv("TRACE_BUCKET", "from-the-environment")
    assert fetch_run.bucket_of() == "from-the-environment"
    assert fetch_run.bucket_of("explicit") == "explicit"


def test_fetch_runs_one_sync_and_says_where(tmp_path):
    runner = FakeRun()
    written = fetch_run.fetch("crypto-horizon-1m", "gen8", what="rollouts",
                              bucket="rsi-arena-traces", dest=str(tmp_path / "gen8"),
                              runner=runner, log=lambda *a: None)
    assert written == [tmp_path / "gen8" / "rollouts"]
    assert runner.calls == [[
        "aws", "s3", "sync",
        "s3://rsi-arena-traces/rsi-arena/crypto-horizon-1m/gen8/rollouts/",
        str(tmp_path / "gen8" / "rollouts"), "--only-show-errors"]]


def test_a_failed_sync_is_not_carried_on_from(tmp_path):
    with pytest.raises(SystemExit, match="failed"):
        fetch_run.fetch("t", "gen8", bucket="b", dest=str(tmp_path),
                        runner=FakeRun(returncode=1), log=lambda *a: None)


def test_a_dry_run_asks_for_nothing(tmp_path):
    runner = FakeRun()
    fetch_run.fetch("t", "gen8", bucket="b", dest=str(tmp_path), runner=runner,
                    dry_run=True, log=lambda *a: None)
    assert runner.calls == []


# ---------------------------------------------------------------------------
# fetch_run.ensure_*: what the two readers call.

def test_a_present_dump_is_not_fetched(tmp_path):
    path = tmp_path / "gen8" / "rollouts" / "baseline.train.json"
    write(path, 10)
    runner = FakeRun()
    assert fetch_run.ensure_file(path, "t", fetch_missing=True, bucket="b", runner=runner) == path
    assert runner.calls == []


def test_an_absent_dump_is_left_alone_without_the_flag(tmp_path):
    path = tmp_path / "gen8" / "rollouts" / "baseline.train.json"
    runner = FakeRun()
    fetch_run.ensure_file(path, "t", fetch_missing=False, runner=runner)
    assert runner.calls == []


def test_an_absent_dump_fetches_its_runs_rollouts(tmp_path):
    """The run id is two directories up, and the siblings come along: the paired
    comparison wants the other side next and it is the same sync."""
    path = tmp_path / "gen8" / "rollouts" / "baseline.train.json"

    class Writes(FakeRun):
        def __call__(self, cmd, **kw):
            super().__call__(cmd, **kw)
            write(path, 10)
            return self

    runner = Writes()
    fetch_run.ensure_file(path, "crypto-horizon-1m", fetch_missing=True,
                          bucket="rsi-arena-traces", runner=runner, log=lambda *a: None)
    assert runner.calls[0][3] == \
        "s3://rsi-arena-traces/rsi-arena/crypto-horizon-1m/gen8/rollouts/"


def test_an_empty_rollouts_directory_counts_as_absent(tmp_path):
    """`git rm --cached` leaves the directory behind on every checkout that had
    the files, and "the directory exists" is not "the dumps are here"."""
    directory = tmp_path / "gen8" / "rollouts"
    directory.mkdir(parents=True)
    runner = FakeRun()
    fetch_run.ensure_dir(directory, "crypto-horizon-1m", "gen8", fetch_missing=True,
                         bucket="b", runner=runner, log=lambda *a: None)
    assert runner.calls[0][3] == "s3://b/rsi-arena/crypto-horizon-1m/gen8/rollouts/"


# ---------------------------------------------------------------------------
# backfill_books --fetch asks for the run it was given.

def test_backfill_books_fetch_asks_for_the_right_key(tmp_path, monkeypatch):
    backfill_books = load_script("backfill_books")
    run_dir = tmp_path / "crypto-horizon-1m" / "gen8"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps(
        {"topic": "crypto-horizon-1m", "run_dir": str(run_dir), "settings": {}}))

    asked: list[str] = []

    def fake_sync(uri, into, **kw):
        asked.append(uri)
        return 0

    monkeypatch.setattr(backfill_books.fetch_run, "sync", fake_sync)
    monkeypatch.setenv("TRACE_BUCKET", "rsi-arena-traces")
    # Nothing arrives, so the backfill reports no rollouts and stops - the point
    # is the key it asked for, not the replay.
    assert backfill_books.backfill(run_dir, fetch=True, log=lambda *a: None) == {}
    assert asked == ["s3://rsi-arena-traces/rsi-arena/crypto-horizon-1m/gen8/rollouts/"]


def test_backfill_books_without_the_flag_asks_for_nothing(tmp_path, monkeypatch):
    backfill_books = load_script("backfill_books")
    run_dir = tmp_path / "crypto-horizon-1m" / "gen8"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps(
        {"topic": "crypto-horizon-1m", "run_dir": str(run_dir), "settings": {}}))

    def explode(*a, **kw):
        raise AssertionError("a backfill must not download twelve gigabytes unasked")

    monkeypatch.setattr(backfill_books.fetch_run, "sync", explode)
    assert backfill_books.backfill(run_dir, fetch=False, log=lambda *a: None) == {}


# ---------------------------------------------------------------------------
# s3_usage: the summing.

OBJECTS = [
    ("rsi-arena/crypto-horizon-1m/gen8/rollouts/candidate.holdout.json", 107_244_581),
    ("rsi-arena/crypto-horizon-1m/gen8/rollouts/baseline.holdout.json", 90_516_391),
    ("rsi-arena/crypto-horizon-1m/gen8/manifest.json", 15_172),
    ("rsi-arena/crypto-horizon-1m/gen7/rollouts/candidate.holdout.json", 27_264_323),
    ("rsi-arena/kalshi-horizon-5m/gen12/gepa/candidates.json", 44_688),
    ("rsi-arena/live/crypto-horizon-1m/2026-09-26/crypto-forecasts.jsonl", 2_048),
]


def test_usage_sums_by_topic_and_run():
    groups = s3_usage.group(OBJECTS)
    assert groups["crypto-horizon-1m/gen8"] == {
        "objects": 3, "bytes": 107_244_581 + 90_516_391 + 15_172}
    assert groups["crypto-horizon-1m/gen7"] == {"objects": 1, "bytes": 27_264_323}
    assert groups["kalshi-horizon-5m/gen12"] == {"objects": 1, "bytes": 44_688}
    assert groups["live/crypto-horizon-1m"] == {"objects": 1, "bytes": 2_048}


def test_usage_can_group_by_topic_alone():
    groups = s3_usage.group(OBJECTS, depth=1)
    assert set(groups) == {"crypto-horizon-1m", "kalshi-horizon-5m", "live"}
    assert groups["crypto-horizon-1m"]["objects"] == 4
    assert sum(r["bytes"] for r in groups.values()) == sum(n for _, n in OBJECTS)


def test_a_stray_object_at_the_root_is_shown_not_dropped():
    assert s3_usage.group([("rsi-arena/loose.json", 12)]) == {
        "loose.json": {"objects": 1, "bytes": 12}}
    assert s3_usage.group([("rsi-arena/", 0)]) == {"(root)": {"objects": 1, "bytes": 0}}


def test_a_key_outside_the_prefix_is_grouped_by_its_own_path():
    """A bucket may hold things the arena did not put there; they are shown as
    they are rather than folded into whatever the prefix would have named."""
    assert s3_usage.group([("other/place/thing.json", 5)]) == {
        "other/place": {"objects": 1, "bytes": 5}}


def test_render_names_the_bucket_the_total_and_the_bill():
    text = s3_usage.render(s3_usage.group(OBJECTS), bucket="rsi-arena-traces",
                           prefix="rsi-arena/")
    assert "crypto-horizon-1m/gen8" in text
    assert "s3://rsi-arena-traces/rsi-arena/" in text
    assert "a month of Standard storage" in text
    # Largest first, so the thing worth deleting is the first thing read.
    rows = [line.split()[0] for line in text.splitlines()[2:5]]
    assert rows[0] == "crypto-horizon-1m/gen8"


def test_render_says_so_when_there_is_nothing_there():
    assert "nothing under" in s3_usage.render({}, bucket="b", prefix="p/")


def test_usage_takes_any_lister():
    """So the summing is testable without boto3, credentials or a network."""
    groups = s3_usage.usage("bucket", "rsi-arena/", list_objects=lambda b, p: OBJECTS)
    assert groups["crypto-horizon-1m/gen8"]["objects"] == 3


def test_the_cli_lister_pages_through_and_stops():
    pages = [
        {"Contents": [{"Key": "rsi-arena/a/gen1/x", "Size": 5}], "NextToken": "t1"},
        {"Contents": [{"Key": "rsi-arena/a/gen1/y", "Size": 7}]},
    ]
    runner = FakeRun()

    def fake(cmd, **kw):
        runner.calls.append(list(cmd))
        return FakeRun(stdout=json.dumps(pages[len(runner.calls) - 1]))

    got = list(s3_usage.via_cli("bucket", "rsi-arena/", runner=fake))
    assert got == [("rsi-arena/a/gen1/x", 5), ("rsi-arena/a/gen1/y", 7)]
    assert len(runner.calls) == 2
    assert "--starting-token" in runner.calls[1] and "t1" in runner.calls[1]


def test_a_failed_listing_says_what_the_cli_said():
    with pytest.raises(SystemExit, match="AccessDenied"):
        list(s3_usage.via_cli("bucket", "p/",
                              runner=lambda *a, **kw: FakeRun(returncode=1,
                                                             stderr="AccessDenied")))


def test_usage_needs_a_bucket(monkeypatch):
    monkeypatch.delenv("TRACE_BUCKET", raising=False)
    with pytest.raises(SystemExit, match="no bucket"):
        s3_usage.bucket_of()
