"""Tests for incident_manager.py — incident lifecycle."""
import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from incident_manager import (
    Incident,
    IncidentManager,
    IncidentStatus,
    Severity,
    TimelineEvent,
    severity_from_burn_rate,
    severity_from_budget,
    ESCALATION_THRESHOLDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mgr() -> IncidentManager:
    return IncidentManager()


@pytest.fixture
def open_incident(mgr: IncidentManager) -> Incident:
    return mgr.open_incident(
        slo_id="proxmox-api-availability",
        service="proxmox-api",
        title="Test incident",
        description="Created by test",
        burn_rate=15.0,
        budget_remaining=0.02,
    )


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

def test_severity_from_burn_rate_p1() -> None:
    assert severity_from_burn_rate(14.4) == Severity.P1
    assert severity_from_burn_rate(20.0) == Severity.P1


def test_severity_from_burn_rate_p2() -> None:
    assert severity_from_burn_rate(6.0) == Severity.P2
    assert severity_from_burn_rate(10.0) == Severity.P2


def test_severity_from_burn_rate_p3() -> None:
    assert severity_from_burn_rate(3.0) == Severity.P3
    assert severity_from_burn_rate(4.5) == Severity.P3


def test_severity_from_burn_rate_p4() -> None:
    assert severity_from_burn_rate(0.5) == Severity.P4
    assert severity_from_burn_rate(2.9) == Severity.P4


def test_severity_from_budget_p1() -> None:
    assert severity_from_budget(0.0) == Severity.P1
    assert severity_from_budget(-0.1) == Severity.P1


def test_severity_from_budget_p2() -> None:
    assert severity_from_budget(0.03) == Severity.P2


def test_severity_from_budget_p3() -> None:
    assert severity_from_budget(0.10) == Severity.P3


def test_severity_from_budget_p4() -> None:
    assert severity_from_budget(0.50) == Severity.P4


# ---------------------------------------------------------------------------
# Opening incidents
# ---------------------------------------------------------------------------

def test_open_incident_sets_all_fields(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    assert open_incident.id is not None
    assert open_incident.slo_id  == "proxmox-api-availability"
    assert open_incident.service == "proxmox-api"
    assert open_incident.title   == "Test incident"
    assert open_incident.status  == IncidentStatus.OPEN
    assert open_incident.is_open is True


def test_open_incident_auto_severity_from_burn_rate(mgr: IncidentManager) -> None:
    incident = mgr.open_incident("slo", "svc", "hi", burn_rate=15.0)
    assert incident.severity == Severity.P1


def test_open_incident_explicit_severity(mgr: IncidentManager) -> None:
    incident = mgr.open_incident("slo", "svc", "hi", severity=Severity.P3)
    assert incident.severity == Severity.P3


def test_open_incident_creates_timeline_entry(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    assert len(open_incident.timeline) >= 1
    assert open_incident.timeline[0].event_type == "opened"


def test_open_incident_stored_in_manager(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    assert open_incident.id in mgr.incidents


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------

def test_update_status_to_investigating(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    updated = mgr.update_status(open_incident.id, IncidentStatus.INVESTIGATING)
    assert updated.status == IncidentStatus.INVESTIGATING
    assert updated.is_open is True


def test_resolve_incident(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    resolved = mgr.resolve_incident(
        open_incident.id,
        root_cause="Memory pressure on Orin node caused API timeout cascade.",
        action_items=["Tune memory limits", "Add memory alerting"],
    )
    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.resolved_at is not None
    assert resolved.root_cause == "Memory pressure on Orin node caused API timeout cascade."
    assert len(resolved.action_items) == 2
    assert resolved.is_open is False


def test_close_incident(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    mgr.resolve_incident(open_incident.id)
    closed = mgr.close_incident(open_incident.id)
    assert closed.status == IncidentStatus.CLOSED
    assert closed.closed_at is not None
    assert closed.is_open is False


def test_update_nonexistent_incident_raises(mgr: IncidentManager) -> None:
    with pytest.raises(KeyError):
        mgr.update_status("nonexistent", IncidentStatus.RESOLVED)


# ---------------------------------------------------------------------------
# TTR calculation
# ---------------------------------------------------------------------------

def test_ttr_minutes_calculated_correctly(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    opened = datetime.fromisoformat(open_incident.opened_at)
    resolved_dt = opened + timedelta(minutes=13)
    open_incident.resolved_at = resolved_dt.isoformat()
    assert open_incident.ttr_minutes == pytest.approx(13.0, abs=0.01)


def test_ttr_none_when_unresolved(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    assert open_incident.ttr_minutes is None


def test_average_ttr_across_incidents(mgr: IncidentManager) -> None:
    for minutes in [10.0, 16.0]:
        inc = mgr.open_incident("slo", "svc", "Test")
        opened = datetime.fromisoformat(inc.opened_at)
        inc.resolved_at = (opened + timedelta(minutes=minutes)).isoformat()
        inc.status = IncidentStatus.RESOLVED

    avg = mgr.average_ttr()
    assert avg == pytest.approx(13.0, abs=0.01)  # (10 + 16) / 2 = 13


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

def test_escalation_not_triggered_immediately(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    # Freshly opened — should NOT escalate yet
    escalated = mgr.check_escalation(open_incident.id)
    assert escalated is False
    assert open_incident.escalated is False


def test_escalation_triggered_on_old_incident(mgr: IncidentManager) -> None:
    """Force opened_at far in the past to trigger escalation."""
    inc = mgr.open_incident("slo", "svc", "Old incident", severity=Severity.P1)
    # Back-date to 10 minutes ago (threshold for P1 is 5 min)
    ten_min_ago = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    inc.opened_at = ten_min_ago
    escalated = mgr.check_escalation(inc.id)
    assert escalated is True
    assert inc.escalated is True


def test_escalation_not_retriggered(mgr: IncidentManager) -> None:
    inc = mgr.open_incident("slo", "svc", "Old incident", severity=Severity.P1)
    ten_min_ago = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    inc.opened_at = ten_min_ago
    mgr.check_escalation(inc.id)   # first call escalates
    result = mgr.check_escalation(inc.id)  # second call should not re-escalate
    assert result is False


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def test_get_open_incidents(mgr: IncidentManager) -> None:
    inc = mgr.open_incident("slo", "svc", "Open one")
    open_list = mgr.get_open_incidents()
    assert any(i.id == inc.id for i in open_list)


def test_get_resolved_incidents_after_close(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    mgr.resolve_incident(open_incident.id)
    mgr.close_incident(open_incident.id)
    resolved = mgr.get_resolved_incidents()
    assert any(i.id == open_incident.id for i in resolved)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_incident_persisted_to_disk(tmp_path: Path) -> None:
    store = tmp_path / "incidents.json"
    mgr = IncidentManager(store_path=store)
    inc = mgr.open_incident("slo", "svc", "Disk test")

    mgr2 = IncidentManager(store_path=store)
    assert inc.id in mgr2.incidents
    assert mgr2.incidents[inc.id].title == "Disk test"


def test_incident_serialisation_round_trip(
    mgr: IncidentManager, open_incident: Incident
) -> None:
    d = open_incident.to_dict()
    restored = Incident.from_dict(d)
    assert restored.id       == open_incident.id
    assert restored.severity == open_incident.severity
    assert restored.status   == open_incident.status
