"""What the trajectories cost, by topic and by run, without opening the console.

    python scripts/s3_usage.py
    python scripts/s3_usage.py --depth 1              # per topic
    python scripts/s3_usage.py --prefix rsi-arena/crypto-horizon-1m/ --json

Now that ``rollouts/`` and ``gepa/`` live in S3 rather than in git, the
repository stops growing and something else starts. A gigabyte a month of
Standard storage is about two and a half cents, which is not the reason to look -
the reason is that nobody can see it. The repository announced its own size every
time somebody cloned it; a bucket announces nothing until the invoice.

So: one command that lists the arena's prefix and adds up what is under each
``<topic>/<run>``, which is exactly how ``loop.yml`` keys its uploads. It is a
read, it needs only ``s3:ListBucket``, and it is the answer to "is gen8 up there"
as well as to "what does this cost".

Uses boto3 when it is installed and the ``aws`` CLI when it is not, because the
workflow has the CLI and a laptop usually has neither on purpose.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from typing import Callable, Iterable, Iterator

#: Cents per gigabyte-month, S3 Standard in us-east-2 as of September 2026.
#: Printed as an estimate and labelled as one; a bucket's real bill also carries
#: requests and, past 50 TB, a cheaper tier neither of us will reach.
USD_PER_GB_MONTH = 0.023

#: What the loop writes under, so the listing is the arena's and not the bucket's.
DEFAULT_PREFIX = "rsi-arena/"


def bucket_of(explicit: str = "") -> str:
    bucket = explicit or os.environ.get("TRACE_BUCKET", "")
    if not bucket:
        raise SystemExit("no bucket: pass --bucket, or set TRACE_BUCKET")
    return bucket


def via_boto3(bucket: str, prefix: str) -> Iterator[tuple[str, int]]:
    """(key, size) for everything under the prefix, paginated by boto3."""
    import boto3                                          # noqa: PLC0415 - optional

    pages = boto3.client("s3").get_paginator("list_objects_v2")
    for page in pages.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            yield obj["Key"], int(obj["Size"])


def via_cli(bucket: str, prefix: str, *, runner=subprocess.run) -> Iterator[tuple[str, int]]:
    """The same, through ``aws s3api list-objects-v2``, one page at a time.

    ``--output json`` rather than ``s3 ls --summarize``: the summary form prints a
    total and throws the keys away, and the keys are what the grouping is for.
    """
    token = ""
    while True:
        cmd = ["aws", "s3api", "list-objects-v2", "--bucket", bucket,
               "--prefix", prefix, "--output", "json", "--max-items", "1000"]
        if token:
            cmd += ["--starting-token", token]
        done = runner(cmd, capture_output=True, text=True)
        if getattr(done, "returncode", 0):
            raise SystemExit(f"aws s3api list-objects-v2 failed: "
                             f"{(done.stderr or '').strip() or 'no output'}")
        page = json.loads(done.stdout or "{}")
        for obj in page.get("Contents") or []:
            yield obj["Key"], int(obj["Size"])
        token = page.get("NextToken") or ""
        if not token:
            return


def lister() -> Callable[[str, str], Iterable[tuple[str, int]]]:
    """boto3 if it imports, the CLI otherwise."""
    try:
        import boto3                                      # noqa: F401,PLC0415
    except ImportError:
        return via_cli
    return via_boto3


def group(objects: Iterable[tuple[str, int]], *, prefix: str = DEFAULT_PREFIX,
          depth: int = 2) -> dict[str, dict[str, int]]:
    """Objects and bytes under each of the first ``depth`` components after the prefix.

    ``depth=2`` is ``<topic>/<run>``, which is the unit the loop uploads and the
    unit a person deletes. A key with fewer components than that groups under
    what it has rather than being dropped: a stray object at the prefix root is
    exactly the thing worth seeing.
    """
    out: dict[str, dict[str, int]] = {}
    for key, size in objects:
        tail = key[len(prefix):] if key.startswith(prefix) else key
        parts = [p for p in tail.split("/") if p]
        name = "/".join(parts[:depth]) or "(root)"
        row = out.setdefault(name, {"objects": 0, "bytes": 0})
        row["objects"] += 1
        row["bytes"] += size
    return out


def render(groups: dict[str, dict[str, int]], *, bucket: str, prefix: str) -> str:
    """The table, widest column first, with a total and a monthly estimate."""
    if not groups:
        return f"nothing under s3://{bucket}/{prefix}"
    width = max(len(k) for k in groups)
    rows = [f"{'prefix':{width}}  {'objects':>8}  {'size':>10}",
            "-" * (width + 22)]
    for name, row in sorted(groups.items(), key=lambda kv: -kv[1]["bytes"]):
        rows.append(f"{name:{width}}  {row['objects']:8,}  {row['bytes'] / 1048576:8.1f} MB")
    n = sum(r["objects"] for r in groups.values())
    total = sum(r["bytes"] for r in groups.values())
    gb = total / 1073741824
    rows += ["-" * (width + 22),
             f"{'total':{width}}  {n:8,}  {total / 1048576:8.1f} MB",
             "",
             f"s3://{bucket}/{prefix} - {gb:.2f} GB, about "
             f"${gb * USD_PER_GB_MONTH:.2f} a month of Standard storage"]
    return "\n".join(rows)


def usage(bucket: str, prefix: str = DEFAULT_PREFIX, *, depth: int = 2,
          list_objects=None) -> dict[str, dict[str, int]]:
    """The grouping, listed however this machine can list."""
    return group((list_objects or lister())(bucket, prefix), prefix=prefix, depth=depth)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default="", help="defaults to $TRACE_BUCKET")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--depth", type=int, default=2,
                    help="1 groups by topic, 2 by topic/run (default: 2)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    bucket = bucket_of(args.bucket)
    groups = usage(bucket, args.prefix, depth=args.depth)
    if args.json:
        print(json.dumps({"bucket": bucket, "prefix": args.prefix, "groups": groups,
                          "objects": sum(r["objects"] for r in groups.values()),
                          "bytes": sum(r["bytes"] for r in groups.values())}, indent=2))
    else:
        print(render(groups, bucket=bucket, prefix=args.prefix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
