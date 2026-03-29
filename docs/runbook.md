# SRE Reliability Pipeline — Operations Runbook

**Owner:** Micheal Breedlove
**Cluster:** Aegis (Jasper / Nova / Mira / Orin)
**Last Updated:** 2026-03-29

---

## Table of Contents

1. [Overview](#overview)
2. [Components](#components)
3. [Running the Pipeline](#running-the-pipeline)
4. [SLO Catalog](#slo-catalog)
5. [Burn-Rate Alert Thresholds](#burn-rate-alert-thresholds)
6. [Safety Gate Actions](#safety-gate-actions)
7. [Incident Lifecycle](#incident-lifecycle)
8. [Postmortem Process](#postmortem-process)
9. [Running Tests](#running-tests)
10. [Troubleshooting](#troubleshooting)

---

## Overview

The SRE Reliability Pipeline evaluates 6 SLOs across 5 sliding time windows,
detects accelerating error-budget consumption via multi-window burn-rate
alerting, and automatically opens incidents with severity classification when
critical thresholds are exceeded.

All automation that could affect service reliability must pass through the
**SLO Safety Gate** before executing. The gate blocks high-risk actions when
the error budget is critically low or a critical burn-rate alert is active.

---

## Components

| File | Role |
|---|---|
| `scripts/slo/slo_eval.py` | Evaluate SLIs against SLO targets; produce per-window results |
| `scripts/slo/burn_rate.py` | Calculate multi-window burn rates; fire alerts |
| `scripts/slo/slo_gate.py` | Safety gate — block/warn risky automation |
| `scripts/slo/slo_runner.py` | Orchestrator — runs a full evaluation cycle |
| `scripts/incident/incident_manager.py` | Incident lifecycle: open, track, resolve, close |
| `scripts/incident/incident_render.py` | Postmortem markdown generation |
| `config/slo_catalog.json` | SLO definitions (6 SLOs) |

---

## Running the Pipeline

### Full evaluation cycle (programmatic)

```python
from scripts.slo.slo_runner import SLORunner, SLORunnerResult
from scripts.slo.slo_eval import SLIReading

runner = SLORunner(
    catalog_path="config/slo_catalog.json",
    incident_store="/var/lib/sre/incidents.json",
)

# Build SLI readings from your monitoring system (Prometheus, etc.)
readings = {
    "1h": [
        SLIReading("proxmox-api",  "1h", total_requests=3600, good_requests=3598),
        SLIReading("grafana",      "1h", p95_latency_ms=110),
        SLIReading("prometheus",   "1h", total_requests=500, good_requests=499),
        SLIReading("nfs",          "1h", total_requests=200, good_requests=200),
        SLIReading("node-exporter","1h", error_rate=0.002),
        SLIReading("vm-lifecycle", "1h", total_requests=10, good_requests=10),
    ],
    # Repeat for "6h", "24h", "7d", "30d" ...
}

result = runner.run(readings, pending_actions=["rolling_deploy"])
print(result.summary())
```

### Incident tick (escalation check)

```bash
python scripts/incident/incident_manager.py --tick --store /var/lib/sre/incidents.json
```

### SLO evaluation (CLI)

```bash
python scripts/slo/slo_eval.py --config config/slo_catalog.json --window 1h
```

---

## SLO Catalog

| SLO ID | Service | Type | Target | Description |
|---|---|---|---|---|
| `proxmox-api-availability` | `proxmox-api` | availability | 99.9% | Proxmox REST API (port 8006) responds successfully |
| `grafana-dashboard-latency` | `grafana` | latency | p95 < 500ms | Grafana dashboard load time |
| `prometheus-scrape-success` | `prometheus` | availability | 99.5% | Prometheus scrapes all targets |
| `nfs-mount-availability` | `nfs` | availability | 99.9% | NFS mounts accessible across nodes |
| `node-exporter-error-rate` | `node-exporter` | error_rate | < 1% | Node exporter collection errors |
| `vm-start-success` | `vm-lifecycle` | availability | 99% | VM start operations succeed |

---

## Burn-Rate Alert Thresholds

Modelled after Google SRE Workbook chapter 5 (multi-window burn-rate alerts).

| Window | Critical (block) | Warning (warn) |
|---|---|---|
| 1h | ≥ 14.4× | ≥ 6.0× |
| 6h | ≥ 6.0× | ≥ 3.0× |
| 24h | ≥ 3.0× | ≥ 1.5× |
| 7d | ≥ 1.0× | ≥ 0.5× |
| 30d | ≥ 1.0× | ≥ 0.5× |

A burn rate of **1.0×** = consuming budget at exactly the rate that exhausts it
in 30 days.  A burn rate of **14.4×** = budget gone in ~50 hours.

---

## Safety Gate Actions

| Action | Risk Level | Block Condition |
|---|---|---|
| `health_check`, `status_check`, `metric_scrape` | Low | Never blocked |
| `config_reload`, `scaling_up`, `alert_silence` | Medium | Warn if budget < 20% |
| `vm_restart`, `service_restart`, `rolling_deploy` | High | Block if budget < 5% or burn ≥ 14.4× |
| `database_migration`, `firewall_change`, `node_drain` | High | Block if budget < 5% or burn ≥ 14.4× |

---

## Incident Lifecycle

```
open → investigating → mitigating → resolved → closed
```

### Severity classification

| Severity | Burn Rate | Budget Remaining | Escalation (minutes) |
|---|---|---|---|
| P1 | ≥ 14.4× | ≤ 0% | 5 min |
| P2 | ≥ 6.0× | < 5% | 15 min |
| P3 | ≥ 3.0× | < 20% | 60 min |
| P4 | < 3.0× | ≥ 20% | 240 min |

### Opening a manual incident

```python
from scripts.incident.incident_manager import IncidentManager, Severity

mgr = IncidentManager(store_path="/var/lib/sre/incidents.json")
inc = mgr.open_incident(
    slo_id="proxmox-api-availability",
    service="proxmox-api",
    title="Proxmox API elevated error rate on node Orin",
    description="API returning 503 on ~3% of requests since 14:22 UTC",
    burn_rate=15.0,
    budget_remaining=0.03,
)
print(f"Opened incident {inc.id} ({inc.severity.value})")
```

### Resolving an incident

```python
mgr.resolve_incident(
    inc.id,
    root_cause="NFS I/O contention caused Proxmox worker thread exhaustion.",
    action_items=[
        "Tune NFS `rsize`/`wsize` mount options",
        "Add NFS I/O utilisation alert at 70%",
        "Review Proxmox API worker thread pool sizing",
    ],
)
mgr.close_incident(inc.id)
```

---

## Postmortem Process

1. Resolve the incident with root cause and action items
2. Generate the postmortem markdown:

```python
from scripts.incident.incident_render import render_postmortem

md = render_postmortem(inc)
with open(f"docs/postmortems/{inc.id}.md", "w") as f:
    f.write(md)
```

3. Review, sign off, and commit to the repo
4. Close the incident

---

## Running Tests

```bash
# Install dependencies
pip install -r requirements.txt

# All tests (38+ cases)
pytest tests/ -v

# SLO tests only (Priority 27)
bash scripts/test_priority27_slo.sh

# Incident tests only (Priority 28)
bash scripts/incident/test_priority28_incidents.sh

# With coverage report
pytest tests/ --cov=scripts --cov-report=term-missing
```

---

## Troubleshooting

### `KeyError: 'Incident not found'`

The incident ID doesn't exist in the store. Check the store file path and
verify the ID with `mgr.incidents.keys()`.

### `ValueError: Unknown window`

Only `1h`, `6h`, `24h`, `7d`, `30d` are valid window identifiers.

### All SLOs skipped in evaluation

SLI readings must have the same `service` name as defined in `slo_catalog.json`.
Check that service names match exactly (case-sensitive).

### Gate blocks everything unexpectedly

Check `budget_remaining` value passed to `SLOGate.check()`.  A value of `0.0`
exhausts the budget and blocks all non-trivial actions. Verify your SLI data
is current and not showing stale/zero request counts.

### Tests fail with import errors

Ensure `conftest.py` is present in the `tests/` directory and that
`scripts/slo/` and `scripts/incident/` exist. Run pytest from the repo root:

```bash
cd /path/to/sre-pipeline-work
pytest tests/ -v
```
