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

The key is ``rsi-arena/<topic>/<run>/``, exactly what ``loop.yml`` uploads: the
topic as the loop names it, the run directory's basename, no runs-directory
prefix. A run's local home is its topic's runs directory
(``runs/crypto-horizon-1m/gen8``), which is what ``--dest`` defaults to - except
for the first topic's original lineage, which lives flat in ``runs/`` and needs
``--dest runs/gen12`` said out loud.
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


def key_for(topic: str, run_id: str, what: str = "all") -> str:
    """``rsi-arena/<topic>/<run>/``, narrowed to one subtree when asked.

    Always ends in a slash. ``aws s3 sync`` treats a prefix as a directory and a
    missing trailing slash makes it sync siblings whose names merely start the
    same way - ``gen1`` would pull ``gen10`` down with it.
    """
    if what not in WHAT:
        raise ValueError(f"what must be one of {', '.join(WHAT)}, not {what!r}")
    key = f"{PREFIX}/{topic}/{run_id}/"
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
    """The (uri, directory) pairs a fetch would sync. One pair; a list so the
    caller can print it, and so ``all`` could be split later without moving the
    seam."""
    return [(uri_for(bucket_of(bucket), topic, run_id, what),
             dest_for(topic, run_id, what, dest))]


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
    """
    written: list[Path] = []
    for uri, into in plan(topic, run_id, what=what, bucket=bucket, dest=dest):
        status = sync(uri, into, runner=runner, dry_run=dry_run, log=log)
        if status:
            raise SystemExit(f"aws s3 sync {uri} failed (exit {status}); "
                             "check the credentials and that the run reached S3")
        written.append(into)
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
