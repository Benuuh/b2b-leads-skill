"""Compatibility shim: the converter now ships as ``app.evidence_stage``."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.evidence_stage import *  # noqa: F401,F403,E402
