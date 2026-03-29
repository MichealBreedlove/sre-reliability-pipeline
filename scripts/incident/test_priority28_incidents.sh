#!/usr/bin/env bash
# Priority-28 Incident management test suite
# Runs incident lifecycle and integration tests via pytest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

echo "=== Priority 28 — Incident Management Tests ==="
python -m pytest tests/test_incident_manager.py tests/test_integration.py \
    -v --tb=short "$@"
