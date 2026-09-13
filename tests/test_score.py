from datetime import datetime, timezone

from rsi_arena.bench import Window, pooled, quote_from, score_window, window_value


def window(mid_now: float, realised: float) -> Window:
    return Window(ticker="A", at=datetime(2026, 8, 23, tzinfo=timezone.utc), mid_now=mid_now,
                  realised=realised)


def test_quote_from_anchors_on_the_exchange_mid_and_clamps():
    assert quote_from(0.50, 3, 2) == (0.53, 0.51, 0.55)
    assert quote_from(0.99, 50, 0)[0] == 0.99
    assert quote_from(0.02, -50, 0)[0] == 0.01


def test_skill_is_fraction_of_no_change_error_removed():
    s = score_window({"delta_cents": 5, "half_width_cents": 1}, window(0.50, 0.60))
    assert round(s.skill, 3) == 0.5 and not s.echoed and not s.covered
    assert round(window_value(s), 3) == 0.75
    worse = score_window({"delta_cents": -5, "half_width_cents": 0}, window(0.50, 0.60))
    assert worse.skill == -0.5 and window_value(worse) == 0.25
    awful = score_window({"delta_cents": -30, "half_width_cents": 0}, window(0.50, 0.60))
    assert awful.skill < -1 and window_value(awful) == 0.0


def test_echo_and_unmeasurable_are_flagged():
    echo = score_window({"delta_cents": 0}, window(0.50, 0.55))
    assert echo.echoed and echo.skill == 0.0 and window_value(echo) == 0.5
    flat = score_window({"delta_cents": 2}, window(0.50, 0.50))
    assert flat.unmeasurable and window_value(flat) == 0.5


def test_unusable_output_scores_nothing():
    assert score_window("text", window(0.5, 0.6)) is None
    assert score_window({"delta_cents": "5"}, window(0.5, 0.6)) is None
    assert score_window({"delta_cents": True}, window(0.5, 0.6)) is None
    assert window_value(None) == 0.0


def test_pooled_sums_errors_before_dividing():
    a = score_window({"delta_cents": 10}, window(0.50, 0.60))     # perfect on a big move
    b = score_window({"delta_cents": 1}, window(0.50, 0.501))     # bad on a tiny one
    p = pooled([a, b])
    assert p["n"] == 2 and p["moved"] == 1
    assert p["skill"] > 0.9                                       # the big window dominates
