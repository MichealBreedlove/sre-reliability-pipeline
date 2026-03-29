"""Tests for slo_gate.py — SLO safety gate."""
import pytest
from burn_rate import BurnRateCalculator, BurnRateResult, WINDOW_HOURS, BUDGET_PERIOD_HOURS
from slo_gate import SLOGate, GateDecision, GateResult, ACTION_RISK


@pytest.fixture
def gate() -> SLOGate:
    return SLOGate()


@pytest.fixture
def calc() -> BurnRateCalculator:
    return BurnRateCalculator()


def _critical_burn(calc: BurnRateCalculator, slo_id: str = "slo-1") -> BurnRateResult:
    """Return a BurnRateResult at exactly the critical 1h threshold (14.4x)."""
    consumed = 14.4 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    return calc.calculate(slo_id, "1h", consumed, 0.5)


def _warning_burn(calc: BurnRateCalculator, slo_id: str = "slo-1") -> BurnRateResult:
    """Return a BurnRateResult at 8x (warning range for 1h)."""
    consumed = 8.0 * (WINDOW_HOURS["1h"] / BUDGET_PERIOD_HOURS)
    return calc.calculate(slo_id, "1h", consumed, 0.9)


# ---------------------------------------------------------------------------
# Low-risk actions
# ---------------------------------------------------------------------------

def test_low_risk_always_allowed_healthy_budget(gate: SLOGate) -> None:
    result = gate.check("health_check", 1.0)
    assert result.allowed is True
    assert result.decision == GateDecision.ALLOW


def test_low_risk_allowed_even_with_exhausted_budget(gate: SLOGate) -> None:
    result = gate.check("status_check", 0.0)
    assert result.allowed is True


def test_low_risk_allowed_with_critical_burn(gate: SLOGate, calc: BurnRateCalculator) -> None:
    result = gate.check("metric_scrape", 0.5, [_critical_burn(calc)])
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Budget exhausted
# ---------------------------------------------------------------------------

def test_high_risk_blocked_budget_exhausted(gate: SLOGate) -> None:
    result = gate.check("vm_restart", 0.0)
    assert result.blocked is True
    assert "exhausted" in result.reason.lower()


def test_medium_risk_blocked_budget_exhausted(gate: SLOGate) -> None:
    result = gate.check("config_reload", 0.0)
    assert result.blocked is True


# ---------------------------------------------------------------------------
# Low budget
# ---------------------------------------------------------------------------

def test_high_risk_blocked_low_budget(gate: SLOGate) -> None:
    """High-risk blocked at 3% (< 5% threshold)."""
    result = gate.check("rolling_deploy", 0.03)
    assert result.blocked is True
    assert "critically low" in result.reason.lower()


def test_high_risk_allowed_healthy_budget(gate: SLOGate) -> None:
    result = gate.check("vm_restart", 0.50)
    assert result.allowed is True


def test_medium_risk_warned_degraded_budget(gate: SLOGate) -> None:
    """Medium-risk warned at 15% (< 20% threshold)."""
    result = gate.check("config_reload", 0.15)
    assert result.warned is True
    assert "degraded" in result.reason.lower()


def test_medium_risk_allowed_healthy_budget(gate: SLOGate) -> None:
    result = gate.check("scaling_up", 0.80)
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Burn-rate interaction
# ---------------------------------------------------------------------------

def test_high_risk_blocked_by_critical_burn_rate(
    gate: SLOGate, calc: BurnRateCalculator
) -> None:
    result = gate.check("service_restart", 0.50, [_critical_burn(calc)])
    assert result.blocked is True
    assert result.burn_rate is not None and result.burn_rate >= 14.4


def test_medium_risk_warned_by_critical_burn_rate(
    gate: SLOGate, calc: BurnRateCalculator
) -> None:
    result = gate.check("config_reload", 0.80, [_critical_burn(calc)])
    assert result.warned is True


def test_high_risk_allowed_with_only_warning_burn(
    gate: SLOGate, calc: BurnRateCalculator
) -> None:
    """Warning-level burn (8x) should NOT block high-risk actions on its own."""
    result = gate.check("vm_restart", 0.80, [_warning_burn(calc)])
    assert result.allowed is True


# ---------------------------------------------------------------------------
# Batch check
# ---------------------------------------------------------------------------

def test_batch_check_returns_all_actions(gate: SLOGate) -> None:
    actions = ["health_check", "vm_restart", "config_reload"]
    results = gate.check_batch(actions, 0.50)
    assert set(results.keys()) == set(actions)


def test_batch_check_mixed_decisions(gate: SLOGate, calc: BurnRateCalculator) -> None:
    """With exhausted budget: low=ALLOW, high=BLOCK, medium=BLOCK."""
    results = gate.check_batch(
        ["status_check", "vm_restart", "config_reload"], 0.0
    )
    assert results["status_check"].decision  == GateDecision.ALLOW
    assert results["vm_restart"].decision    == GateDecision.BLOCK
    assert results["config_reload"].decision == GateDecision.BLOCK


# ---------------------------------------------------------------------------
# Unknown action defaults to medium risk
# ---------------------------------------------------------------------------

def test_unknown_action_treated_as_medium_risk(gate: SLOGate) -> None:
    result = gate.check("some_unknown_action", 0.10)
    # 10% < 20% warn threshold → should warn
    assert result.warned is True
