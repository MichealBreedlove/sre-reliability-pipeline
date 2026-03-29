"""SLO Evaluation Engine.

Evaluates service-level indicators against SLO targets across 5 sliding time
windows: 1h, 6h, 24h, 7d, 30d.  Produces per-window results with error-budget
accounting ready for downstream burn-rate and gate checks.

Usage:
    python scripts/slo/slo_eval.py --config config/slo_catalog.json --window 1h
"""
from __future__ import annotations

import json
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any


WINDOWS: List[str] = ["1h", "6h", "24h", "7d", "30d"]

WINDOW_SECONDS: Dict[str, int] = {
    "1h":  3_600,
    "6h":  21_600,
    "24h": 86_400,
    "7d":  604_800,
    "30d": 2_592_000,
}


@dataclass
class SLODefinition:
    id: str
    name: str
    service: str
    sli_type: str          # "availability" | "latency" | "error_rate"
    target: float          # fraction (0-1) for avail/error_rate; ms for latency
    windows: List[str]
    threshold_ms: Optional[float] = None
    description: str = ""

    # Names that map from JSON keys
    _FIELDS = {
        "id", "name", "service", "sli_type", "target",
        "windows", "threshold_ms", "description",
    }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SLODefinition":
        filtered = {k: v for k, v in d.items() if k in cls._FIELDS}
        return cls(**filtered)


@dataclass
class SLIReading:
    """A single SLI measurement snapshot for one service in one time window."""
    service: str
    window: str
    timestamp: str = ""
    # Availability / throughput
    total_requests: int = 0
    good_requests: int = 0
    # Latency
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    # Explicit error rate (overrides good/total when provided)
    error_rate: Optional[float] = None

    def availability(self) -> float:
        """Fraction of requests that succeeded (0-1)."""
        if self.total_requests == 0:
            return 1.0
        return self.good_requests / self.total_requests

    def effective_error_rate(self) -> float:
        """Error rate as a fraction (0-1)."""
        if self.error_rate is not None:
            return self.error_rate
        if self.total_requests == 0:
            return 0.0
        return (self.total_requests - self.good_requests) / self.total_requests


@dataclass
class SLOEvalResult:
    slo_id: str
    slo_name: str
    service: str
    window: str
    target: float
    actual: float
    passed: bool
    error_budget_remaining: float   # fraction 0-1
    error_budget_consumed: float    # fraction 0-1
    evaluated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SLOEvaluator:
    """Load an SLO catalog and evaluate readings against it."""

    def __init__(self, catalog_path: str | Path):
        self.catalog_path = Path(catalog_path)
        self.slos: List[SLODefinition] = []
        self._load_catalog()

    def _load_catalog(self) -> None:
        with open(self.catalog_path) as fh:
            data = json.load(fh)
        self.slos = [SLODefinition.from_dict(e) for e in data.get("slos", [])]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        sli_readings: List[SLIReading],
        window: str = "1h",
    ) -> List[SLOEvalResult]:
        """Evaluate all SLOs for a single time window."""
        if window not in WINDOWS:
            raise ValueError(f"Unknown window {window!r}. Valid: {WINDOWS}")

        by_service: Dict[str, SLIReading] = {r.service: r for r in sli_readings}
        now = datetime.now(timezone.utc).isoformat()
        results: List[SLOEvalResult] = []

        for slo in self.slos:
            if window not in slo.windows:
                continue
            reading = by_service.get(slo.service)
            if reading is None:
                continue
            results.append(self._eval_single(slo, reading, window, now))

        return results

    def evaluate_all_windows(
        self,
        sli_by_window: Dict[str, List[SLIReading]],
    ) -> Dict[str, List[SLOEvalResult]]:
        """Evaluate all SLOs across every provided time window."""
        return {
            window: self.evaluate(readings, window)
            for window, readings in sli_by_window.items()
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _eval_single(
        self,
        slo: SLODefinition,
        reading: SLIReading,
        window: str,
        now: str,
    ) -> SLOEvalResult:
        if slo.sli_type == "latency":
            return self._eval_latency(slo, reading, window, now)
        elif slo.sli_type == "error_rate":
            return self._eval_error_rate(slo, reading, window, now)
        else:
            # "availability" and anything else
            return self._eval_availability(slo, reading, window, now)

    def _eval_availability(
        self,
        slo: SLODefinition,
        reading: SLIReading,
        window: str,
        now: str,
    ) -> SLOEvalResult:
        actual = reading.availability()
        passed = actual >= slo.target
        budget_total = 1.0 - slo.target
        error_consumed = max(0.0, slo.target - actual)
        if budget_total > 0:
            consumed_frac = min(1.0, error_consumed / budget_total)
        else:
            consumed_frac = 0.0 if passed else 1.0
        return SLOEvalResult(
            slo_id=slo.id,
            slo_name=slo.name,
            service=slo.service,
            window=window,
            target=slo.target,
            actual=actual,
            passed=passed,
            error_budget_remaining=1.0 - consumed_frac,
            error_budget_consumed=consumed_frac,
            evaluated_at=now,
        )

    def _eval_error_rate(
        self,
        slo: SLODefinition,
        reading: SLIReading,
        window: str,
        now: str,
    ) -> SLOEvalResult:
        # target = max allowed compliance fraction (e.g. 0.99 means <=1% error)
        compliance = 1.0 - reading.effective_error_rate()
        passed = compliance >= slo.target
        budget_total = 1.0 - slo.target
        error_consumed = max(0.0, slo.target - compliance)
        consumed_frac = (
            min(1.0, error_consumed / budget_total) if budget_total > 0
            else (0.0 if passed else 1.0)
        )
        return SLOEvalResult(
            slo_id=slo.id,
            slo_name=slo.name,
            service=slo.service,
            window=window,
            target=slo.target,
            actual=compliance,
            passed=passed,
            error_budget_remaining=1.0 - consumed_frac,
            error_budget_consumed=consumed_frac,
            evaluated_at=now,
        )

    def _eval_latency(
        self,
        slo: SLODefinition,
        reading: SLIReading,
        window: str,
        now: str,
    ) -> SLOEvalResult:
        threshold = slo.threshold_ms if slo.threshold_ms is not None else slo.target
        actual = reading.p95_latency_ms
        passed = actual <= threshold
        if passed:
            consumed_frac = 0.0
        else:
            # Proportional overshoot, capped at 100%
            consumed_frac = min(1.0, (actual - threshold) / threshold)
        return SLOEvalResult(
            slo_id=slo.id,
            slo_name=slo.name,
            service=slo.service,
            window=window,
            target=threshold,
            actual=actual,
            passed=passed,
            error_budget_remaining=1.0 - consumed_frac,
            error_budget_consumed=consumed_frac,
            evaluated_at=now,
        )


def load_slo_catalog(path: str | Path) -> List[SLODefinition]:
    with open(path) as fh:
        data = json.load(fh)
    return [SLODefinition.from_dict(e) for e in data.get("slos", [])]


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="SLO Evaluator")
    parser.add_argument("--config", required=True, help="Path to slo_catalog.json")
    parser.add_argument("--window", default="1h", choices=WINDOWS)
    args = parser.parse_args()

    evaluator = SLOEvaluator(args.config)
    print(f"Loaded {len(evaluator.slos)} SLOs from {args.config}")
    print(f"Window: {args.window}")
    print("Pass SLI readings programmatically via SLOEvaluator.evaluate().")


if __name__ == "__main__":  # pragma: no cover
    main()
