"""SLO Runner — Orchestrator.

Runs a full SLO evaluation cycle:
  1. Evaluate SLOs across all time windows
  2. Calculate burn rates and fire alerts
  3. Check safety gates for pending automation
  4. Open incidents for critical violations

Usage:
    python scripts/slo/slo_runner.py --config config/slo_catalog.json
"""
from __future__ import annotations

import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Allow running standalone or imported from tests (with conftest.py setting path)
_SLO_DIR      = Path(__file__).parent
_INCIDENT_DIR = _SLO_DIR.parent / "incident"

for _p in (_SLO_DIR, _INCIDENT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from slo_eval      import SLOEvaluator, SLIReading, SLOEvalResult, WINDOWS
from burn_rate     import BurnRateCalculator, BurnRateResult, BurnRateAlert
from slo_gate      import SLOGate, GateResult, GateDecision
from incident_manager import IncidentManager, Incident, Severity


DEFAULT_CATALOG = Path(__file__).parent.parent.parent / "config" / "slo_catalog.json"


class SLORunnerResult:
    """Aggregated output from one full evaluation cycle."""

    def __init__(self) -> None:
        self.eval_results:       Dict[str, List[SLOEvalResult]]  = {}
        self.burn_rate_results:  Dict[str, List[BurnRateResult]] = {}
        self.alerts:             List[BurnRateAlert]              = []
        self.gate_checks:        Dict[str, GateResult]           = {}
        self.new_incidents:      List[Incident]                  = []
        self.run_at:             str = datetime.now(timezone.utc).isoformat()

    @property
    def total_slos_evaluated(self) -> int:
        return sum(len(v) for v in self.eval_results.values())

    @property
    def passing_slos(self) -> int:
        return sum(
            sum(1 for r in v if r.passed) for v in self.eval_results.values()
        )

    @property
    def failing_slos(self) -> int:
        return self.total_slos_evaluated - self.passing_slos

    def summary(self) -> str:
        return (
            f"Run at {self.run_at}\n"
            f"  SLOs evaluated : {self.total_slos_evaluated} "
            f"({self.passing_slos} passing, {self.failing_slos} failing)\n"
            f"  Burn-rate alerts: {len(self.alerts)}\n"
            f"  New incidents   : {len(self.new_incidents)}"
        )


class SLORunner:
    """Orchestrate the full reliability evaluation cycle."""

    def __init__(
        self,
        catalog_path: str | Path = DEFAULT_CATALOG,
        incident_store: Optional[str | Path] = None,
    ) -> None:
        self.evaluator    = SLOEvaluator(catalog_path)
        self.burn_calc    = BurnRateCalculator()
        self.gate         = SLOGate()
        self.incident_mgr = IncidentManager(store_path=incident_store)

    def run(
        self,
        sli_by_window: Dict[str, List[SLIReading]],
        pending_actions: Optional[List[str]] = None,
    ) -> SLORunnerResult:
        """
        Run a full evaluation cycle.

        Args:
            sli_by_window:    Mapping of window label → list of SLI readings.
            pending_actions:  Automation actions to check against the safety gate.

        Returns:
            SLORunnerResult with all evaluation output.
        """
        result = SLORunnerResult()

        # ── 1. Evaluate SLOs ────────────────────────────────────────────
        result.eval_results = self.evaluator.evaluate_all_windows(sli_by_window)

        # ── 2. Aggregate budget state per SLO ───────────────────────────
        budget_remaining:    Dict[str, float]              = {}
        consumptions_by_slo: Dict[str, Dict[str, float]]  = {}

        for window, eval_list in result.eval_results.items():
            for er in eval_list:
                slo_id = er.slo_id
                consumptions_by_slo.setdefault(slo_id, {})[window] = (
                    er.error_budget_consumed
                )
                # Track the worst (lowest) budget remaining seen across windows
                budget_remaining[slo_id] = min(
                    budget_remaining.get(slo_id, 1.0),
                    er.error_budget_remaining,
                )

        # ── 3. Calculate burn rates ──────────────────────────────────────
        burn_by_slo: Dict[str, List[BurnRateResult]] = {}
        for slo_id, consumptions in consumptions_by_slo.items():
            br_list = self.burn_calc.calculate_multi_window(
                slo_id, consumptions, budget_remaining.get(slo_id, 1.0)
            )
            burn_by_slo[slo_id] = br_list
            result.burn_rate_results[slo_id] = br_list

        # ── 4. Generate alerts ───────────────────────────────────────────
        for slo_id, br_list in burn_by_slo.items():
            slo_def = next((s for s in self.evaluator.slos if s.id == slo_id), None)
            service = slo_def.service if slo_def else slo_id
            result.alerts.extend(self.burn_calc.generate_alerts(br_list, service))

        # ── 5. Safety gate ───────────────────────────────────────────────
        if pending_actions:
            worst_budget   = min(budget_remaining.values()) if budget_remaining else 1.0
            all_br_results = [r for rs in burn_by_slo.values() for r in rs]
            for action in pending_actions:
                result.gate_checks[action] = self.gate.check(
                    action, worst_budget, all_br_results
                )

        # ── 6. Auto-open incidents for critical SLOs ─────────────────────
        for slo_id, br_list in burn_by_slo.items():
            critical = [r for r in br_list if r.alert_severity == "critical"]
            if not critical:
                continue
            # Skip if an incident is already open for this SLO
            already_open = any(
                i.slo_id == slo_id for i in self.incident_mgr.get_open_incidents()
            )
            if already_open:
                continue

            worst         = max(critical, key=lambda r: r.burn_rate)
            slo_def       = next(
                (s for s in self.evaluator.slos if s.id == slo_id), None
            )
            service       = slo_def.service if slo_def else slo_id
            budget_rem    = budget_remaining.get(slo_id, 1.0)

            incident = self.incident_mgr.open_incident(
                slo_id=slo_id,
                service=service,
                title=(
                    f"SLO violation: {slo_id} burn rate {worst.burn_rate:.1f}x "
                    f"[{worst.window}]"
                ),
                description=(
                    f"Critical burn rate of {worst.burn_rate:.1f}x detected "
                    f"in window {worst.window}. "
                    f"Error budget remaining: {budget_rem:.1%}."
                ),
                burn_rate=worst.burn_rate,
                budget_remaining=budget_rem,
            )
            result.new_incidents.append(incident)

        return result


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="SLO Runner")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CATALOG),
        help="Path to slo_catalog.json",
    )
    args = parser.parse_args()

    runner = SLORunner(catalog_path=args.config)
    print(f"SLO Runner initialised with {len(runner.evaluator.slos)} SLOs.")
    print("Pass sli_by_window data via SLORunner.run() to execute a cycle.")


if __name__ == "__main__":  # pragma: no cover
    main()
