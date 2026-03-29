#!/usr/bin/env bash
# Priority-27 SLO test suite
# Runs all SLO evaluation and burn-rate tests via pytest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== Priority 27 — SLO Reliability Tests ==="
python -m pytest tests/test_slo_eval.py tests/test_burn_rate.py tests/test_slo_gate.py \
    -v --tb=short "$@"
