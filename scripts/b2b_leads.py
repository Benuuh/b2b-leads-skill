"""Maintained thin entrypoint used by the installed b2b-leads Skill."""
import os
from pathlib import Path
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT
if "--root" in sys.argv:
    index = sys.argv.index("--root")
    if index + 1 < len(sys.argv):
        ROOT = Path(sys.argv[index + 1]).resolve()
os.environ.setdefault("SCA_PROJECT_ROOT", str(ROOT))
sys.path.insert(0, str(CODE_ROOT / "backend"))

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
