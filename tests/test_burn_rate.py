"""Tests for burn_rate.py — multi-window burn-rate calculation."""
import pytest
from burn_rate import (
    BurnRateCalculator,
    BurnRateResult,
    BurnRateAlert,
    BURN_RATE_THRESHOLDS,
    BUDGET_PERIOD_HOURS,
    WINDOW_HOURS,
)


@pytest.fixture
def calc() -> BurnRateCalculator:
    return BurnRateCalculator()


# ---------------------------------------------------------------------------
# Basic burn-rate math
# ---------------------------------------------------------------------------

def test_burn_rate_at_exactly_one(calc: BurnRateCalculator) -> None:
    """Consuming exactly the expected fraction yields burn_rate == 1.0."""
    window = "1h"
    expected = WINDOW_HOURS[window] / BUDGET_PERIOD_HOURS  # 1/720
    result = calc.calculate("slo-1", window, expected, 0.999)
    assert result.burn_rate == pytest.approx(1.0, rel=1e-4)


def test_burn_rate_zero_when_no_consumption(calc: BurnRateCalculator) -> None:
    result = calc.calculate("slo-1", "1h", 0.0, 1.0)
    assert result.burn_rate == pytest.approx(0.0)
    assert result.alert_fired is False
    assert result.alert_severity is None


def test_burn_rate_critical_1h(calc: BurnRateCalculator) -> None:
    """error_budget_consumed = 14.4 * (1/720) triggers critical alert in 1h window."""
    consumed = 14.4 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "1h", consumed, 0.98)
    assert result.burn_rate == pytest.approx(14.4, rel=1e-3)
    assert result.alert_severity == "critical"
    assert result.alert_fired is True


def test_burn_rate_warning_1h(calc: BurnRateCalculator) -> None:
    """Burn rate of 8x (between warning=6 and critical=14.4) → warning."""
    consumed = 8.0 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "1h", consumed, 0.99)
    assert result.alert_severity == "warning"


def test_burn_rate_critical_6h(calc: BurnRateCalculator) -> None:
    """6x burn rate in 6h window triggers critical."""
    consumed = 6.0 * (WINDOW_HOURS["6h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "6h", consumed, 0.95)
    assert result.burn_rate == pytest.approx(6.0, rel=1e-3)
    assert result.alert_severity == "critical"


def test_burn_rate_warning_24h(calc: BurnRateCalculator) -> None:
    """1.6x burn rate in 24h window (between 1.5 and 3.0) → warning."""
    consumed = 1.6 * (WINDOW_HOURS["24h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "24h", consumed, 0.90)
    assert result.alert_severity == "warning"


def test_burn_rate_below_threshold_no_alert(calc: BurnRateCalculator) -> None:
    """0.4x burn rate in 7d window (below 0.5 warning) → no alert."""
    consumed = 0.4 * (WINDOW_HOURS["7d"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "7d", consumed, 0.99)
    assert result.alert_fired is False


# ---------------------------------------------------------------------------
# Time-to-exhaustion
# ---------------------------------------------------------------------------

def test_time_to_exhaustion_at_14x_burn(calc: BurnRateCalculator) -> None:
    """At 14.4x burn with 100% budget remaining, TTE ≈ 720/14.4 ≈ 50h."""
    consumed = 14.4 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "1h", consumed, 1.0)
    assert result.time_to_exhaustion_hours is not None
    assert result.time_to_exhaustion_hours == pytest.approx(
        BUDGET_PERIOD_HOURS / 14.4, rel=0.01
    )


def test_time_to_exhaustion_zero_when_budget_gone(calc: BurnRateCalculator) -> None:
    consumed = 14.4 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "1h", consumed, 0.0)
    assert result.time_to_exhaustion_hours == pytest.approx(0.0)


def test_time_to_exhaustion_none_when_no_burn(calc: BurnRateCalculator) -> None:
    result = calc.calculate("slo-1", "1h", 0.0, 0.95)
    assert result.time_to_exhaustion_hours is None


# ---------------------------------------------------------------------------
# Unknown window
# ---------------------------------------------------------------------------

def test_unknown_window_raises(calc: BurnRateCalculator) -> None:
    with pytest.raises(ValueError, match="Unknown window"):
        calc.calculate("slo-1", "99d", 0.01, 0.99)


# ---------------------------------------------------------------------------
# Multi-window calculation
# ---------------------------------------------------------------------------

def test_calculate_multi_window_returns_all_windows(calc: BurnRateCalculator) -> None:
    consumptions = {w: 0.001 for w in ["1h", "6h", "24h"]}
    results = calc.calculate_multi_window("slo-1", consumptions, 0.99)
    assert len(results) == 3
    windows = {r.window for r in results}
    assert windows == {"1h", "6h", "24h"}


# ---------------------------------------------------------------------------
# Alert generation
# ---------------------------------------------------------------------------

def test_generate_alerts_only_for_firing(calc: BurnRateCalculator) -> None:
    results = [
        calc.calculate("slo-1", "1h",  0.0, 1.0),  # no alert
        calc.calculate("slo-1", "6h",  6.0 * (WINDOW_HOURS["6h"] / BUDGET_PERIOD_HOURS), 0.5),
    ]
    alerts = calc.generate_alerts(results, service="proxmox-api")
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert alerts[0].service  == "proxmox-api"


def test_alert_message_contains_burn_rate(calc: BurnRateCalculator) -> None:
    consumed = 14.4 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "1h", consumed, 0.9)
    alerts = calc.generate_alerts([result])
    assert "14.4" in alerts[0].message


def test_recommend_action_critical(calc: BurnRateCalculator) -> None:
    consumed = 14.4 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "1h", consumed, 0.9)
    alerts = calc.generate_alerts([result])
    assert "Immediate investigation" in alerts[0].recommended_action


def test_recommend_action_warning(calc: BurnRateCalculator) -> None:
    consumed = 8.0 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    result = calc.calculate("slo-1", "1h", consumed, 0.95)
    alerts = calc.generate_alerts([result])
    assert "Monitor closely" in alerts[0].recommended_action
