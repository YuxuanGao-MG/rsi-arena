import pytest

from rsi_arena.harness.template import ConditionError, evaluate, reads, render


def test_render_dotted_paths_and_json_for_structures():
    state = {"question": "T", "plan": {"queries": ["a", "b"]}, "n": 3}
    assert render("Q {{question}} {{ plan.queries }} {{n}}", state) == 'Q T ["a", "b"] 3'


def test_render_missing_name_is_a_key_error_naming_the_path():
    with pytest.raises(KeyError, match="plan.queries"):
        render("{{plan.queries}}", {"plan": {}})


def test_reads_reports_top_level_names():
    assert reads("{{a.b}} and {{c}} and {{a}}") == {"a", "c"}


def test_evaluate_conditions_over_state():
    state = {"notes": {"sufficient": True, "claims": [1, 2]}, "loop_iteration": 2}
    assert evaluate("notes.sufficient", state)
    assert evaluate("len(notes.claims) >= 2 and loop_iteration < 4", state)
    assert not evaluate("not notes.sufficient", state)
    assert evaluate("notes['sufficient'] == True", state)


def test_evaluate_refuses_code():
    with pytest.raises(ConditionError):
        evaluate("__import__('os').system('true')", {})
    with pytest.raises(ConditionError):
        evaluate("(lambda: 1)()", {})
    with pytest.raises(ConditionError, match="not in state"):
        evaluate("missing > 1", {})


def test_a_path_through_none_renders_empty_but_a_missing_name_still_raises():
    """A skipped step leaves its output_key as None, so a plan that branches on
    a decision reads the skipped branch's fields as nothing. A name nothing
    ever wrote is still a broken harness."""
    assert render("driver: {{opinion.text}}|", {"opinion": None}) == "driver: |"
    with pytest.raises(KeyError):
        render("{{opinion.text}}", {})
