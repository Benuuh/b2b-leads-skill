"""The staged converter is a first-class entrypoint, not a side script."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import compare_stage_inputs as comparison  # noqa: E402
from app import cli  # noqa: E402


def _files(tmp_path):
    case = comparison.case_valid()
    spec = tmp_path / 'spec.json'
    artifact = tmp_path / 'artifact.json'
    spec.write_text(json.dumps(comparison.SPEC), encoding='utf-8')
    artifact.write_text(json.dumps(case['payload']), encoding='utf-8')
    return spec, artifact, case


def test_stage_draft_command_is_offline_and_writes_the_subset(tmp_path, capsys):
    spec, artifact, case = _files(tmp_path)
    out = tmp_path / 'subset.json'
    code = cli.main(['stage-draft', '--spec-json', str(spec), '--artifact-json', str(artifact),
                     '--candidate-ids', 'a', '--as-of', comparison.AS_OF, '--out', str(out)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['submittable'] == 1
    assert payload['errors'] == []
    assert payload['versions']['core_version']
    written = json.loads(out.read_text(encoding='utf-8'))
    assert written['candidates'][0]['company_name'] == case['manual']['a']['company_name']


def test_advance_accepts_an_artifact_and_reuses_the_runner(tmp_path, monkeypatch):
    spec, artifact, case = _files(tmp_path)
    seen = {}
    monkeypatch.setattr('app.runtime.prepare', lambda: {})
    monkeypatch.setattr('app.runs.maintenance', lambda **kwargs: None)
    monkeypatch.setattr('app.runs.status', lambda run, action=None: {'spec': comparison.SPEC})
    monkeypatch.setattr('app.runs.advance', lambda run, payload, **kwargs: seen.update(
        payload=payload, kwargs=kwargs) or {'run_id': run, 'summary': {}})
    args = cli.parser().parse_args(['advance', '--run', 'stage-run',
                                    '--artifact-json', str(artifact),
                                    '--candidate-ids', 'a', '--as-of', comparison.AS_OF])
    result = cli.dispatch(args)
    assert result['staging']['submittable'] == 1
    assert seen['kwargs']['chunk_id'].startswith('stage-')
    assert seen['payload']['candidates'][0] == case['manual']['a']
