"""Incident Manager.

Full lifecycle management for SLO-triggered incidents:
  open → investigating → mitigating → resolved → closed

Severity is derived automatically from burn rate or can be set explicitly.
Escalation triggers when an incident remains open past its severity threshold.

Usage:
    python scripts/incident/incident_manager.py --tick --store /tmp/incidents.json
"""
from __future__ import annotations

import json
import uuid
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any


class Severity(Enum):
    P1 = "P1"   # Critical — service completely unavailable
    P2 = "P2"   # High     — major degradation, SLO at risk
    P3 = "P3"   # Medium   — minor degradation, budget eroding slowly
    P4 = "P4"   # Low      — informational, budget healthy


class IncidentStatus(Enum):
    OPEN          = "open"
    INVESTIGATING = "investigating"
    MITIGATING    = "mitigating"
    RESOLVED      = "resolved"
    CLOSED        = "closed"


# Minutes before escalation fires at each severity level
ESCALATION_THRESHOLDS: Dict[Severity, int] = {
    Severity.P1: 5,
    Severity.P2: 15,
    Severity.P3: 60,
    Severity.P4: 240,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


def severity_from_burn_rate(burn_rate: float) -> Severity:
    """Classify severity based on observed burn rate."""
    if burn_rate >= 14.4:
        return Severity.P1
    if burn_rate >= 6.0:
        return Severity.P2
    if burn_rate >= 3.0:
        return Severity.P3
    return Severity.P4


def severity_from_budget(budget_remaining: float) -> Severity:
    """Classify severity based on remaining error budget."""
    if budget_remaining <= 0.0:
        return Severity.P1
    if budget_remaining < 0.05:
        return Severity.P2
    if budget_remaining < 0.20:
        return Severity.P3
    return Severity.P4


@dataclass
class TimelineEvent:
    timestamp: str
    event_type: str
    description: str
    actor: str = "system"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Incident:
    id: str
    slo_id: str
    service: str
    severity: Severity
    status: IncidentStatus
    title: str
    description: str
    opened_at: str
    updated_at: str
    resolved_at: Optional[str] = None
    closed_at: Optional[str] = None
    timeline: List[TimelineEvent] = field(default_factory=list)
    root_cause: str = ""
    action_items: List[str] = field(default_factory=list)
    escalated: bool = False
    burn_rate: float = 0.0
    budget_remaining: float = 1.0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.status not in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)

    @property
    def ttr_minutes(self) -> Optional[float]:
        """Time-to-resolution in minutes (None if unresolved)."""
        if self.resolved_at is None:
            return None
        opened   = datetime.fromisoformat(self.opened_at)
        resolved = datetime.fromisoformat(self.resolved_at)
        return (resolved - opened).total_seconds() / 60.0

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def add_event(
        self,
        event_type: str,
        description: str,
        actor: str = "system",
    ) -> None:
        self.timeline.append(
            TimelineEvent(
                timestamp=_now_iso(),
                event_type=event_type,
                description=description,
                actor=actor,
            )
        )
        self.updated_at = _now_iso()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["status"]   = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Incident":
        d = d.copy()
        d["severity"] = Severity(d["severity"])
        d["status"]   = IncidentStatus(d["status"])
        d["timeline"] = [TimelineEvent(**e) for e in d.get("timeline", [])]
        return cls(**d)


