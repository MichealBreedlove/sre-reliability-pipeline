"""SLO Safety Gate.

Blocks or warns on risky automation actions when the error budget is critically
low or a high burn-rate alert is active.  Acts as the last line of defence
before any automated change reaches production infrastructure.

Decision logic
--------------
  LOW risk actions   → always ALLOW
  MEDIUM risk actions → WARN  when budget < 20%
  HIGH risk actions  → BLOCK when budget < 5%, burn rate >= 14.4x, or budget exhausted
  Any action         → BLOCK when budget is fully exhausted (0%)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from burn_rate import BurnRateResult


class GateDecision(Enum):
    ALLOW = "allow"
    WARN  = "warn"
    BLOCK = "block"


@dataclass
class GateResult:
    action: str
    decision: GateDecision
    reason: str
    slo_id: Optional[str] = None
    burn_rate: Optional[float] = None
    budget_remaining: Optional[float] = None

    @property
    def allowed(self) -> bool:
        return self.decision == GateDecision.ALLOW

    @property
    def blocked(self) -> bool:
        return self.decision == GateDecision.BLOCK

    @property
    def warned(self) -> bool:
        return self.decision == GateDecision.WARN


# Action → risk level mapping
ACTION_RISK: Dict[str, str] = {
    # Low risk — read-only or purely observational
    "read":                 "low",
    "status_check":         "low",
    "metric_scrape":        "low",
    "health_check":         "low",
    "log_query":            "low",
    # Medium risk — config or scaling changes with recoverable impact
    "config_reload":        "medium",
    "scaling_up":           "medium",
    "scheduled_maintenance":"medium",
    "alert_silence":        "medium",
    # High risk — changes with potential for extended outage
    "vm_restart":           "high",
    "service_restart":      "high",
    "rolling_deploy":       "high",
    "database_migration":   "high",
    "firewall_change":      "high",
    "network_reconfigure":  "high",
    "node_drain":           "high",
    "storage_rebalance":    "high",
}

# Thresholds
BLOCK_BUDGET_THRESHOLD: float = 0.05   # Block high-risk at < 5% remaining
WARN_BUDGET_THRESHOLD: float  = 0.20   # Warn medium-risk at < 20% remaining
BLOCK_BURN_RATE: float        = 14.4   # Block high-risk at critical 1h burn rate


class SLOGate:
    """Evaluate whether a given action is safe to execute."""

    def __init__(
        self,
        block_budget_threshold: float = BLOCK_BUDGET_THRESHOLD,
        warn_budget_threshold: float  = WARN_BUDGET_THRESHOLD,
        block_burn_rate: float        = BLOCK_BURN_RATE,
    ):
        self.block_budget_threshold = block_budget_threshold
        self.warn_budget_threshold  = warn_budget_threshold
        self.block_burn_rate        = block_burn_rate

    def check(
        self,
        action: str,
        budget_remaining: float,
        burn_rate_results: Optional[List[BurnRateResult]] = None,
        slo_id: str = "",
    ) -> GateResult:
        """
        Evaluate whether *action* should proceed.

        Args:
            action:             Action identifier (see ACTION_RISK map).
            budget_remaining:   Error budget fraction remaining (0-1).
            burn_rate_results:  Current burn-rate results for relevant SLOs.
            slo_id:             SLO context for the gate check.

        Returns:
            GateResult with ALLOW / WARN / BLOCK decision.
        """
        burn_rate_results = burn_rate_results or []
        risk = ACTION_RISK.get(action, "medium")

        max_burn = max((r.burn_rate for r in burn_rate_results), default=0.0)
        has_critical_alert = any(
            r.alert_severity == "critical" for r in burn_rate_results
        )

        # Low-risk: always allow
        if risk == "low":
            return GateResult(
                action=action,
                decision=GateDecision.ALLOW,
                reason="Low-risk action — always permitted regardless of budget state",
                slo_id=slo_id,
                budget_remaining=budget_remaining,
            )

        # Budget fully exhausted — block everything non-trivial
        if budget_remaining <= 0:
            return GateResult(
                action=action,
                decision=GateDecision.BLOCK,
                reason=(
                    f"Error budget exhausted (remaining={budget_remaining:.1%}). "
                    "All non-trivial automation blocked to protect reliability."
                ),
                slo_id=slo_id,
                budget_remaining=budget_remaining,
                burn_rate=max_burn if max_burn > 0 else None,
            )

        # High-risk: block on low budget OR critical burn rate
        if risk == "high":
            if budget_remaining < self.block_budget_threshold:
                return GateResult(
                    action=action,
                    decision=GateDecision.BLOCK,
                    reason=(
                        f"High-risk action blocked: budget critically low "
                        f"({budget_remaining:.1%} remaining, "
                        f"threshold {self.block_budget_threshold:.0%})."
                    ),
                    slo_id=slo_id,
                    budget_remaining=budget_remaining,
                    burn_rate=max_burn if max_burn > 0 else None,
                )
            if max_burn >= self.block_burn_rate:
                return GateResult(
                    action=action,
                    decision=GateDecision.BLOCK,
                    reason=(
                        f"High-risk action blocked: burn rate {max_burn:.1f}x "
                        f">= critical threshold {self.block_burn_rate}x."
                    ),
                    slo_id=slo_id,
                    budget_remaining=budget_remaining,
                    burn_rate=max_burn,
                )
            if has_critical_alert:
                return GateResult(
                    action=action,
                    decision=GateDecision.BLOCK,
                    reason="High-risk action blocked: critical burn-rate alert active.",
                    slo_id=slo_id,
                    budget_remaining=budget_remaining,
                    burn_rate=max_burn if max_burn > 0 else None,
                )

        # Medium-risk: warn on degraded budget or active alert
        if risk == "medium":
            if budget_remaining < self.warn_budget_threshold:
                return GateResult(
                    action=action,
                    decision=GateDecision.WARN,
                    reason=(
                        f"Budget degraded ({budget_remaining:.1%} remaining). "
                        "Proceed with caution and monitor SLO compliance."
                    ),
                    slo_id=slo_id,
                    budget_remaining=budget_remaining,
                    burn_rate=max_burn if max_burn > 0 else None,
                )
            if has_critical_alert:
                return GateResult(
                    action=action,
                    decision=GateDecision.WARN,
                    reason="Critical burn-rate alert active — proceed with caution.",
                    slo_id=slo_id,
                    budget_remaining=budget_remaining,
                    burn_rate=max_burn if max_burn > 0 else None,
                )

        return GateResult(
            action=action,
            decision=GateDecision.ALLOW,
            reason=f"Budget healthy ({budget_remaining:.1%} remaining). Action permitted.",
            slo_id=slo_id,
            budget_remaining=budget_remaining,
            burn_rate=max_burn if max_burn > 0 else None,
        )

    def check_batch(
        self,
        actions: List[str],
        budget_remaining: float,
        burn_rate_results: Optional[List[BurnRateResult]] = None,
        slo_id: str = "",
    ) -> Dict[str, GateResult]:
        """Check multiple actions in one call."""
        return {
            a: self.check(a, budget_remaining, burn_rate_results, slo_id)
            for a in actions
        }
