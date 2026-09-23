"""The topic seam: a second topic plugs in with the same shape as the first,
and the first keeps behaving exactly as it did."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import datetime, timezone

from rsi_arena.cli import _settings, build_parser, main
from rsi_arena.harness import Harness
from rsi_arena.loop import Archive, Progress, Scoreboard, Settings, reflection_templates
from rsi_arena.topics import TOPICS, TopicSpec, load_topic, spec_of
from rsi_arena.topics.kalshi_horizon import KalshiHorizon, Window

BASE = "harnesses/horizon-5m.json"


def parse(*argv: str):
    return build_parser().parse_args(list(argv))


# -- the spec is the dataclass, for the first topic ----------------------------

def test_the_kalshi_spec_is_what_settings_always_defaulted_to():
    d, spec = Settings(), spec_of("kalshi-horizon-5m")
    for name in ("harness", "benchmark", "windows_dir", "runs_dir", "per_fixture",
                 "window_usd", "model_choices", "holdout", "audit", "max_metric_calls", "valset"):
        assert getattr(spec, name) == getattr(d, name), name
    assert spec.jev_harness == "harnesses/horizon-5m-jev.json"
    assert spec.max_day_usd == 100 and spec.unit == "cents"
    assert isinstance(TOPICS["kalshi-horizon-5m"], TopicSpec)


def test_unset_flags_are_filled_from_the_topic():
    """The parser says None; the spec answers; the run sees the same values it always did."""
    args = parse("optimize")
    assert args.harness is None and args.per_fixture is None and args.model_choices is None
    s = _settings(args)
    d = Settings()
    for name in TopicSpec.FILLS:
        assert getattr(s, name) == getattr(d, name), name
    s = _settings(parse("optimize", "--model-choices", "a/b, c/d", "--runs-dir", "elsewhere",
                        "--per-fixture", "4"))
    assert s.model_choices == ("a/b", "c/d") and s.runs_dir == "elsewhere" and s.per_fixture == 4


def test_the_topic_command_prints_the_spec(capsys):
    assert main(["topic", "--topic", "kalshi-horizon-5m", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["harness"] == Settings().harness and "factory" not in out
    assert out["model_choices"] == list(Settings().model_choices)

    assert main(["topic", "--shell"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("export HARNESS=")
    for key in ("BENCHMARK", "WINDOWS_DIR", "RUNS_DIR", "PER_FIXTURE", "WINDOW_USD", "HOLDOUT",
                "AUDIT", "MAX_METRIC_CALLS", "VALSET", "MODEL_CHOICES", "MAX_DAY_USD", "JEV_HARNESS"):
        assert f" {key}=" in line, key
    assert "typesafe/jev-1.13" in line


def test_load_topic_still_builds_the_task():
    task = load_topic(Settings())
    assert isinstance(task, KalshiHorizon) and task.name == "kalshi-horizon-5m"


# -- one archive and one scoreboard per topic, flat -------------------------------

def test_the_first_topic_keeps_its_file_names_and_the_next_gets_its_own():
    assert str(Archive.path_for("runs", "kalshi-horizon-5m")) == "runs/archive.json"
    assert str(Archive.path_for("runs")) == "runs/archive.json"
    assert str(Archive.path_for("runs", "crypto-spot-5m")) == "runs/archive.crypto-spot-5m.json"
    assert str(Scoreboard.path_for("runs", "kalshi-horizon-5m")) == "runs/scoreboard.json"
    assert str(Scoreboard.path_for("out", "us-equities-news-5m")) == "out/scoreboard.us-equities-news-5m.json"


# -- the duck-typed attributes the loop reads with getattr -------------------------

def test_kalshi_answers_the_optional_task_questions():
    task = KalshiHorizon(windows=[])
    w = Window(ticker="KXEPLGAME-X-Y", at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
               mid_now=0.40, realised=0.46, game={"game_id": "1", "clock": "10'"}, event="E")
    assert task.metric.unit == "cents" and task.metric.output_keys == ("delta_cents", "half_width_cents")
    assert task.moved(w) and not task.moved(Window("T", w.at, 0.40, 0.405, event="E"))
    # The same arithmetic `cmd_windows` always used, floating point and all.
    for mid, realised in ((0.40, 0.41), (0.50, 0.51), (0.30, 0.36), (0.5, 0.5001)):
        assert task.moved(Window("T", w.at, mid, realised, event="E")) is (abs(realised - mid) >= 0.01)
    assert task.label(w) == "KXEPLGAME-X-Y at 2026-09-01T12:00Z"
    assert task.context_of(w) == w.game
    d = w.to_dict()
    assert d["group"] == "E"
    assert task.instance_from_dict(d) == w
    task.use_instances([w])
    assert task.instances() == [w]
    assert "Measured on this task" in task.model_notes


def test_the_model_template_reads_its_notes_from_the_task():
    """The sentences about Opus and Jev on soccer moved out of the loop and
    back in through the task, and the prompt did not change by a byte."""
    task = KalshiHorizon(windows=[])
    text = reflection_templates(task, Harness.load(BASE), ("a/b",))["model"]
    assert ("Pick exactly one of: a/b. " + task.model_notes
            + " Reply with the model name alone within ``` blocks.") in text

    class Quiet:
        name, background, inputs = "q", "b", frozenset({"question"})

    plain = reflection_templates(Quiet(), Harness.load(BASE), ("a/b",))["model"]
    assert "Pick exactly one of: a/b. Reply with the model name" in plain
    assert "Measured on this task" not in plain


def test_windows_reports_moves_in_the_topics_unit(monkeypatch, capsys, t0):
    import rsi_arena.cli as cli

    ws = [Window(ticker="A", at=t0, mid_now=0.40, realised=0.46, event="E"),
          Window(ticker="A", at=t0.replace(hour=16), mid_now=0.40, realised=0.401, event="E"),
          Window(ticker="B", at=t0, mid_now=0.50, realised=0.50, event="F")]
    monkeypatch.setattr(cli, "load_topic", lambda s: KalshiHorizon(windows=ws))
    assert main(["windows", "--json", "--holdout", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unit"] == "cents"
    by = {g["group"]: g for g in out["groups"]}
    assert by["E"]["moved"] == 1 and by["E"]["move_p95"] == 6.0 and by["E"]["move_p50"] == 0.1
    assert by["F"]["move_p50"] == 0.0


# -- the tool cache lives with the harness now ------------------------------------

def test_the_tool_cache_is_the_same_object_wherever_it_is_imported_from():
    from rsi_arena.harness import NO_CACHE as a, ToolCache as A, cached
    from rsi_arena.kalshi.replay import NO_CACHE as b, ToolCache as B
    assert A is B and a is b
    assert cached.__module__ == "rsi_arena.harness.toolcache"


def test_the_heartbeat_writes_a_topic_only_where_there_is_a_column():
    """The reader's schema gains the column later; until then the old insert."""
    class Cur:
        def __init__(self, has_topic):
            self.has_topic, self.sql = has_topic, []

        def execute(self, sql, params=None):
            self.sql.append(" ".join(sql.split()))

        def fetchone(self):
            return (1,) if self.has_topic else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        closed = False

        def __init__(self, cur):
            self._cur = cur

        def cursor(self):
            return self._cur

        def commit(self):
            pass

        def close(self):
            pass

    for has_topic in (True, False):
        cur = Cur(has_topic)
        beat = Progress("gen1", topic="crypto-spot-5m", db_url="x")
        beat._conn = Conn(cur)
        beat.phase("baseline")
        insert = [s for s in cur.sql if s.startswith("insert")][0]
        assert ("topic" in insert) is has_topic
    assert Progress("gen1").topic == ""


def test_settings_fields_are_all_still_parsed():
    known = {f.name for f in fields(Settings)}
    assert "runs_dir" in known and hasattr(parse("optimize"), "runs_dir")
