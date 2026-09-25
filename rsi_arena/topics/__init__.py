"""Topics: each one a :class:`~rsi_arena.loop.Task`, and what a run of it defaults to.

Adding one is a package and a :class:`TopicSpec` here. The spec is the seam:
everything the CLI, the workflow and the scripts used to know about Kalshi by
name — which harness file, which benchmark, what a window costs, which models
the search may pick — is a field on it, and ``rsi-arena topic`` prints it so a
shell can read the same answers.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, fields
from typing import Any, Callable

from ..loop import Settings, Task
from .crypto_horizon import CryptoHorizon
from .kalshi_horizon import KalshiHorizon
from .news_equity import NewsEquity


@dataclass(frozen=True)
class TopicSpec:
    """Where a topic's files are and what a run of it costs, before any flag."""

    name: str
    factory: Callable[[Settings], Task]
    harness: str                      # the seed harness
    jev_harness: str                  # the seed for a decisions model, if the topic has one
    benchmark: str
    windows_dir: str                  # the question set, built once and versioned
    runs_dir: str
    per_fixture: int                  # instances kept per group; 0 keeps all
    window_usd: float                 # dollars an instance before anything is measured
    model_choices: tuple
    holdout: int
    audit: int
    max_metric_calls: int
    valset: int
    max_day_usd: float                # what a day of the loop on this topic may spend
    unit: str = "cents"               # the metric's unit, for readers that never load the task
    #: The least an incumbent is priced at when the gate's cost ratio is applied
    #: and when the judgment is reserved. A Jev incumbent costs two thousandths
    #: of a cent a window; at a penny the reserve for 1,200 windows was $24
    #: against a $5 ceiling and the first crypto generation stopped before it
    #: searched. At a fifth of a cent a candidate may spend up to $0.004 a
    #: window - one Opus call on roughly every eighth window, which is what a
    #: gated plan does - and the reserve is under five dollars.
    cost_floor_usd: float = 0.01
    #: Generations a UTC day may hold on this topic; the workflow's crons offer
    #: this many slots and the guard refuses a further one.
    runs_per_day: int = 3

    #: The settings a flag left unset takes from the spec. Everything else on
    #: ``Settings`` keeps the dataclass default whatever the topic.
    FILLS = ("harness", "benchmark", "windows_dir", "runs_dir", "per_fixture", "window_usd",
             "model_choices", "cost_floor_usd", "holdout", "audit", "max_metric_calls", "valset")

    def fill(self, settings: Settings) -> Settings:
        """Every ``None`` on ``settings`` that this spec has an answer for, answered."""
        for name in self.FILLS:
            if getattr(settings, name, None) is None:
                setattr(settings, name, getattr(self, name))
        return settings

    def to_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "factory"}
        out["model_choices"] = list(self.model_choices)
        return out

    def shell(self) -> str:
        """One ``export`` line a workflow can ``eval``."""
        pairs = [("HARNESS", self.harness), ("BENCHMARK", self.benchmark),
                 ("WINDOWS_DIR", self.windows_dir), ("RUNS_DIR", self.runs_dir),
                 ("PER_FIXTURE", self.per_fixture), ("WINDOW_USD", self.window_usd),
                 ("HOLDOUT", self.holdout), ("AUDIT", self.audit),
                 ("MAX_METRIC_CALLS", self.max_metric_calls), ("VALSET", self.valset),
                 ("MODEL_CHOICES", ",".join(self.model_choices)),
                 ("MAX_DAY_USD", self.max_day_usd), ("JEV_HARNESS", self.jev_harness),
                 ("COST_FLOOR_USD", self.cost_floor_usd), ("RUNS_PER_DAY", self.runs_per_day)]
        return "export " + " ".join(f"{k}={shlex.quote(str(v))}" for k, v in pairs)


_defaults = Settings()

