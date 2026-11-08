"""Observable command adapter behavior, without an HTTP server."""
import json
import os
import subprocess
import sys

import pytest

from app import cli
from app.config import ROOT


def test_retired_url_option_has_migration_error(capsys):
    assert cli.main(["--url", "http://localhost:8000", "prepare"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "REMOVED_OPTION"


def test_entry_script_help_does_not_prepare_database():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "b2b_leads.py"), "--help"], capture_output=True)
    assert result.returncode == 0
    assert b"delete-company" in result.stdout
    assert b"suppressions" in result.stdout


@pytest.mark.parametrize('arguments,expected_code,expected_text', [
    (['--help'], 0, '本机获客'),
    (['--url', 'http://localhost:8000', 'prepare'], 2, '已移除'),
])
def test_entry_script_emits_utf8_when_inherited_pipe_encoding_is_cp1252(arguments, expected_code, expected_text):
    env = {**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'}
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/b2b_leads.py'), *arguments],
                            capture_output=True, env=env, timeout=30)
    assert result.returncode == expected_code, result.stderr.decode('utf-8')
    output = result.stdout.decode('utf-8')
    assert expected_text in output
    if expected_code:
        assert json.loads(output)['error']['code'] == 'REMOVED_OPTION'


def test_controller_returns_json_and_preserves_run(monkeypatch, capsys):
    def dispatch(args):
        assert args.command == "status" and args.run == "example-run" and args.action == "pause"
        return {"run_id": args.run, "status": "paused"}
    monkeypatch.setattr(cli, "dispatch", dispatch)
    assert cli.main(["status", "--run", "example-run", "--action", "pause"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload.pop("versions")["core_version"]
    assert payload == {"run_id": "example-run", "status": "paused"}


def test_errors_do_not_print_exception_credentials(monkeypatch, capsys):
    def fail(args):
        raise RuntimeError("postgresql://private:secret@database/")
    monkeypatch.setattr(cli, "dispatch", fail)
    assert cli.main(["prepare"]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "private" not in output
    assert json.loads(output)["error"]["exception_type"] == "RuntimeError"


def test_invalid_json_reports_input_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(cli, "dispatch", lambda args: cli.read_json(args.spec_json))
    assert cli.main(["create", "--run", "sample", "--spec-json", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_INPUT"
