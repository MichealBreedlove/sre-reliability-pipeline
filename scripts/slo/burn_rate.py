"""Burn-Rate Calculator.

Implements the Google SRE multi-window burn-rate alerting model.

A burn rate of 1.0 means the error budget is being consumed at exactly the
rate that would exhaust it at the end of the 30-day compliance window.  A burn
rate of 14.4 means the budget will be gone in ~50 hours.

Standard alert thresholds (mirroring Google's Workbook chapter 5):
  - 1h  window: critical >= 14.4x,  warning >= 6.0x
  - 6h  window: critical >= 6.0x,   warning >= 3.0x
  - 24h window: critical >= 3.0x,   warning >= 1.5x
  - 7d  window: critical >= 1.0x,   warning >= 0.5x
  - 30d window: critical >= 1.0x,   warning >= 0.5x
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# (burn_rate_threshold, severity) — first match wins (highest first)
BURN_RATE_THRESHOLDS: Dict[str, List[Tuple[float, str]]] = {
    "1h":  [(14.4, "critical"), (6.0, "warning")],
    "6h":  [(6.0,  "critical"), (3.0, "warning")],
    "24h": [(3.0,  "critical"), (1.5, "warning")],
    "7d":  [(1.0,  "critical"), (0.5, "warning")],
    "30d": [(1.0,  "critical"), (0.5, "warning")],
}

WINDOW_HOURS: Dict[str, float] = {
    "1h":  1.0,
    "6h":  6.0,
    "24h": 24.0,
    "7d":  168.0,
    "30d": 720.0,
}

BUDGET_PERIOD_HOURS: float = 720.0  # 30-day compliance window


@dataclass
class BurnRateResult:
    slo_id: str
    window: str
    burn_rate: float
    error_budget_consumed: float       # fraction consumed during this window
    alert_severity: Optional[str]      # "critical" | "warning" | None
    alert_fired: bool
    time_to_exhaustion_hours: Optional[float]

    @property
    def is_alerting(self) -> bool:
        return self.alert_fired

    def summary(self) -> str:
        sev = self.alert_severity or "ok"
        tte = (
            f"~{self.time_to_exhaustion_hours:.1f}h to budget exhaustion"
            if self.time_to_exhaustion_hours is not None
            else "budget stable"
        )
        return (
            f"[{self.slo_id}] window={self.window} "
            f"burn={self.burn_rate:.2f}x severity={sev} {tte}"
        )


@dataclass
class BurnRateAlert:
    slo_id: str
    service: str
    window: str
    burn_rate: float
    severity: str
    message: str
    recommended_action: str = ""


class BurnRateCalculator:
    """Calculate burn rates and produce actionable alerts."""

    def __init__(self, budget_period_hours: float = BUDGET_PERIOD_HOURS):
        self.budget_period_hours = budget_period_hours

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate(
        self,
        slo_id: str,
        window: str,
        error_budget_consumed: float,
        budget_remaining: float,
    ) -> BurnRateResult:
        """
        Calculate the burn rate for a single SLO / window pair.

        Args:
            slo_id:               Identifier of the SLO being evaluated.
            window:               Time window label ("1h", "6h", …).
            error_budget_consumed: Fraction of *total* error budget consumed
                                   during this window (0-1).
            budget_remaining:     Fraction of total error budget still remaining
                                   (0-1) — used for time-to-exhaustion estimate.

        Returns:
            BurnRateResult with computed burn rate and alert state.
        """
        if window not in WINDOW_HOURS:
            raise ValueError(f"Unknown window {window!r}. Valid: {list(WINDOW_HOURS)}")

        window_h = WINDOW_HOURS[window]
        # Expected fraction consumed if burning at exactly 1x
        expected = window_h / self.budget_period_hours
        burn_rate = error_budget_consumed / expected if expected > 0 else 0.0

        # Time-to-exhaustion estimate
        tte = self._time_to_exhaustion(burn_rate, budget_remaining)

        # Determine alert severity
        alert_severity: Optional[str] = None
        for threshold, severity in BURN_RATE_THRESHOLDS.get(window, []):
            if burn_rate >= threshold:
                alert_severity = severity
                break

        return BurnRateResult(
            slo_id=slo_id,
            window=window,
            burn_rate=round(burn_rate, 4),
            error_budget_consumed=error_budget_consumed,
            alert_severity=alert_severity,
            alert_fired=alert_severity is not None,
            time_to_exhaustion_hours=tte,
        )

    def calculate_multi_window(
        self,
        slo_id: str,
        consumptions: Dict[str, float],
        budget_remaining: float,
    ) -> List[BurnRateResult]:
        """Calculate burn rates across multiple time windows at once."""
        return [
            self.calculate(slo_id, window, consumed, budget_remaining)
            for window, consumed in consumptions.items()
        ]

    def generate_alerts(
        self,
        results: List[BurnRateResult],
        service: str = "",
    ) -> List[BurnRateAlert]:
        """Convert firing BurnRateResults into BurnRateAlert objects."""
        alerts: List[BurnRateAlert] = []
        for r in results:
            if not r.alert_fired:
                continue
            alerts.append(
                BurnRateAlert(
                    slo_id=r.slo_id,
                    service=service,
                    window=r.window,
                    burn_rate=r.burn_rate,
                    severity=r.alert_severity,  # type: ignore[arg-type]
                    message=(
                        f"Burn rate {r.burn_rate:.1f}x exceeds "
                        f"{r.alert_severity} threshold for window {r.window}"
                    ),
                    recommended_action=self._recommend_action(r),
                )
            )
        return alerts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _time_to_exhaustion(
        self,
        burn_rate: float,
        budget_remaining: float,
    ) -> Optional[float]:
        if budget_remaining <= 0:
            return 0.0
        if burn_rate <= 0:
            return None
        # Rate of consumption per hour = burn_rate / budget_period_hours
        consumption_per_hour = burn_rate / self.budget_period_hours
        if consumption_per_hour <= 0:
            return None
        return round(budget_remaining / consumption_per_hour, 2)

    @staticmethod
    def _recommend_action(result: BurnRateResult) -> str:
        if result.alert_severity == "critical":
            return (
                "Immediate investigation required. "
                "Check service logs, recent deployments, and infrastructure alerts. "
                "Consider rolling back recent changes."
            )
        if result.alert_severity == "warning":
            return (
                "Monitor closely. "
                "Review recent changes and prepare incident response. "
                "No immediate action required unless burn rate increases."
            )
        return "No action required."
