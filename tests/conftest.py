"""Pytest configuration — add scripts and tests directories to sys.path."""
import sys
from pathlib import Path

ROOT         = Path(__file__).parent.parent
TESTS_DIR    = Path(__file__).parent
SLO_DIR      = ROOT / "scripts" / "slo"
INCIDENT_DIR = ROOT / "scripts" / "incident"
CONFIG_DIR   = ROOT / "config"

for _p in (TESTS_DIR, SLO_DIR, INCIDENT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

CATALOG_PATH = CONFIG_DIR / "slo_catalog.json"
