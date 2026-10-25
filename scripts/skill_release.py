"""Freeze and install a matched Skill/core bundle without copying business data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

FORMAT = "b2b-leads-release-1"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def source_files(root):
    files = set()
    for folder, patterns in (("backend/app", ("*.py",)), ("backend/migrations", ("*.py",)),
                             ("scripts", ("*.py", "*.ps1")),
                             ("skills/b2b-leads", ("*.md", "*.py", "*.yaml"))):
        for pattern in patterns:
            files.update(path for path in (root / folder).rglob(pattern)
                         if "__pycache__" not in path.parts)
    files.update(root / name for name in ("backend/pyproject.toml", "backend/requirements.lock", "backend/alembic.ini")
                 if (root / name).is_file())
    required = ("scripts/b2b_leads.py", "skills/b2b-leads/SKILL.md", "skills/b2b-leads/scripts/control.py")
    if any(root / name not in files for name in required):
        raise ValueError("Skill source or controller is missing")
    content = {}
    for path in sorted(files):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Release sources must be ordinary files inside the project")
        content[path.relative_to(root).as_posix()] = path.read_bytes()
    return content


def verify_bundle(path, release_id=None):
    path = Path(path).resolve()
    manifest = json.loads((path / "release.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Invalid release manifest")
    files = manifest.get("files")
    if manifest.get("format") != FORMAT or not isinstance(files, dict) or not files:
        raise ValueError("Invalid release manifest")
    identity = digest(encoded(files))
    if identity != manifest.get("release_id") or release_id and identity != release_id:
        raise ValueError("Release identity mismatch")
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("Invalid release file entry")
        target = (path / name).resolve()
        if target == path or not target.is_relative_to(path) or not target.is_file() or digest(target.read_bytes()) != expected:
            raise ValueError("Release file integrity mismatch")
    actual = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    if actual != set(files) | {"release.json"}:
        raise ValueError("Release contains unlisted files")
    return manifest


def freeze(root):
    root = Path(root).resolve()
    content = source_files(root)
    files = {name: digest(data) for name, data in content.items()}
    release_id = digest(encoded(files))
    parent = (root / ".runtime" / "skill-releases").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    release = parent / release_id
    if release.exists():
        return release, verify_bundle(release, release_id)
    staging = parent / (".staging-" + uuid.uuid4().hex)
    staging.mkdir()
    # Capture the bytes read above, so edits during packaging cannot mix hashes
    # with a later version of a source file. Failed staging folders are retained.
    for name, data in content.items():
        atomic_write(staging / name, data)
    commit = None
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, timeout=5)
        if result.returncode == 0:
            commit = result.stdout.decode("ascii").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    manifest = {"format": FORMAT, "release_id": release_id, "git_commit": commit, "files": files}
    atomic_write(staging / "release.json", encoded(manifest))
    verify_bundle(staging, release_id)
    # Both resolved directory targets are checked before moving the complete tree.
    if staging.resolve().parent != parent or release.resolve().parent != parent:
        raise ValueError("Release directory escaped its namespace")
    try:
        staging.replace(release)
    except OSError:
        if not release.exists():
            raise
        verify_bundle(release, release_id)
    return release, manifest


def install(root, destination, release_id=None):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    source = root / "skills" / "b2b-leads"
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Install destination must differ from the Skill source")
    marker_path = destination / "project.json"
    if destination.exists():
        if not marker_path.is_file():
            raise ValueError("Existing skill has no managed project marker; preserved")
        old = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        if Path(old.get("project_root", "")).resolve() != root:
            raise ValueError("Existing skill belongs to another project; preserved")
    if release_id:
        if len(release_id) != 64 or any(character not in "0123456789abcdef" for character in release_id):
            raise ValueError("Invalid release id")
        release = root / ".runtime" / "skill-releases" / release_id
        manifest = verify_bundle(release, release_id)
    else:
        release, manifest = freeze(root)
    prefix = "skills/b2b-leads/"
    marker = {"project_root": str(root), "source": "b2b-leads", "version": 3,
              "release_root": str(release), "release_id": manifest["release_id"]}
    previous = {}
    try:
        for name in manifest["files"]:
            if name.startswith(prefix):
                relative = name[len(prefix):]
                target = (destination / relative).resolve()
                if not target.is_relative_to(destination):
                    raise ValueError("Installed file escaped the Skill destination")
                previous[target] = target.read_bytes() if target.exists() else None
                atomic_write(target, (release / name).read_bytes())
        # A crash mid-install fails closed; retry the install to recover. Ordinary
        # write failures roll back managed Skill files before returning an error.
        atomic_write(marker_path, encoded(marker))
    except BaseException:
        for target, original in previous.items():
            if original is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, original)
        raise
    return {"installed": True, "skill": "b2b-leads", "path": str(destination),
            "project_root": str(root), "release_id": manifest["release_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--release", help="Reactivate an existing verified release")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.root, args.destination, args.release), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": {"code": "RELEASE_INSTALL_FAILED",
              "message": "安装未完成；检查发布文件、目标归属和路径。原有发布快照保留。"}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
