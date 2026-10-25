"""Installed commands keep using a verified bundle as the checkout changes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from app.config import ROOT

spec = importlib.util.spec_from_file_location("skill_release", ROOT / "scripts" / "skill_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def project(tmp_path):
    root = tmp_path / "project with spaces"
    for name in ("scripts/b2b_leads.py", "skills/b2b-leads/scripts/control.py"):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    skill = root / "skills" / "b2b-leads" / "SKILL.md"
    skill.write_text("---\nname: b2b-leads\ndescription: synthetic installation test\n---\n", encoding="utf-8")
    app = root / "backend" / "app"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "cli.py").write_text('def main():\n    print("frozen-v1")\n    return 0\n', encoding="utf-8")
    (root / ".env").write_text("DO_NOT_PACKAGE=private-synthetic-value", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "contacts.csv").write_text("private-synthetic-contact", encoding="utf-8")
    return root


def command(destination):
    env = dict(os.environ)
    env.pop("SCA_PROJECT_ROOT", None)
    return subprocess.run([sys.executable, str(destination / "scripts" / "control.py"), "--help"],
                          capture_output=True, env=env, timeout=30)


def test_installed_release_is_frozen_and_old_release_can_be_reactivated(tmp_path):
    root, destination = project(tmp_path), tmp_path / "installed skill"
    first = release.install(root, destination)
    assert command(destination).stdout.strip() == b"frozen-v1"
    (root / "backend" / "app" / "cli.py").write_text('def main():\n    print("frozen-v2")\n    return 0\n', encoding="utf-8")
    assert command(destination).stdout.strip() == b"frozen-v1"
    second = release.install(root, destination)
    assert second["release_id"] != first["release_id"]
    assert command(destination).stdout.strip() == b"frozen-v2"
    release.install(root, destination, first["release_id"])
    assert command(destination).stdout.strip() == b"frozen-v1"
    marker = json.loads((destination / "project.json").read_text(encoding="utf-8"))
    manifest = release.verify_bundle(marker["release_root"])
    assert ".env" not in manifest["files"]
    assert not any(name.startswith("data/") for name in manifest["files"])


@pytest.mark.parametrize("tamper", ["skill", "core"])
def test_installed_release_rejects_changed_skill_or_bundle(tmp_path, tamper):
    root, destination = project(tmp_path), tmp_path / "installed"
    release.install(root, destination)
    marker = json.loads((destination / "project.json").read_text(encoding="utf-8"))
    target = destination / "SKILL.md" if tamper == "skill" else Path(marker["release_root"]) / "backend/app/cli.py"
    target.write_text("modified", encoding="utf-8")
    result = command(destination)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "RELEASE_INTEGRITY_ERROR"


def test_install_preserves_unowned_destination_and_rejects_source_target(tmp_path):
    root, destination = project(tmp_path), tmp_path / "unowned"
    destination.mkdir()
    sentinel = destination / "SKILL.md"
    sentinel.write_text("keep me", encoding="utf-8")
    with pytest.raises(ValueError, match="preserved"):
        release.install(root, destination)
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    with pytest.raises(ValueError, match="source"):
        release.install(root, root / "skills" / "b2b-leads")


@pytest.mark.parametrize('extra', ['backend/pycountry.py', 'backend/app/__pycache__/cli.cpython-312.pyc'])
def test_bundle_rejects_unlisted_modules_and_bytecode(tmp_path, extra):
    root, destination = project(tmp_path), tmp_path / 'installed'
    release.install(root, destination)
    marker = json.loads((destination / 'project.json').read_text(encoding='utf-8'))
    added = Path(marker['release_root']) / extra
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_bytes(b'untracked executable content')
    with pytest.raises(ValueError, match='unlisted'):
        release.verify_bundle(marker['release_root'])
    result = command(destination)
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'RELEASE_INTEGRITY_ERROR'


def test_install_write_failure_restores_prior_working_skill(tmp_path, monkeypatch):
    root, destination = project(tmp_path), tmp_path / 'installed'
    first = release.install(root, destination)
    (root / 'skills/b2b-leads/SKILL.md').write_text('changed instructions', encoding='utf-8')
    original = release.atomic_write
    def failed_activation(path, data):
        if path == destination / 'project.json':
            raise OSError('synthetic activation failure')
        return original(path, data)
    monkeypatch.setattr(release, 'atomic_write', failed_activation)
    with pytest.raises(OSError):
        release.install(root, destination)
    assert json.loads((destination / 'project.json').read_text(encoding='utf-8'))['release_id'] == first['release_id']
    assert command(destination).stdout.strip() == b'frozen-v1'


def test_wrapper_rejects_other_project_release_and_malformed_inventory(tmp_path):
    root, destination = project(tmp_path / 'one'), tmp_path / 'installed'
    release.install(root, destination)
    marker_path = destination / 'project.json'
    marker = json.loads(marker_path.read_text(encoding='utf-8'))
    other, _ = release.freeze(project(tmp_path / 'two'))
    original_root = marker['release_root']
    marker['release_root'] = str(other)
    marker_path.write_text(json.dumps(marker), encoding='utf-8')
    assert command(destination).returncode == 1
    marker['release_root'] = original_root
    marker_path.write_text(json.dumps(marker), encoding='utf-8')
    (Path(original_root) / 'release.json').write_text('{"files":[]}', encoding='utf-8')
    result = command(destination)
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'RELEASE_INTEGRITY_ERROR'


def test_installed_shadow_module_cannot_run_before_verification(tmp_path):
    root, destination = project(tmp_path), tmp_path / 'installed'
    release.install(root, destination)
    sentinel = tmp_path / 'shadow-executed'
    (destination / 'scripts/json.py').write_text(
        f'from pathlib import Path\nPath({str(sentinel)!r}).write_text("executed")\nraise SystemExit(73)\n',
        encoding='utf-8')
    result = command(destination)
    assert not sentinel.exists()
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'RELEASE_INTEGRITY_ERROR'


def test_windows_installer_runs_verified_wrapper(tmp_path):
    if os.name != "nt":
        return
    destination = tmp_path / "installed via PowerShell"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(ROOT / "scripts" / "install-b2b-leads.ps1"), "-Destination", str(destination)],
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert json.loads(result.stdout)["release_id"]
    executed = command(destination)
    assert executed.returncode == 0, executed.stdout.decode(errors="replace")
    assert b"delete-company" in executed.stdout
