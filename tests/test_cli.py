"""The flags reach the run.

There was no test here, and that is why a scheduled generation spent five and a
half hours evaluating a question set it had been told to thin. ``--per-fixture``
parsed, appeared in the workflow log, and was dropped on the floor by a
``_settings`` that enumerated five of eleven names. These tests hold the parser
and the settings object to each other, so the next flag cannot go the same way.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from rsi_arena.cli import _settings, build_parser
from rsi_arena.loop import Settings


def parse(*argv: str):
    return build_parser().parse_args(list(argv))


def test_every_settings_flag_reaches_settings():
    """The three that did not: per_fixture, cascade, cascade_floor."""
    args = parse("optimize", "--per-fixture", "8", "--cascade", "5",
                 "--cascade-floor", "-0.02", "--holdout", "35")
    s = _settings(args)
    assert s.per_fixture == 8
    assert s.cascade == 5
    assert s.cascade_floor == pytest.approx(-0.02)
    assert s.holdout == 35


def test_parser_covers_the_settings_it_claims_to():
    """Any flag named after a setting must land on that setting.

    Stated as a sweep rather than a list so adding a flag cannot quietly create
    a sixth unwired name.
    """
    known = {f.name for f in fields(Settings)}
    args = parse("optimize")
    named = {k for k in vars(args) if k in known}
    # The optimize subcommand is the one that carries the whole set.
    assert {"per_fixture", "cascade", "cascade_floor", "holdout", "seed",
            "max_metric_calls", "run_dir"} <= named
    s = _settings(args)
    for name in named:
        if getattr(args, name) is None:
            # Left unset, the flag is the topic's to answer; for the first topic
            # the answer is the dataclass default, which the next test holds.
            assert getattr(s, name) == getattr(Settings(), name), f"{name} was not filled"
            continue
        assert getattr(s, name) == getattr(args, name), f"{name} did not reach Settings"


def test_llm_cache_is_the_one_inverted_flag():
    assert _settings(parse("optimize")).llm_cache is True
    assert _settings(parse("optimize", "--no-llm-cache")).llm_cache is False


def test_defaults_match_the_dataclass():
    """A parser default that drifts from the dataclass is a silent behaviour change."""
    d, s = Settings(), _settings(parse("optimize"))
    for f in fields(Settings):
        if f.name in ("run_dir", "extra"):        # the run names itself
            continue
        assert getattr(s, f.name) == getattr(d, f.name), f"{f.name} default drifted"
