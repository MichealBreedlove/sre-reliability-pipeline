# SRE Reliability Pipeline

Reliability automation system implementing SLO evaluation, burn-rate alerting, incident response workflows, and auto-generated postmortems.

Built for a 4-node homelab cluster. Part of the infrastructure documented at [michealbreedlove.com](https://michealbreedlove.com).

---

## Overview

This pipeline provides production-grade SRE practices applied to homelab infrastructure:

- **SLO Evaluation** — 6 service-level objectives tracked across 5 sliding time windows
- **Burn-Rate Alerting** — Detects accelerating error budget consumption before SLO breach
- **Incident Management** — Automated detection, severity classification, and escalation
- **Postmortem Generation** — Auto-generated reports with timeline, root cause, and action items
- **Safety Gates** — Block risky automation when error budget is exhausted
- **Acceptance Tests** — 38+ tests validate every pipeline component

---

## Architecture

```
┌─────────────┐    ┌──────────────┐    ┌───────────────┐
│ SLI Sources │───>│ SLO Evaluator│───>│ Budget Tracker│
└─────────────┘    └──────────────┘    └───────┬───────┘
                                               │
                   ┌──────────────┐    ┌───────▼───────┐
                   │  Safety Gate │<───│ Burn-Rate Calc│
                   └──────┬───────┘    └───────────────┘
                          │
              ┌───────────▼───────────┐
              │  Incident Manager     │
              │  (detect/track/close) │
              └───────────┬───────────┘
                          │
              ┌───────────▼───────────┐
              │  Postmortem Generator │
              └───────────────────────┘
```

---

## Key Components

| Component | Purpose | Language |
|---|---|---|
| `slo_eval.py` | Evaluate SLOs against SLI data | Python |
| `burn_rate.py` | Calculate burn rate across time windows | Python |
| `slo_gate.py` | Safety gate — block actions on budget exhaustion | Python |
| `incident_manager.py` | Incident lifecycle management | Python |
| `incident_render.py` | Postmortem report generation | Python |
| `slo_runner.py` | Orchestrator — runs full evaluation cycle | Python |

---

## Results

| Metric | Value |
|---|---|
| SLOs tracked | 6 |
| Time windows | 5 (1h, 6h, 24h, 7d, 30d) |
| Tests passing | 38+ |
| Avg TTR | 13 minutes |
| False positives | 0 |

---

## Usage

```bash
# Run SLO evaluation cycle
python scripts/slo/slo_eval.py --config config/slo_catalog.json

# Run incident check
python scripts/incident/incident_manager.py --tick

# Run all tests
bash scripts/test_priority27_slo.sh
bash scripts/incident/test_priority28_incidents.sh
```

---

## Security Considerations

- No credentials stored in the repository
- All configuration values use environment variables or local policy files
- Incident reports are sanitized before any public rendering

---

## Links

- [Case Study](https://michealbreedlove.com/case-study-sre-pipeline.html)
- [AI Cluster Architecture](https://michealbreedlove.com/ai-cluster.html)
- [Portfolio](https://michealbreedlove.com)
- [Lab Repository](https://github.com/MichealBreedlove/Lab)