class IncidentManager:
    """Manage the full lifecycle of SLO-triggered incidents."""

    def __init__(self, store_path: Optional[str | Path] = None):
        self.incidents: Dict[str, Incident] = {}
        self.store_path = Path(store_path) if store_path else None
        if self.store_path and self.store_path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    def open_incident(
        self,
        slo_id: str,
        service: str,
        title: str,
        description: str = "",
        severity: Optional[Severity] = None,
        burn_rate: float = 0.0,
        budget_remaining: float = 1.0,
    ) -> Incident:
        """Open a new incident, returning the created object."""
        if severity is None:
            severity = severity_from_burn_rate(burn_rate)

        now = _now_iso()
        incident = Incident(
            id=str(uuid.uuid4())[:8],
            slo_id=slo_id,
            service=service,
            severity=severity,
            status=IncidentStatus.OPEN,
            title=title,
            description=description,
            opened_at=now,
            updated_at=now,
            burn_rate=burn_rate,
            budget_remaining=budget_remaining,
        )
        incident.add_event("opened", f"Incident opened: {title}")
        self.incidents[incident.id] = incident
        self._persist()
        return incident

    def update_status(
        self,
        incident_id: str,
        status: IncidentStatus,
        note: str = "",
        actor: str = "system",
    ) -> Incident:
        """Transition an incident to a new status."""
        incident = self._get(incident_id)
        old_status = incident.status
        incident.status = status
        desc = f"Status: {old_status.value} → {status.value}"
        if note:
            desc += f". {note}"
        incident.add_event("status_change", desc, actor=actor)

        if status == IncidentStatus.RESOLVED and incident.resolved_at is None:
            incident.resolved_at = _now_iso()
            incident.add_event("resolved", "Incident marked resolved", actor=actor)

        if status == IncidentStatus.CLOSED:
            incident.closed_at = _now_iso()
            incident.add_event("closed", "Incident closed", actor=actor)

        self._persist()
        return incident

    def resolve_incident(
        self,
        incident_id: str,
        root_cause: str = "",
        action_items: Optional[List[str]] = None,
        actor: str = "system",
    ) -> Incident:
        """Resolve an incident, optionally recording root cause and actions."""
        incident = self._get(incident_id)
        if root_cause:
            incident.root_cause = root_cause
        if action_items:
            incident.action_items = action_items
        return self.update_status(
            incident_id, IncidentStatus.RESOLVED, actor=actor
        )

    def close_incident(
        self,
        incident_id: str,
        actor: str = "system",
    ) -> Incident:
        """Close a resolved incident (post-postmortem sign-off)."""
        return self.update_status(
            incident_id, IncidentStatus.CLOSED, actor=actor
        )

    def check_escalation(self, incident_id: str) -> bool:
        """Return True and mark escalated if the incident exceeds its SLA."""
        incident = self._get(incident_id)
        if not incident.is_open or incident.escalated:
            return False

        opened = datetime.fromisoformat(incident.opened_at)
        elapsed_minutes = (_utcnow() - opened).total_seconds() / 60.0
        threshold = ESCALATION_THRESHOLDS.get(incident.severity, 60)

        if elapsed_minutes >= threshold:
            incident.escalated = True
            incident.add_event(
                "escalated",
                (
                    f"Escalated after {elapsed_minutes:.0f}m open "
                    f"(threshold {threshold}m for {incident.severity.value})"
                ),
            )
            self._persist()
            return True

        return False

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_open_incidents(self) -> List[Incident]:
        return [i for i in self.incidents.values() if i.is_open]

    def get_resolved_incidents(self) -> List[Incident]:
        return [
            i for i in self.incidents.values()
            if i.status in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)
        ]

    def average_ttr(self) -> Optional[float]:
        """Average time-to-resolution in minutes across all resolved incidents."""
        ttrs = [
            i.ttr_minutes
            for i in self.incidents.values()
            if i.ttr_minutes is not None
        ]
        return sum(ttrs) / len(ttrs) if ttrs else None

    def tick(self) -> List[str]:
        """Run one escalation-check pass; return human-readable log lines."""
        log: List[str] = []
        for incident in list(self.incidents.values()):
            if incident.is_open and self.check_escalation(incident.id):
                log.append(
                    f"ESCALATED {incident.id} ({incident.severity.value}): "
                    f"{incident.title}"
                )
        return log

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, incident_id: str) -> Incident:
        if incident_id not in self.incidents:
            raise KeyError(f"Incident {incident_id!r} not found")
        return self.incidents[incident_id]

    def _persist(self) -> None:
        if self.store_path is None:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.store_path, "w") as fh:
            json.dump({k: v.to_dict() for k, v in self.incidents.items()}, fh, indent=2)

    def _load(self) -> None:
        with open(self.store_path) as fh:  # type: ignore[arg-type]
            data = json.load(fh)
        for k, v in data.items():
            self.incidents[k] = Incident.from_dict(v)


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Incident Manager")
    parser.add_argument("--tick", action="store_true", help="Run escalation tick")
    parser.add_argument(
        "--store", default="/tmp/incidents.json", help="Incident store path"
    )
    args = parser.parse_args()

    mgr = IncidentManager(store_path=args.store)
    if args.tick:
        for line in mgr.tick():
            print(line)
        print(f"Open incidents: {len(mgr.get_open_incidents())}")


if __name__ == "__main__":  # pragma: no cover
    main()
