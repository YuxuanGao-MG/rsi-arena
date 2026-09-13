from rsi_arena.topics.kalshi_horizon import WindowScore, pooled, quote_from, score_output


def test_quote_from_anchors_on_the_exchange_mid_and_clamps():
    assert quote_from(0.50, 3, 2) == (0.53, 0.51, 0.55)
    assert quote_from(0.99, 50, 0)[0] == 0.99
    assert quote_from(0.02, -50, 0)[0] == 0.01


def test_skill_is_the_fraction_of_no_change_error_removed():
    s = score_output({"delta_cents": 5, "half_width_cents": 1}, 0.50, 0.60)
    assert round(s.skill, 3) == 0.5 and not s.echoed and not s.covered and round(s.value, 3) == 0.75
    worse = score_output({"delta_cents": -5}, 0.50, 0.60)
    assert worse.skill == -0.5 and worse.value == 0.25
    assert score_output({"delta_cents": -30}, 0.50, 0.60).value == 0.0


def test_echo_silence_and_unmeasurable():
    echo = score_output({"delta_cents": 0}, 0.50, 0.55)
    assert echo.echoed and echo.skill == 0.0 and echo.value == 0.5
    assert WindowScore.silent(0.50, 0.55).to_dict() == echo.to_dict()
    flat = score_output({"delta_cents": 2}, 0.50, 0.50)
    assert flat.unmeasurable and flat.value == 0.5


def test_unusable_output_scores_nothing():
    assert score_output("text", 0.5, 0.6) is None
    assert score_output({"delta_cents": "5"}, 0.5, 0.6) is None
    assert score_output({"delta_cents": True}, 0.5, 0.6) is None


def test_pooled_sums_errors_before_dividing():
    a = score_output({"delta_cents": 10}, 0.50, 0.60)      # perfect on a big move
    b = score_output({"delta_cents": 1}, 0.50, 0.501)      # bad on a tiny one
    p = pooled([a, b])
    assert p["moved"] == 1 and p["skill"] > 0.9
