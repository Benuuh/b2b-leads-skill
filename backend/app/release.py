"""Report the immutable installed bundle used to execute a command."""
import json
from pathlib import Path

from app import __version__

CORE_VERSION = __version__
SKILL_VERSION = __version__


def release_identity():
    from app.persistence import SCHEMA_REVISION
    manifest_path = Path(__file__).resolve().parents[2] / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    return {"core_version": CORE_VERSION, "skill_version": SKILL_VERSION,
            "schema_revision": SCHEMA_REVISION,
            "release_id": manifest.get("release_id", "development"),
            "git_commit": manifest.get("git_commit")}


def versions():
    """Version envelope printed by every command and embedded in each result."""
    from app.capabilities import RULE_VERSION
    identity = release_identity()
    return {"skill_version": SKILL_VERSION, "core_version": CORE_VERSION,
            "rule_version": RULE_VERSION, "schema_revision": identity["schema_revision"],
            "release_id": identity["release_id"], "git_commit": identity.get("git_commit"),
            "installed": identity["release_id"] != "development"}