TOPICS: dict[str, TopicSpec] = {
    KalshiHorizon.name: TopicSpec(
        name=KalshiHorizon.name,
        factory=KalshiHorizon.from_settings,
        # Read off the settings dataclass rather than restated, so the first
        # topic's spec is by construction what a bare ``Settings()`` has always
        # been.
        harness=_defaults.harness,
        jev_harness="harnesses/horizon-5m-jev.json",
        benchmark=_defaults.benchmark,
        windows_dir=_defaults.windows_dir,
        runs_dir=_defaults.runs_dir,
        per_fixture=_defaults.per_fixture,
        window_usd=_defaults.window_usd,
        model_choices=_defaults.model_choices,
        holdout=_defaults.holdout,
        audit=_defaults.audit,
        max_metric_calls=_defaults.max_metric_calls,
        valset=_defaults.valset,
        # Two generations a day at the measured cold price, with room.
        max_day_usd=100.0,
        unit=KalshiHorizon.metric.unit,
        cost_floor_usd=_defaults.cost_floor_usd,
        runs_per_day=3,
    ),
    NewsEquity.name: TopicSpec(
        name=NewsEquity.name,
        factory=NewsEquity.from_settings,
        # The seed is the decisions-model harness: at two thousandths of a
        # cent a window the whole question set is judged every generation,
        # which is the resolution the Kalshi topic never could buy. The Opus
        # file is the chat-model seed for a model comparison. ``TopicSpec``
        # has one seed field, so the Jev file is it.
        harness="harnesses/news-equity-5m-jev.json",
        jev_harness="harnesses/news-equity-5m-jev.json",
        benchmark="benchmarks/news-2026-09.json",
        windows_dir="benchmarks/windows-news",
        runs_dir="runs/news-equity-5m",
        per_fixture=0,
        window_usd=0.00005,
        model_choices=("typesafe/jev-1.13", "openai/gpt-5-mini"),
        # 2,246 symbol-days on the first question set: a held-out set of 300
        # resolves a gap about a third the size that the Kalshi hundred does.
        holdout=300,
        audit=100,
        # Calls are not the bound on a topic whose windows cost a fraction of a
        # cent: one accepted candidate's full valset pass is 600, and 1200 let
        # the search propose exactly once. The dollar stopper bounds the
        # rewriter's bill; the calls just have to be out of its way.
        max_metric_calls=4800,
        valset=400,
        # The cap is checked against the key's spend for the whole UTC day, and
        # the key is shared by every topic and by anyone benching locally: at
        # fifteen the first news generation found the day already two-thirds
        # spent by the crypto generation before it. Sixty leaves the three
        # topics' expected spend (about seventy, eleven and eleven) under the
        # key's own hundred.
        # One cap for every topic, because it is checked against the shared
        # key's spend for the whole UTC day.
        max_day_usd=100.0,
        unit=NewsEquity.metric.unit,
        cost_floor_usd=0.002,
        runs_per_day=3,
    ),
    CryptoHorizon.name: TopicSpec(
        name=CryptoHorizon.name,
        factory=CryptoHorizon.from_settings,
        # The seed the loop starts from is the decisions-model harness: the
        # spec has one slot for the seed and one for the Jev seed, and on this
        # topic they are the same file. The chat-model seed beside it
        # (harnesses/crypto-horizon-1m.json) is there to be named with
        # ``--harness`` when a run wants Opus in the driving seat.
        harness="harnesses/crypto-horizon-1m-jev.json",
        jev_harness="harnesses/crypto-horizon-1m-jev.json",
        benchmark="benchmarks/crypto-2026-09.json",
        windows_dir="benchmarks/windows-crypto",
        runs_dir="runs/crypto-horizon-1m",
        # Twenty-four instants a day across three coins: power comes from days,
        # but not only from days. Measured on the held-out rollouts of gen4,
        # gen5 and gen7 by thinning them after the fact: at 24 windows a day the
        # paired standard error per day is 0.066-0.098 pooled skill, at 12 it is
        # 0.081-0.127 and at 8 it is 0.115-0.151. A football match's windows
        # really are one observation seen thirty-four times; a UTC day's minute
        # horizons are not, so the cheapest way to halve the interval is not to
        # thin them. Twenty-four a day costs half a cent a day to score.
        per_fixture=24,
        # A Jev window, measured rather than assumed: gen1-gen7 paid between
        # $0.00013 and $0.00037 an instance over fourteen half-generations,
        # averaging about $0.00024. The spec said 0.00005 for seven generations,
        # which under-priced the search share of the prediction by four times.
        window_usd=0.0002,
        model_choices=("typesafe/jev-1.13", "openai/gpt-5-mini"),
        # A third of the 365-day set held out, a sixth held back to confirm, and
        # about half left to search on (120 + 60 + 185). `scripts/power.py` on
        # the paired held-out rollouts of gen1-gen7 puts what thirty days can
        # resolve at 0.041-0.070 pooled skill (the gate recorded 0.031 for gen5
        # itself), and gen5 was promoted on +0.025 - inside the margin, by a test
        # that could not see it. 120 days resolve 0.020-0.035, exactly twice as
        # fine, which makes a gain the size of gen5's the smallest thing the gate
        # can now honestly accept. Crypto bars are free and unlimited, so the
        # only thing that ever bounded this was how many days had been fetched.
        holdout=120,
        audit=60,
        # Calls are not the bound on a topic whose windows cost a fraction of a
        # cent, but they do bound how often the search may iterate: GEPA scores
        # the seed over the whole valset before proposing anything, and every
        # candidate it keeps costs another full pass. At valset 1200 and 12,000
        # calls that is one seed pass, eight kept candidates and 150 proposals;
        # at the old 4,800 it would have been the seed and three candidates with
        # nothing left to propose with. The dollar stopper bounds the bill.
        max_metric_calls=12000,
        # Fifty days of the 185 in train, up from twenty-five. The valset is how
        # the search ranks its own candidates, and at 600 windows that ranking
        # carried a standard error of about 0.016 pooled skill - larger than the
        # effects it was choosing between, so it was picking winners by luck.
        # 1200 windows is 27% of train and takes that to about 0.011.
        valset=1200,
        # The cap is checked against the key's spend for the whole UTC day, and
        # the key is shared by every topic and by anyone benching locally: at
        # fifteen the first news generation found the day already two-thirds
        # spent by the crypto generation before it.
        #
        # Seventy, not the key's own hundred, because the deeper held-out set
        # roughly doubled a generation. Priced at $0.0002 a window: the baseline
        # is the incumbent over probe (20 days x 24) plus held-out (120 x 24) =
        # 3,360 windows, $0.67; the search is 12,000 calls plus a 1,200 valset
        # pass, $2.64; the judgment reserves the same 3,360 windows at the most
        # the gate lets a candidate cost (2.0 x the $0.002 floor), $13.44; and
        # the rewriter is 75 Sonnet reflections at $0.08, $6.00. About $23 cold,
        # against $11 at thirty held-out days, and the workflow's 1.1 margin
        # makes it $25. Three slots is $75 - the whole shared day - so the guard
        # refuses a start once the day is at seventy, which leaves thirty for
        # Kalshi ($17) and news ($10).
        max_day_usd=70.0,
        unit=CryptoHorizon.metric.unit,
        cost_floor_usd=0.002,
        runs_per_day=3,
    ),
}


def spec_of(topic: str) -> TopicSpec:
    try:
        return TOPICS[topic]
    except KeyError:
        raise KeyError(f"unknown topic {topic!r} (have: {', '.join(TOPICS)})") from None


def load_topic(settings: Settings) -> Task:
    return spec_of(settings.topic).factory(settings)


__all__ = ["TOPICS", "TopicSpec", "load_topic", "spec_of", "KalshiHorizon", "NewsEquity", "CryptoHorizon"]
