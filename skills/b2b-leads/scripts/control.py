"""Run the verified installed release, or the source controller in development."""
import sys

# Python normally searches this writable scripts directory before the standard
# library. Remove it before importing even the verifier's own dependencies.
sys.path[:] = [entry for entry in sys.path[1:] if entry]
for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, 'reconfigure', None)
    if callable(reconfigure):
        reconfigure(encoding='utf-8')
import hashlib
import json
from pathlib import Path
import runpy

# Frozen bundles contain source only. Reject existing caches below and avoid
# generating caches that could later execute different bytes from checked source.
sys.dont_write_bytecode = True
skill = Path(__file__).resolve().parents[1]
marker = skill / "project.json"
root = skill.parents[1]
code_root = root
if marker.is_file():
    try:
        installed = json.loads(marker.read_text(encoding="utf-8-sig"))
        if not isinstance(installed, dict) or installed.get('source') != 'b2b-leads' or installed.get('version') != 3:
            raise ValueError("Unsupported installation marker")
        root = Path(installed["project_root"]).resolve()
        release_id = installed['release_id']
        if not isinstance(release_id, str) or len(release_id) != 64 or any(c not in '0123456789abcdef' for c in release_id):
            raise ValueError("Invalid release id")
        code_root = Path(installed["release_root"]).resolve()
        if code_root != (root / '.runtime' / 'skill-releases' / release_id).resolve():
            raise ValueError("Release belongs to a different project")
        manifest = json.loads((code_root / "release.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Invalid release manifest")
        files = manifest["files"]
        if not isinstance(files, dict) or not files:
            raise ValueError("Invalid release file inventory")
        identity = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")).hexdigest()
        if manifest.get("format") != "b2b-leads-release-1" or identity != installed["release_id"] or identity != manifest["release_id"]:
            raise ValueError("Release identity mismatch")
        for name, expected in files.items():
            if not isinstance(name, str) or not isinstance(expected, str) or len(expected) != 64:
                raise ValueError("Invalid release file entry")
            target = (code_root / name).resolve()
            if not target.is_relative_to(code_root) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("Release integrity mismatch")
            prefix = "skills/b2b-leads/"
            if name.startswith(prefix):
                local = (skill / name[len(prefix):]).resolve()
                if not local.is_relative_to(skill) or hashlib.sha256(local.read_bytes()).hexdigest() != expected:
                    raise ValueError("Installed Skill differs from its release")
        actual = {item.relative_to(code_root).as_posix() for item in code_root.rglob('*') if item.is_file()}
        if actual != set(files) | {'release.json'}:
            raise ValueError("Release contains unlisted files")
        installed_files = {name[len('skills/b2b-leads/'):] for name in files if name.startswith('skills/b2b-leads/')}
        executable = {'.py', '.pyc', '.pyd', '.so', '.dll', '.pth', '.zip'}
        if any(item.suffix.lower() in executable and item.relative_to(skill).as_posix() not in installed_files
               for item in skill.rglob('*') if item.is_file()):
            raise ValueError("Installed Skill contains unlisted executable files")
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({"error": {"code": "RELEASE_INTEGRITY_ERROR", "message": "Skill 与核心发布不一致；请从项目重新安装或选择已验证的旧发布。"}}, ensure_ascii=False))
        raise SystemExit(1)
controller = code_root / "scripts" / "b2b_leads.py"
if not controller.is_file():
    raise SystemExit("Project controller is unavailable; reinstall this skill from its checkout")
sys.argv = [str(controller), "--root", str(root), *sys.argv[1:]]
runpy.run_path(str(controller), run_name="__main__")
