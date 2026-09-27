"""Refuse to stage a file GitHub will refuse to receive, and say which one.

    python scripts/check_commit_size.py runs/crypto-horizon-1m benchmarks/windows-crypto
    python scripts/check_commit_size.py "$RUNS_DIR" --bucket "$TRACE_BUCKET" \
        --topic crypto-horizon-1m --run-dir runs/crypto-horizon-1m/gen8

GitHub rejects any file over 100 MB at push time, after the commit is made. That
is the worst place to find out: on 25 and 26 September three scheduled crypto
generations reached a verdict, published it to the reader, and then died here -

    remote: error: File runs/crypto-horizon-1m/gen8/rollouts/candidate.holdout.json
    is 102.54 MB; this exceeds GitHub's file size limit of 100.00 MB

- and because the generation directory never reached the repository, ``rsi-arena
next`` could not see it and the lineage silently restarted from gen7. The push
failed loudly; the *consequence* was silent, which is the part that cost three
days of search.

So the check moves to before ``git add``, where it can name the file and where
nothing has been committed yet, and the limit is 90 MB rather than 100: a dump
that has reached ninety is one held-out rotation from being refused, and the
answer is the same either way - it belongs in S3, which it already is.

Files git already ignores are not reported. The point is to catch a *new* heavy
thing nobody has a rule for, not to re-litigate ``runs/**/rollouts/``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

#: Ten megabytes of headroom under GitHub's hundred. Reaching the real limit is
#: the failure this exists to prevent, so the warning has to come before it.
LIMIT_MB = 90.0

#: GitHub's own ceiling, quoted in the message so the number is not folklore.
GITHUB_MB = 100.0


def walk(paths: list[str]) -> list[Path]:
    """Every file under the given paths, which may be files or directories.

    Order is sorted so the message is the same on two runs over the same tree;
    a message that moves is a message people stop reading.
    """
    found: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            found.append(p)
        elif p.is_dir():
            found.extend(q for q in p.rglob("*") if q.is_file())
    return sorted(set(found))


def ignored(paths: list[Path], *, runner=subprocess.run) -> set[Path]:
    """Which of these git already ignores, via ``git check-ignore``.

    Fails open. Without git - a tarball, a test, a machine with no repository -
    every file is a candidate and the check is stricter, not weaker. Exit 1 from
    check-ignore means "none of them are ignored", which is not an error.
    """
    if not paths:
        return set()
    try:
        done = runner(["git", "check-ignore", "--stdin"],
                      input="\n".join(str(p) for p in paths) + "\n",
                      capture_output=True, text=True)
    except (OSError, ValueError):                       # pragma: no cover - no git here
        return set()
    return {Path(line) for line in (done.stdout or "").splitlines() if line.strip()}


def oversize(paths: list[str], *, limit_mb: float = LIMIT_MB,
             skip_ignored: bool = True, runner=subprocess.run) -> list[tuple[Path, int]]:
    """(path, bytes) for each file over the limit, largest first."""
    limit = int(limit_mb * 1048576)
    files = walk(paths)
    big = [(p, p.stat().st_size) for p in files if p.stat().st_size > limit]
    if big and skip_ignored:
        skip = ignored([p for p, _ in big], runner=runner)
        big = [(p, n) for p, n in big if p not in skip]
    return sorted(big, key=lambda pair: -pair[1])


def s3_location(path: Path, *, bucket: str = "", topic: str = "", run_dir: str = "") -> str:
    """Where this file's copy is in S3, as a sentence, or "" if it cannot be said.

    Only files inside the run directory this job wrote have a knowable key: the
    workflow syncs ``$run_dir`` to ``rsi-arena/<topic>/<basename>/`` and the key
    is the path relative to it. Anything else gets no claim made about it.
    """
    if not (bucket and topic and run_dir):
        return ""
    root = Path(run_dir)
    try:
        inside = path.resolve().relative_to(root.resolve())
    except ValueError:
        return ""
    return f"s3://{bucket}/rsi-arena/{topic}/{root.name}/{inside}"


def report(big: list[tuple[Path, int]], *, limit_mb: float = LIMIT_MB, bucket: str = "",
           topic: str = "", run_dir: str = "") -> str:
    """The message a person reads at 3am from a phone.

    Names every file, its size, and where the evidence actually lives, then says
    the one thing to do about it. No stack trace, no glob, no "see above".
    """
    lines = [f"{len(big)} file{'' if len(big) == 1 else 's'} over {limit_mb:g} MB; "
             f"GitHub refuses anything over {GITHUB_MB:g} MB, so nothing was staged."]
    for path, size in big:
        lines.append(f"  {path} is {size / 1048576:.2f} MB")
        where = s3_location(path, bucket=bucket, topic=topic, run_dir=run_dir)
        lines.append(f"    its copy is at {where}" if where else
                     "    no S3 copy is known for it (it is outside this run's directory)")
    lines += [
        "",
        "A file this size is evidence, not state a later generation reads. Either add",
        "it to .gitignore (runs/**/rollouts/ and runs/**/gepa/ already cover a run's",
        "heavy parts) or take its directory out of what this step commits. Read it back",
        "with scripts/fetch_run.py <topic> <run> --what rollouts.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files or directories about to be staged")
    ap.add_argument("--limit-mb", type=float, default=LIMIT_MB)
    ap.add_argument("--bucket", default=os.environ.get("TRACE_BUCKET", ""))
    ap.add_argument("--topic", default=os.environ.get("TOPIC", ""))
    ap.add_argument("--run-dir", default="", help="the run directory this job wrote")
    ap.add_argument("--include-ignored", action="store_true",
                    help="report files git ignores too (by default they are the point)")
    args = ap.parse_args(argv)

    big = oversize(args.paths, limit_mb=args.limit_mb,
                   skip_ignored=not args.include_ignored)
    if not big:
        return 0
    message = report(big, limit_mb=args.limit_mb, bucket=args.bucket,
                     topic=args.topic, run_dir=args.run_dir)
    print(message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        first = big[0][0]
        print(f"::error title=too big to commit::{first} is "
              f"{big[0][1] / 1048576:.2f} MB, over the {args.limit_mb:g} MB limit")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"### Nothing was committed - a file is too big\n\n```\n{message}\n```\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
