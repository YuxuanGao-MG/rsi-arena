"""Write a question set's files whole, or not at all.

A window file is read back by its existence: ``build_windows`` skips a group
whose file is on disk, because that is what makes a build resumable and what
stops a committed question set from being recomputed under a harness already
scored on it. So a half-written file is worse than a missing one — it is a
group that will never be rebuilt and can no longer be parsed.

``path.write_text`` leaves exactly that behind when the process is killed
between the truncate and the flush, which is what a weekly roll under a job
timeout invites. Writing to a sibling temp file and renaming it into place is
atomic on POSIX, so a reader sees the old file or the new one and never half
of either.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json_atomic(path: str | Path, payload: Any, **dumps: Any) -> Path:
    """``payload`` as JSON at ``path``, via a temp file in the same directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, **dumps))
    os.replace(tmp, path)
    return path


__all__ = ["write_json_atomic"]
