"""Tests for slo_eval.py — SLO evaluation engine."""
import json
import pytest
import tempfile
from pathlib import Path

from slo_eval import (
    SLODefinition,
    SLIReading,
    SLOEvalResult,
    SLOEvaluator,
    WINDOWS,
    load_slo_catalog,
)
from conftest import CATALOG_PATH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_catalog(tmp_path: Path) -> Path:
    """Write a minimal 2-SLO catalog to a temp file."""
    catalog = {
        "slos": [
            {
                "id": "api-availability",
                "name": "API Availability",
                "service": "api",
                "sli_type": "availability",
                "target": 0.999,
                "windows": ["1h", "6h", "24h", "7d", "30d"],
            },
            {
                "id": "api-latency",
                "name": "API Latency p95",
                "service": "api",
                "sli_type": "latency",
                "target": 300.0,
                "threshold_ms": 300.0,
                "windows": ["1h", "6h", "24h", "7d", "30d"],
            },
        ]
    }
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(catalog))
    return p


@pytest.fixture
def evaluator(minimal_catalog: Path) -> SLOEvaluator:
    return SLOEvaluator(minimal_catalog)


@pytest.fixture
def real_evaluator() -> SLOEvaluator:
    """Evaluator backed by the real slo_catalog.json."""
    return SLOEvaluator(CATALOG_PATH)


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def test_load_catalog_from_real_file(real_evaluator: SLOEvaluator) -> None:
    """Real catalog should contain exactly 6 SLOs."""
    assert len(real_evaluator.slos) == 6


def test_load_slo_catalog_helper(minimal_catalog: Path) -> None:
    slos = load_slo_catalog(minimal_catalog)
    assert len(slos) == 2
    assert slos[0].id == "api-availability"


def test_slo_definition_from_dict() -> None:
    d = {
        "id": "test",
        "name": "Test",
        "service": "svc",
        "sli_type": "availability",
        "target": 0.99,
        "windows": ["1h"],
    }
    slo = SLODefinition.from_dict(d)
    assert slo.id == "test"
    assert slo.target == 0.99
    assert slo.threshold_ms is None


# ---------------------------------------------------------------------------
# Availability SLO evaluation
# ---------------------------------------------------------------------------

def test_evaluate_availability_pass(evaluator: SLOEvaluator) -> None:
    """3598/3600 = 99.94% > 99.9% target → pass."""
    readings = [SLIReading(service="api", window="1h", total_requests=3600, good_requests=3598)]
    results = evaluator.evaluate(readings, "1h")
    avail = next(r for r in results if r.slo_id == "api-availability")
    assert avail.passed is True
    assert avail.actual == pytest.approx(3598 / 3600, rel=1e-5)


def test_evaluate_availability_fail(evaluator: SLOEvaluator) -> None:
    """3590/3600 = 99.72% < 99.9% target → fail."""
    readings = [SLIReading(service="api", window="1h", total_requests=3600, good_requests=3590)]
    results = evaluator.evaluate(readings, "1h")
    avail = next(r for r in results if r.slo_id == "api-availability")
    assert avail.passed is False


def test_error_budget_consumed_when_failing(evaluator: SLOEvaluator) -> None:
    """Budget consumed > 0 when SLO fails."""
    readings = [SLIReading(service="api", window="1h", total_requests=3600, good_requests=3590)]
    results = evaluator.evaluate(readings, "1h")
    avail = next(r for r in results if r.slo_id == "api-availability")
    assert avail.error_budget_consumed > 0
    assert avail.error_budget_remaining < 1.0


def test_error_budget_zero_when_passing(evaluator: SLOEvaluator) -> None:
    """Budget consumed == 0 when SLO passes with margin."""
    readings = [SLIReading(service="api", window="1h", total_requests=3600, good_requests=3600)]
    results = evaluator.evaluate(readings, "1h")
    avail = next(r for r in results if r.slo_id == "api-availability")
    assert avail.error_budget_consumed == pytest.approx(0.0)
    assert avail.error_budget_remaining == pytest.approx(1.0)


def test_availability_zero_requests_counts_as_full(evaluator: SLOEvaluator) -> None:
    """Zero total_requests → availability treated as 1.0 (no data = no failure)."""
    readings = [SLIReading(service="api", window="1h", total_requests=0, good_requests=0)]
    results = evaluator.evaluate(readings, "1h")
    avail = next(r for r in results if r.slo_id == "api-availability")
    assert avail.passed is True


# ---------------------------------------------------------------------------
# Latency SLO evaluation
# ---------------------------------------------------------------------------

def test_evaluate_latency_pass(evaluator: SLOEvaluator) -> None:
    """p95 = 120ms < 300ms threshold → pass."""
    readings = [SLIReading(service="api", window="1h", p95_latency_ms=120.0)]
    results = evaluator.evaluate(readings, "1h")
    lat = next(r for r in results if r.slo_id == "api-latency")
    assert lat.passed is True
    assert lat.error_budget_consumed == pytest.approx(0.0)


def test_evaluate_latency_fail(evaluator: SLOEvaluator) -> None:
    """p95 = 450ms > 300ms threshold → fail."""
    readings = [SLIReading(service="api", window="1h", p95_latency_ms=450.0)]
    results = evaluator.evaluate(readings, "1h")
    lat = next(r for r in results if r.slo_id == "api-latency")
    assert lat.passed is False
    assert lat.error_budget_consumed > 0


def test_evaluate_latency_budget_proportional(evaluator: SLOEvaluator) -> None:
    """Latency 2x the threshold → budget_consumed capped at 1.0."""
    readings = [SLIReading(service="api", window="1h", p95_latency_ms=600.0)]
    results = evaluator.evaluate(readings, "1h")
    lat = next(r for r in results if r.slo_id == "api-latency")
    assert lat.error_budget_consumed == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Unknown window / missing data
# ---------------------------------------------------------------------------

def test_unknown_window_raises(evaluator: SLOEvaluator) -> None:
    with pytest.raises(ValueError, match="Unknown window"):
        evaluator.evaluate([], "99h")


def test_missing_sli_reading_skipped(evaluator: SLOEvaluator) -> None:
    """An SLO whose service has no reading is silently skipped."""
    results = evaluator.evaluate([], "1h")
    assert results == []


# ---------------------------------------------------------------------------
# Multi-window evaluation
# ---------------------------------------------------------------------------

def test_evaluate_all_windows(evaluator: SLOEvaluator) -> None:
    """evaluate_all_windows returns a result dict keyed by window name."""
    readings = SLIReading(service="api", window="X", total_requests=100, good_requests=99)
    by_window = {w: [readings] for w in WINDOWS}
    all_results = evaluator.evaluate_all_windows(by_window)
    assert set(all_results.keys()) == set(WINDOWS)
    for window, results in all_results.items():
        assert len(results) >= 1  # at least the availability SLO


def test_real_catalog_all_slos_have_windows(real_evaluator: SLOEvaluator) -> None:
    """Every SLO in the real catalog declares all 5 windows."""
    for slo in real_evaluator.slos:
        assert set(slo.windows) == set(WINDOWS), (
            f"SLO {slo.id!r} missing windows"
        )
