"""Compatibility entrypoint for explicit retention preview/apply."""
import runpy
from pathlib import Path
import sys
sys.argv = [sys.argv[0], "maintenance", *sys.argv[1:]]
runpy.run_path(str(Path(__file__).with_name("b2b_leads.py")), run_name="__main__")
