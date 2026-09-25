"""What each width the seeds offer would actually trade, over the committed sets.

Half width stopped being a by-product of the forecast's dispersion and became a
question the seeds ask outright, with five venue-appropriate levels from the
tick to well past the median path. The levels are only worth offering if they
bracket the range where the answer changes, and the question sets now carry the
realised path (``scripts/path_windows.py``), so that can be checked without a
network call and without running a harness.

For every window with a path, a quote centred on the instant's mid at each of
the seed's widths, filled the way ``Book.post`` fills one: the bid at the first
bar whose low reaches it, the ask at the first whose high does. Centring on the
mid rather than on a forecast is the honest baseline - a seed whose expected
move is a fraction of a tick is quoting around the mid whatever it says - and it
makes the table a property of the venue, not of a candidate.

``both`` is the round trip inside the window, which is the only fill that is
unambiguously good: the spread, twice the maker fee, no position left over.
``one`` is the adversely selected half - the path went through one side and kept
going - and it is the number the Kalshi book lost ninety-six per cent to.

    python scripts/width_fills.py                          # every topic
    python scripts/width_fills.py --topic crypto-horizon-1m
    python scripts/width_fills.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


class Topic(NamedTuple):
    """A topic's windows on disk, and the widths its seed offers."""

    name: str
    windows_dir: str
    unit: str
    relative: bool
    widths: tuple[float, ...]


#: The three sets, with the ``width`` question's ``values`` from each Jev seed.
TOPICS: tuple[Topic, ...] = (
    Topic("kalshi-horizon-5m", "benchmarks/windows", "cents", False, (1, 2, 4, 8, 16)),
    Topic("crypto-horizon-1m", "benchmarks/windows-crypto", "bps", True, (2, 5, 10, 20, 40)),
    Topic("news-equity-5m", "benchmarks/windows-news", "bps", True, (5, 12, 25, 50, 100)),
)


def span(mid: float, width: float, relative: bool) -> float:
    """A width in the topic's unit, as a distance in price space. ``quote_from``."""
    return mid * width / 1e4 if relative else width / 100.0


def travel(mid: float, high: float, low: float, relative: bool) -> float:
    """How far the path reached from the mid, in the unit: the wider side."""
    reach = max(high - mid, mid - low, 0.0)
    return reach / mid * 1e4 if relative else reach * 100.0


def windows(topic: Topic) -> Iterator[dict[str, Any]]:
    directory = ROOT / topic.windows_dir
    for path in sorted(directory.glob("*.json")):
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(rows, list):
            yield from (r for r in rows if isinstance(r, dict))


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    i = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[i]


def measure(topic: Topic) -> dict[str, Any]:
    """Fill shares per width, and where the path's reach sits, over one set."""
    both = [0] * len(topic.widths)
    one = [0] * len(topic.widths)
    reaches: list[float] = []
    counted = pathless = 0
    for row in windows(topic):
        bars = row.get("path")
        mid = row.get("mid_now")
        if not isinstance(mid, (int, float)) or not mid:
            continue
        if not isinstance(bars, list) or not bars:
            pathless += 1
            continue
        try:
            high = max(float(b["high"]) for b in bars)
            low = min(float(b["low"]) for b in bars)
        except (KeyError, TypeError, ValueError):
            pathless += 1
            continue
        counted += 1
        reaches.append(travel(float(mid), high, low, topic.relative))
        for i, width in enumerate(topic.widths):
            half = span(float(mid), width, topic.relative)
            bid, ask = float(mid) - half, float(mid) + half
            hit_bid, hit_ask = low <= bid, high >= ask
            if hit_bid and hit_ask:
                both[i] += 1
            elif hit_bid or hit_ask:
                one[i] += 1
    reaches.sort()
    return {
        "topic": topic.name, "unit": topic.unit, "windows": counted, "pathless": pathless,
        "reach": {f"p{int(q * 100)}": round(quantile(reaches, q), 2)
                  for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
        "widths": [{"width": w,
                    "both": round(both[i] / counted, 4) if counted else 0.0,
                    "one_side_only": round(one[i] / counted, 4) if counted else 0.0,
                    "no_fill": round((counted - both[i] - one[i]) / counted, 4) if counted else 0.0}
                   for i, w in enumerate(topic.widths)],
    }


def render(result: dict[str, Any]) -> str:
    lines = [f"{result['topic']}  ({result['windows']} windows with a path, "
             f"{result['pathless']} without)",
             "  reach from the mid, " + result["unit"] + ": "
             + ", ".join(f"{k} {v}" for k, v in result["reach"].items()),
             f"  {'half width':>12} {'both sides':>11} {'one side':>10} {'no fill':>9}"]
    for row in result["widths"]:
        lines.append(f"  {row['width']:>10} {result['unit'][:2]} {row['both']:>10.1%} "
                     f"{row['one_side_only']:>10.1%} {row['no_fill']:>9.1%}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fill rates per quoted half width, per topic.")
    p.add_argument("--topic", choices=[t.name for t in TOPICS], action="append",
                   help="one topic; repeatable. Default: all three.")
    p.add_argument("--json", action="store_true", help="the rows, not the table")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    chosen = [t for t in TOPICS if not args.topic or t.name in args.topic]
    results = [measure(t) for t in chosen]
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print("\n\n".join(render(r) for r in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
