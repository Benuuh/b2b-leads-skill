"""Report the immutable installed bundle used to execute a command."""
import json
from pathlib import Path

CORE_VERSION = "2.0.1"


def release_identity():
    from app.persistence import SCHEMA_REVISION
    manifest_path = Path(__file__).resolve().parents[2] / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    return {"core_version": CORE_VERSION, "schema_revision": SCHEMA_REVISION,
            "release_id": manifest.get("release_id", "development"),
            "git_commit": manifest.get("git_commit")}
