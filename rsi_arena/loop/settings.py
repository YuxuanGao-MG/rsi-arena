"""Everything a run of the loop is parameterised by, in one place."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Settings:
    topic: str = "kalshi-horizon-5m"
    harness: str = "harnesses/horizon-5m.json"     # a harness file, or a run directory to continue from
    benchmark: str = "benchmarks/epl-2026-09.json"
    windows_dir: str = "benchmarks/windows"         # the question set, built once and versioned
    holdout: int = 2                               # instance groups (fixtures) the optimizer never sees
    seed: int = 0
    every: int = 5                                 # minutes between windows
    model: str | None = None                       # override the harness model
    reflection_model: str = "anthropic/claude-sonnet-4.5"
    cache_dir: str = ".cache"
    llm_cache: bool = True
    concurrency: int = 4
    max_metric_calls: int = 300                    # instance evaluations GEPA may spend
    minibatch: int = 8                             # instances per reflection step
    max_cost_ratio: float = 2.0                    # a candidate may cost at most this times the incumbent
    run_dir: str = "runs/latest"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
