"""Typed questions for a model that answers with distributions, not text.

TypeSafe's Jev (on OpenRouter as ``typesafe/jev-1.13``, through the alpha
Decisions API rather than chat completions) generates no prose. It takes a
``state`` - the same rendered text a chat model would be prompted with - and a
set of typed questions, and returns calibrated probabilities: a ``noul`` is
P(yes); a ``choice`` is a distribution over labels; a ``score`` is a
distribution over ordered levels plus its expectation. It answers in well under
a second and charges $0.042 a million input tokens, output free: about two
thousandths of a cent a window against three and a half cents for Opus 5.

For a five-minute price forecast that is the right shape. The harness has only
ever asked for a number and a width, and a distribution over cent moves *is* a
forecast and a width in one answer. What it cannot do is call tools or explain
itself, so a plan for it runs its tool steps as fixed steps - which the
incumbent plan already does - and ends in one prompt step that carries
``questions`` in place of an output schema.

Two things here are ours and not the API's. A ``score`` question may carry
``values``, one number per level, so the runner can turn a distribution over
"down 1 to 2 cents" into an expectation in cents; it is stripped before the
request. And ``answers`` maps the step's output fields onto the questions:

    "answers": {"delta_cents":      {"from": "move", "as": "mean"},
                "half_width_cents": {"from": "width", "as": "mean", "min": 1},
                "confidence":       {"from": "move", "as": "confidence"},
                "driver":           {"as": "const", "value": "expectation of the move distribution"}}

Both live in the plan, so the optimizer may rewrite the levels, the values
and the mapping like any other part of the plan.

The width is worth a question of its own, and the seeds ask one. ``half_range``
reads the move distribution's dispersion, which is a statement about how unsure
the model is and not about what market it would stand behind - and a
distribution piled on one level reads as zero, which the engine then widens to
the venue tick. Every seed did that, and the paper book's first finding was that
they were quoting a one-cent market on a four-cent path and being run over on
both sides. A ``score`` question over venue-appropriate widths, mapped with
``{"as": "mean", "min": <tick>}``, makes the width a decision the search can be
shown the cost of. ``half_range`` stays available; nothing asks for it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Model ids that answer through the Decisions API. OpenRouter lists the
#: pinned id and a floating alias.
DECISION_MODEL_PREFIXES: tuple[str, ...] = ("typesafe/", "~typesafe/")

QUESTION_TYPES = ("noul", "choice", "score")

#: What an ``answers`` entry may ask of a question's answer.
KINDS = ("mean", "stdev", "half_range", "confidence", "probability", "choice", "score", "const")


def is_decision_model(model: str | None) -> bool:
    return bool(model) and model.startswith(DECISION_MODEL_PREFIXES)


@dataclass
class Decision:
    """One Decisions API answer set, with what it cost."""

    answers: dict[str, Any]
    cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    model: str = ""


def validate_questions(questions: Any, answers: Any = None) -> None:
    """Refuse a step whose questions the API would refuse, before the call.

    Raises ``ValueError`` with a sentence; the spec wraps it as a HarnessError.
    """
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty object keyed by question name")
    for key, q in questions.items():
        if not isinstance(q, dict):
            raise ValueError(f"question {key!r} must be an object")
        kind = q.get("type")
        if kind not in QUESTION_TYPES:
            raise ValueError(f"question {key!r} has type {kind!r}; one of {', '.join(QUESTION_TYPES)}")
        if not isinstance(q.get("instructions"), str) or not q["instructions"].strip():
            raise ValueError(f"question {key!r} needs 'instructions'")
        criteria = q.get("criteria")
        if kind == "score":
            if not isinstance(criteria, list) or len(criteria) < 2:
                raise ValueError(f"score question {key!r} needs 'criteria': a list of at least two levels")
            values = q.get("values")
            if values is not None:
                if (not isinstance(values, list) or len(values) != len(criteria)
                        or not all(isinstance(v, (int, float)) for v in values)):
                    raise ValueError(f"score question {key!r} 'values' must be one number per level")
                if any(values[i] >= values[i + 1] for i in range(len(values) - 1)):
                    raise ValueError(f"score question {key!r} 'values' must increase with the levels")
        elif kind == "choice":
            if not isinstance(criteria, dict) or len(criteria) < 2:
                raise ValueError(f"choice question {key!r} needs 'criteria': an object of at least two labels")
        elif criteria is not None and not isinstance(criteria, dict):
            raise ValueError(f"noul question {key!r} 'criteria', if given, is an object with true/false")
    if answers is None:
        return
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object keyed by output field")
    for field_name, spec in answers.items():
        if not isinstance(spec, dict):
            raise ValueError(f"answers[{field_name!r}] must be an object")
        kind = spec.get("as")
        if kind not in KINDS:
            raise ValueError(f"answers[{field_name!r}] 'as' is {kind!r}; one of {', '.join(KINDS)}")
        if kind == "const":
            if "value" not in spec:
                raise ValueError(f"answers[{field_name!r}] const needs 'value'")
            continue
        src = spec.get("from")
        if src not in questions:
            raise ValueError(f"answers[{field_name!r}] reads question {src!r}, which is not asked")
        qtype = questions[src]["type"]
        needs = {"mean": "score", "stdev": "score", "half_range": "score", "score": "score",
                 "probability": "noul", "choice": "choice"}
        if kind in needs and qtype != needs[kind]:
            raise ValueError(f"answers[{field_name!r}] asks {kind!r} of a {qtype} question")
        if kind in ("mean", "stdev", "half_range") and questions[src].get("values") is None:
            raise ValueError(f"answers[{field_name!r}] asks {kind!r} of {src!r}, which has no 'values'")
        if kind == "confidence" and qtype == "noul":
            raise ValueError(f"answers[{field_name!r}]: a noul has no confidence; use 'probability'")


def wire_questions(questions: dict[str, Any]) -> dict[str, Any]:
    """The questions as the API takes them: our ``values`` stripped."""
    return {k: {kk: vv for kk, vv in q.items() if kk != "values"} for k, q in questions.items()}


def _distribution(answer: dict[str, Any], values: list[float]) -> list[tuple[float, float]]:
    """(value, probability) per level, in level order, probabilities renormalised."""
    probs = answer.get("probabilities") or {}
    ps = [float(probs.get(str(i), 0.0)) for i in range(len(values))]
    total = sum(ps)
    if total <= 0:
        # No distribution came back; fall back to the reported score's level.
        idx = int(round(float(answer.get("score", 0.0))))
        ps = [1.0 if i == min(max(idx, 0), len(values) - 1) else 0.0 for i in range(len(values))]
        total = 1.0
    return [(v, p / total) for v, p in zip(values, ps)]


def mean_of(answer: dict[str, Any], values: list[float]) -> float:
    return sum(v * p for v, p in _distribution(answer, values))


def stdev_of(answer: dict[str, Any], values: list[float]) -> float:
    dist = _distribution(answer, values)
    m = sum(v * p for v, p in dist)
    return math.sqrt(max(0.0, sum(p * (v - m) ** 2 for v, p in dist)))


def half_range_of(answer: dict[str, Any], values: list[float], coverage: float = 0.5) -> float:
    """Half the width of the tightest band around the mean holding ``coverage`` mass.

    Levels are taken nearest-the-mean first until the mass is reached; the
    band is the farthest level taken. A distribution piled on one level gives
    zero, which is what "I would quote a tight market" means.
    """
    dist = _distribution(answer, values)
    m = sum(v * p for v, p in dist)
    taken, reach = 0.0, 0.0
    for v, p in sorted(dist, key=lambda vp: abs(vp[0] - m)):
        taken += p
        reach = max(reach, abs(v - m))
        if taken >= coverage - 1e-9:
            break
    return reach


def answers_to_output(questions: dict[str, Any], answers: dict[str, Any],
                      mapping: dict[str, Any] | None) -> dict[str, Any]:
    """The step's output, built from the answers by the ``answers`` mapping.

    Without a mapping the output is the answers themselves. The raw answers
    ride along under ``decision`` either way, because the distribution is the
    part of the answer worth reading back.
    """
    if not mapping:
        return dict(answers)
    out: dict[str, Any] = {}
    for field_name, spec in mapping.items():
        kind = spec["as"]
        if kind == "const":
            out[field_name] = spec["value"]
            continue
        src = spec["from"]
        answer = answers.get(src)
        if not isinstance(answer, dict):
            raise ValueError(f"no answer to question {src!r} came back")
        values = [float(v) for v in questions[src].get("values") or []]
        if kind == "mean":
            value: Any = mean_of(answer, values)
        elif kind == "stdev":
            value = stdev_of(answer, values)
        elif kind == "half_range":
            value = half_range_of(answer, values, float(spec.get("coverage", 0.5)))
        elif kind == "confidence":
            value = float(answer.get("confidence", 0.0))
        elif kind == "probability":
            value = float(answer.get("noul", 0.0))
        elif kind == "choice":
            value = answer.get("choice")
        else:                                  # "score": the API's own expectation, in levels
            value = float(answer.get("score", 0.0))
        if isinstance(value, (int, float)):
            if "min" in spec:
                value = max(float(spec["min"]), value)
            if "max" in spec:
                value = min(float(spec["max"]), value)
            value = round(float(value), 4)
        out[field_name] = value
    out["decision"] = answers
    return out
