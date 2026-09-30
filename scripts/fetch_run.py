"""Bring a run's heavy parts back from S3, so a tool that reads them can run here.

    python scripts/fetch_run.py crypto-horizon-1m gen8
    python scripts/fetch_run.py crypto-horizon-1m gen8 --what rollouts
    python scripts/fetch_run.py kalshi-horizon-5m gen12 --what all --dest runs/gen12

The repository keeps a run's *light* files - ``manifest.json``, ``best.json``,
``books/``, ``valset.json`` - and nothing else; ``rollouts/`` and ``gepa/`` live
in S3 under ``s3://$TRACE_BUCKET/rsi-arena/<topic>/<run>/`` because a single
rollout dump with traces has passed a hundred megabytes and GitHub refuses those.
``docs/design.md`` ("What a later run needs from an earlier one") says why that
is the right line. This is the other half of the deal: the two scripts that do
read rollouts - ``backfill_books.py`` and ``compare_on_rollouts.py`` - call
``ensure_dir`` and ``ensure_file`` below when asked with ``--fetch``, and a
person who wants the whole thing runs this directly.

Deliberately not automatic. A backfill over twelve generations that quietly
downloads twelve gigabytes because one file was missing is a surprise, and the
surprise arrives as a bill. ``--fetch`` is the sentence in which somebody asks.

The key is ``rsi-arena/<topic>/[<lineage>/]<run>/``, exactly what ``loop.yml``
uploads. The lineage appears only when it is not the topic's own name, which
means one topic: Kalshi has two, ``runs/gen1..12`` from the Opus months and
``runs/kalshi-jev/gen1..16`` since, and ten pairs of those share a number. Under
the basename alone both lineages synced into one prefix, so uploading the backlog
would have mixed two generations' trajectories under one name and a fetch would
have brought back a blend. Nothing had collided yet only because none of the
colliding pairs had been uploaded at all; the reader's run ids have carried the
same distinction all along (``rsi_arena.loop.generation.qualified``), and this
was the one place still keyed by the bare name.

So ``runs/kalshi-jev/gen10`` is ``kalshi-horizon-5m/kalshi-jev/gen10/`` and the
Opus-era ``runs/gen10`` stays ``kalshi-horizon-5m/gen10/``; crypto and news,
whose runs directory is named after the topic, do not move. The kalshi-jev
generations uploaded before 30 September sit under the bare name, so a fetch
tries the lineage prefix and falls back to it. A run's local home is its topic's runs
directory (``runs/crypto-horizon-1m/gen8``), which is what ``--dest`` defaults to
- except for the first topic's original lineage, which lives flat in ``runs/``
and needs ``--dest runs/gen12`` said out loud.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsi_arena.topics import spec_of                          # noqa: E402

#: Everything the loop writes to S3 sits under this one prefix, so a bucket can
#: hold other things and ``s3_usage.py`` can still answer "what does the arena
#: cost".
PREFIX = "rsi-arena"

#: The parts of a run directory that are in S3 and not in git.
HEAVY = ("rollouts", "gepa")

#: What ``--what`` accepts.
WHAT = ("rollouts", "gepa", "all")


def bucket_of(explicit: str = "") -> str:
    """The bucket named on the command line, or ``$TRACE_BUCKET``.

    Raises rather than defaulting: guessing a bucket name is how a sync goes to
    somebody else's account and says nothing.
    """
    bucket = explicit or os.environ.get("TRACE_BUCKET", "")
    if not bucket:
        raise SystemExit("no bucket: pass --bucket, or set TRACE_BUCKET "
                         "(the repository secret's value; the bucket is rsi-arena-traces)")
    return bucket


def within(topic: str, run_id: str) -> str:
    """``<lineage>/<run>`` when the lineage needs saying, else just ``<run>``.

    A reader id may arrive qualified, ``gen10@kalshi-jev`` - that is how the
    overview and the database spell it - and means the same lineage. A bare id
    means the lineage the loop runs now, which is the topic's runs directory.
    ``runs/gen10``, the Opus lineage, is bare by construction and stays bare.
    """
    run, _, said = run_id.partition("@")
    if said:
        lineage = said
    else:
        # An unknown topic keys by the run alone rather than raising. This is a
        # path builder: it is called to print a key and to compare one, and a
        # caller that has already been told its topic is unknown should not be
        # told again by a KeyError from inside a string.
        try:
            lineage = Path(spec_of(topic).runs_dir).name
        except KeyError:
            lineage = topic
    return run if lineage in (topic, "runs", "") else f"{lineage}/{run}"


def key_for(topic: str, run_id: str, what: str = "all") -> str:
    """``rsi-arena/<topic>/<run id>/``, narrowed to one subtree when asked.

    Always ends in a slash. ``aws s3 sync`` treats a prefix as a directory and a
    missing trailing slash makes it sync siblings whose names merely start the
    same way - ``gen1`` would pull ``gen10`` down with it.
    """
    if what not in WHAT:
        raise ValueError(f"what must be one of {', '.join(WHAT)}, not {what!r}")
    key = f"{PREFIX}/{topic}/{within(topic, run_id)}/"
    return key if what == "all" else f"{key}{what}/"


def legacy_key_for(topic: str, run_id: str, what: str = "all") -> str:
    """Where a generation uploaded before 30 September sits: the bare run name.

    Returned even when it equals ``key_for`` - the caller compares the two and
    only falls back when they differ, so there is no second pointless sync.
    """
    if what not in WHAT:
        raise ValueError(f"what must be one of {', '.join(WHAT)}, not {what!r}")
    key = f"{PREFIX}/{topic}/{run_id.split('@', 1)[0]}/"
    return key if what == "all" else f"{key}{what}/"


def uri_for(bucket: str, topic: str, run_id: str, what: str = "all") -> str:
    return f"s3://{bucket}/{key_for(topic, run_id, what)}"


def dest_for(topic: str, run_id: str, what: str = "all", dest: str = "") -> Path:
    """Where the download lands: the topic's runs directory, or ``--dest``.

    ``--dest`` names the run directory, not the subtree, so
    ``--what rollouts --dest runs/gen12`` writes ``runs/gen12/rollouts``. That is
    the only reading under which ``--what all`` and ``--what rollouts`` put the
    same file in the same place.
    """
    root = Path(dest) if dest else Path(spec_of(topic).runs_dir) / run_id
    return root if what == "all" else root / what


def plan(topic: str, run_id: str, *, what: str = "all", bucket: str = "",
         dest: str = "") -> list[tuple[str, Path]]:
    """The (uri, directory) pairs a fetch would sync, in the order to try them.

    Two when the qualified key and the pre-30-September one differ: the second is
    only reached if the first synced nothing, which is what ``fetch`` does with
    this list. One otherwise.
    """
    b, into = bucket_of(bucket), dest_for(topic, run_id, what, dest)
    first = uri_for(b, topic, run_id, what)
    legacy = f"s3://{b}/{legacy_key_for(topic, run_id, what)}"
    return [(first, into)] if legacy == first else [(first, into), (legacy, into)]


def sync(uri: str, into: Path, *, runner=subprocess.run, dry_run: bool = False,
         log=print) -> int:
    """``aws s3 sync`` into a directory. Returns the exit status.

    ``sync`` rather than ``cp --recursive`` on the way down: a fetch is usually
    the second one somebody runs, and the first one already paid for most of it.
    """
    cmd = ["aws", "s3", "sync", uri, str(into), "--only-show-errors"]
    log(f"  {' '.join(cmd)}")
    if dry_run:
        return 0
    into.mkdir(parents=True, exist_ok=True)
    return int(getattr(runner(cmd), "returncode", 0) or 0)


def fetch(topic: str, run_id: str, *, what: str = "all", bucket: str = "",
          dest: str = "", runner=subprocess.run, dry_run: bool = False,
          log=print) -> list[Path]:
    """Download a run's heavy parts. Returns the directories written to.

    Raises ``SystemExit`` on a failed sync, because every caller wants the same
    answer: a fetch that did not fetch is not a thing to carry on from.

    ``plan`` may offer a second prefix - where generations uploaded before
    30 September sit - and it is tried only when the first brought nothing back.
    ``aws s3 sync`` from a prefix that does not exist exits zero and writes no
    files, so "brought nothing back" is the question to ask, not the status.
    """
    written: list[Path] = []
    candidates = plan(topic, run_id, what=what, bucket=bucket, dest=dest)
    for n, (uri, into) in enumerate(candidates):
        before = {p for p in into.rglob("*") if p.is_file()} if into.exists() else set()
        status = sync(uri, into, runner=runner, dry_run=dry_run, log=log)
        if status:
            raise SystemExit(f"aws s3 sync {uri} failed (exit {status}); "
                             "check the credentials and that the run reached S3")
        written.append(into)
        if dry_run:
            continue
        got = {p for p in into.rglob("*") if p.is_file()} if into.exists() else set()
        if got - before or n == len(candidates) - 1:
            break
        log(f"  nothing under {uri}; trying where it sat before 30 September")
    return written


# -- what the two readers call ----------------------------------------------

def ensure_dir(directory: Path, topic: str, run_id: str = "", *, what: str = "rollouts",
               fetch_missing: bool = False, bucket: str = "", runner=subprocess.run,
               log=print) -> list[Path]:
    """The JSON files in a run's ``rollouts/`` or ``gepa/``, fetched if it is empty.

    An empty directory counts as absent: ``git rm --cached`` left one behind on
    every checkout that had the files before, and a backfill that read "the
    directory exists" as "the dumps are here" would report no rollouts and exit
    green.
    """
    found = sorted(directory.glob("*.json"))
    if found or not fetch_missing:
        return found
    log(f"  {directory} is empty; fetching {what} for {topic}/{run_id or directory.parent.name}")
    fetch(topic, run_id or directory.parent.name, what=what, bucket=bucket,
          dest=str(directory.parent), runner=runner, log=log)
    return sorted(directory.glob("*.json"))


def ensure_file(path: Path, topic: str, *, fetch_missing: bool = False, bucket: str = "",
                runner=subprocess.run, log=print) -> Path:
    """One rollout dump, fetching its run's whole ``rollouts/`` if it is absent.

    ``path`` is ``<runs dir>/<run>/rollouts/<side>.<split>.json``, so the run id
    is two directories up. Fetching the sibling dumps as well is deliberate: they
    come from one sync, the paired comparison usually wants the other side next,
    and asking S3 twice costs more than the bytes.
    """
    if path.exists() or not fetch_missing:
        return path
    run_id = path.parent.parent.name
    log(f"  {path} is absent; fetching rollouts for {topic}/{run_id}")
    fetch(topic, run_id, what="rollouts", bucket=bucket, dest=str(path.parent.parent),
          runner=runner, log=log)
    if not path.exists():
        raise SystemExit(f"{path} is still absent after fetching "
                         f"{uri_for(bucket_of(bucket), topic, run_id, 'rollouts')}")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("topic", help="the loop's name for it, e.g. crypto-horizon-1m")
    ap.add_argument("run_id", help="the run directory's basename, e.g. gen8")
    ap.add_argument("--what", default="all", choices=WHAT,
                    help="which heavy part; all is the whole run directory (default: all)")
    ap.add_argument("--dest", default="",
                    help="the run directory to write into; defaults to the topic's")
    ap.add_argument("--bucket", default="", help="defaults to $TRACE_BUCKET")
    ap.add_argument("--dry-run", action="store_true", help="print the sync and stop")
    args = ap.parse_args(argv)

    for uri, into in plan(args.topic, args.run_id, what=args.what,
                          bucket=args.bucket, dest=args.dest):
        print(f"{uri} -> {into}")
    written = fetch(args.topic, args.run_id, what=args.what, bucket=args.bucket,
                    dest=args.dest, dry_run=args.dry_run)
    if not args.dry_run:
        for into in written:
            files = sum(1 for _ in into.rglob("*") if _.is_file())
            size = sum(p.stat().st_size for p in into.rglob("*") if p.is_file())
            print(f"{into}: {files} files, {size / 1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
