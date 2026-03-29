"""End-to-end integration tests for the full SRE pipeline."""
import json
import pytest
from pathlib import Path

from slo_eval    import SLIReading, WINDOWS
from burn_rate   import BurnRateCalculator, WINDOW_HOURS, BUDGET_PERIOD_HOURS
from slo_gate    import SLOGate, GateDecision
from slo_runner  import SLORunner, SLORunnerResult
from incident_manager import IncidentManager, IncidentStatus, Severity
from incident_render  import render_postmortem, render_summary_report

from conftest import CATALOG_PATH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _healthy_readings(window: str = "1h") -> list[SLIReading]:
    """SLI readings that satisfy every SLO in the real catalog."""
    return [
        SLIReading("proxmox-api",  window, total_requests=3600, good_requests=3598),
        SLIReading("grafana",      window, p95_latency_ms=120.0),
        SLIReading("prometheus",   window, total_requests=1000, good_requests=999),
        SLIReading("nfs",          window, total_requests=1000, good_requests=999),
        SLIReading("node-exporter",window, error_rate=0.005),
        SLIReading("vm-lifecycle", window, total_requests=50, good_requests=50),
    ]


def _degraded_readings(window: str = "1h") -> list[SLIReading]:
    """SLI readings that violate the Proxmox API availability SLO hard."""
    return [
        SLIReading("proxmox-api",  window, total_requests=3600, good_requests=3480),  # ~96.7%
        SLIReading("grafana",      window, p95_latency_ms=120.0),
        SLIReading("prometheus",   window, total_requests=1000, good_requests=999),
        SLIReading("nfs",          window, total_requests=1000, good_requests=999),
        SLIReading("node-exporter",window, error_rate=0.005),
        SLIReading("vm-lifecycle", window, total_requests=50, good_requests=50),
    ]


@pytest.fixture
def runner(tmp_path: Path) -> SLORunner:
    store = tmp_path / "incidents.json"
    return SLORunner(catalog_path=CATALOG_PATH, incident_store=store)


# ---------------------------------------------------------------------------
# Full pipeline — healthy system
# ---------------------------------------------------------------------------

def test_full_pipeline_healthy_all_slos_pass(runner: SLORunner) -> None:
    """All healthy readings → all SLOs pass, no alerts, no incidents."""
    sli_by_window = {w: _healthy_readings(w) for w in WINDOWS}
    result = runner.run(sli_by_window)

    assert result.failing_slos == 0
    assert result.passing_slos == result.total_slos_evaluated
    assert result.alerts == []
    assert result.new_incidents == []


def test_full_pipeline_healthy_no_gate_blocks(runner: SLORunner) -> None:
    """With healthy SLOs, high-risk actions pass through the gate."""
    sli_by_window = {w: _healthy_readings(w) for w in WINDOWS}
    result = runner.run(
        sli_by_window,
        pending_actions=["vm_restart", "rolling_deploy"],
    )
    for action, gate_result in result.gate_checks.items():
        assert gate_result.decision == GateDecision.ALLOW, (
            f"{action} was unexpectedly {gate_result.decision.value}: {gate_result.reason}"
        )


# ---------------------------------------------------------------------------
# Full pipeline — degraded system
# ---------------------------------------------------------------------------

def test_full_pipeline_degraded_slo_fails(runner: SLORunner) -> None:
    """Degraded Proxmox API (96.7% avail) → that SLO fails in all windows."""
    sli_by_window = {w: _degraded_readings(w) for w in WINDOWS}
    result = runner.run(sli_by_window)

    proxmox_results = [
        r
        for window_results in result.eval_results.values()
        for r in window_results
        if r.slo_id == "proxmox-api-availability"
    ]
    assert any(not r.passed for r in proxmox_results), (
        "Expected proxmox-api-availability to fail on degraded readings"
    )


def test_full_pipeline_degraded_opens_incident(runner: SLORunner) -> None:
    """Critical burn rate on degraded readings → at least one incident opened."""
    sli_by_window = {w: _degraded_readings(w) for w in WINDOWS}
    result = runner.run(sli_by_window)
    assert len(result.new_incidents) >= 1
    assert result.new_incidents[0].slo_id == "proxmox-api-availability"


def test_full_pipeline_degraded_blocks_high_risk(runner: SLORunner) -> None:
    """Critical SLO breach → high-risk actions blocked by safety gate."""
    sli_by_window = {w: _degraded_readings(w) for w in WINDOWS}
    result = runner.run(
        sli_by_window,
        pending_actions=["vm_restart"],
    )
    assert result.gate_checks["vm_restart"].blocked is True


def test_full_pipeline_no_duplicate_incidents(runner: SLORunner) -> None:
    """Running the pipeline twice doesn't open duplicate incidents."""
    sli_by_window = {w: _degraded_readings(w) for w in WINDOWS}
    result1 = runner.run(sli_by_window)
    result2 = runner.run(sli_by_window)
    # Second run should find the already-open incident and skip
    assert len(result2.new_incidents) == 0


# ---------------------------------------------------------------------------
# Postmortem rendering
# ---------------------------------------------------------------------------

def test_postmortem_render_contains_required_sections(tmp_path: Path) -> None:
    mgr = IncidentManager()
    inc = mgr.open_incident(
        slo_id="proxmox-api-availability",
        service="proxmox-api",
        title="Proxmox API degraded — 3.2% error rate",
        description="Node Orin returned 503 on API requests during NFS contention.",
        burn_rate=14.4,
        budget_remaining=0.02,
    )
    mgr.resolve_incident(
        inc.id,
        root_cause="NFS contention on TrueNAS caused Proxmox API workers to queue.",
        action_items=["Tune NFS timeout", "Add NFS I/O alerting", "Capacity review"],
    )
    md = render_postmortem(inc)

    assert "# Postmortem:" in md
    assert "## Timeline"    in md
    assert "## Root Cause"  in md
    assert "## Action Items" in md
    assert "NFS contention" in md
    assert "14.40" in md  # burn rate


def test_summary_report_shows_avg_ttr(tmp_path: Path) -> None:
    from datetime import datetime, timezone, timedelta

    mgr = IncidentManager()
    incidents = []
    for minutes in [10.0, 16.0]:
        inc = mgr.open_incident("slo", "svc", "Test")
        opened = datetime.fromisoformat(inc.opened_at)
        inc.resolved_at = (opened + timedelta(minutes=minutes)).isoformat()
        inc.status = IncidentStatus.RESOLVED
        incidents.append(inc)

    md = render_summary_report(incidents)
    assert "13 min" in md  # avg of 10 and 16 = 13


def test_runner_summary_string(runner: SLORunner) -> None:
    sli_by_window = {w: _healthy_readings(w) for w in WINDOWS}
    result = runner.run(sli_by_window)
    summary = result.summary()
    assert "SLOs evaluated" in summary
    assert "passing" in summary
