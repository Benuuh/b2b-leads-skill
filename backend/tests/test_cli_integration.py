"""Run the installed-style CLI end to end, without API, workers or UI."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from app.config import ROOT, settings
from app import __version__


def test_direct_commands_resume_and_export_chinese_excel(clean_db, tmp_path):
    env = {**os.environ, "SCA_DATABASE_URL": settings.database_url,
           "SCA_ARTIFACT_ROOT": str(settings.artifact_root), "SCA_FIXTURE_MODE": "false"}
    def command(*args):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "b2b_leads.py"), *args],
                                env=env, cwd=ROOT, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout.decode("utf-8", errors="replace")
        return json.loads(result.stdout)
    def save(name, body):
        path = tmp_path / name
        path.write_text(json.dumps(body), encoding="utf-8")
        return str(path)
    now = datetime.now(timezone.utc).isoformat()
    url = "https://directory.example.org/member/synthetic-company"
    candidate = {"company_name": "Synthetic CLI freight fixture", "country_code": "US",
        "industry_text": "freight forwarding", "source_url": url, "source_type": "directory",
        "source_excerpt": "Synthetic CLI freight fixture provides freight forwarding services in the US; +1 213 555 0123",
        "retrieved_at": now, "phone": "+12135550123", "sources": [
            {"field": "company_name", "value": "Synthetic CLI freight fixture", "source_url": url,
             "source_type": "directory", "source_excerpt": "Synthetic CLI freight fixture", "retrieved_at": now},
            {"field": "phone", "value": "+12135550123", "source_url": url,
             "source_type": "directory", "source_excerpt": "Business phone +1 213 555 0123", "retrieved_at": now}]}
    assert command("prepare")["api_required"] is False
    from app.segment_brief import inspect_segment
    spec_body = {"spec_version": 1, "name": "CLI fixture", "industry": "freight forwarding", "countries": ["US"], "quantity": 200}
    spec = save("spec.json", spec_body)
    assert command("create", "--run", "cli-fixture", "--spec-json", spec,
                   "--confirm-hash", inspect_segment(spec_body)["confirm_hash"])["status"] == "running"
    body = save("chunk.json", {"candidates": [candidate], "checkpoint": {"page": 1}})
    first = command("append", "--run", "cli-fixture", "--chunk", "round1", "--input-json", body)
    assert command("append", "--run", "cli-fixture", "--chunk", "round1", "--input-json", body) == first
    assert command("status", "--run", "cli-fixture", "--action", "pause")["status"] == "paused"
    processed = command("process", "--run", "cli-fixture", "--resume", "--operation-key", "process-round1")
    assert processed["processed_jobs"] == 1
    assert command("finish", "--run", "cli-fixture")["status"] == "partial"
    status = command("status", "--run", "cli-fixture")
    assert status["summary"]["contactable"] == 1
    result = command("export", "--run", "cli-fixture")
    assert result["summary"]["contactable"] == 1
    assert result["summary"]["target_shortfall"] == 199
    assert Path(result["file"]).is_file() and Path(result["report_file"]).is_file()
    from openpyxl import load_workbook
    workbook = load_workbook(result["file"])
    assert workbook.sheetnames == ["Companies", "Contacts", "Sources", "Manifest"]
    assert "企业名称" in next(workbook["Companies"].values)
    assert "来源原文摘录" in next(workbook["Sources"].values)
    assert result['manifest']['runtime_release']['core_version'] == __version__
    assert command("list")["items"][0]["run_id"] == "cli-fixture"
    suppression = command("suppressions", "add", "--type", "company", "--value", candidate["company_name"])
    assert command("status", "--run", "cli-fixture")["summary"]["contactable"] == 0
    assert not Path(result["file"]).exists()
    command("suppressions", "remove", "--id", suppression["id"])
    refreshed = command("export", "--run", "cli-fixture", "--format", "csv")
    assert refreshed["summary"]["contactable"] == 1
    command("maintenance")


def test_advance_command_keeps_chunk_and_processing_replay(
        clean_db, tmp_path, packaging_spec, packaging_candidate):
    env = {**os.environ, 'SCA_DATABASE_URL': settings.database_url,
           'SCA_ARTIFACT_ROOT': str(settings.artifact_root), 'SCA_FIXTURE_MODE': 'false'}
    def command(*args):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/b2b_leads.py'), *args],
                                env=env, cwd=ROOT, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout.decode('utf-8', errors='replace')
        return json.loads(result.stdout)
    spec = tmp_path / 'spec.json'
    chunk = tmp_path / 'chunk.json'
    from app.segment_brief import inspect_segment
    spec.write_text(json.dumps(packaging_spec), encoding='utf-8')
    chunk.write_text(json.dumps({'candidates': [packaging_candidate]}), encoding='utf-8')
    command('create', '--run', 'cli-advance', '--spec-json', str(spec),
            '--confirm-hash', inspect_segment(packaging_spec)['confirm_hash'])
    first = command('advance', '--run', 'cli-advance', '--chunk', 'first', '--input-json', str(chunk))
    assert first['advanced'] and first['summary']['contactable'] == 1
    assert first['admission']['accepted'] == first['processed_jobs'] == 1
    assert 'segment' not in first and 'plan' not in first
    assert command('advance', '--run', 'cli-advance', '--chunk', 'first', '--input-json', str(chunk)) == first
