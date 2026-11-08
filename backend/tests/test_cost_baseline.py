"""Cost reporting must preserve unknowns and avoid inventing unit costs."""
import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

path = Path(__file__).resolve().parents[2] / 'scripts' / 'cost_baseline.py'
spec = importlib.util.spec_from_file_location('cost_baseline', path)
baseline = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = baseline
spec.loader.exec_module(baseline)


def measured(value, scope='whole run', source='provider'):
    return {'value': value, 'scope': scope, 'source': source,
            'missing_reason': None if value is not None else 'unavailable'}


def report():
    return {'report_version': 'cost-baseline-1', 'run_id': 'example', 'release': 'development',
            'spec_version': '2', 'rule_version': 'b2b-policy-2', 'metrics_version': '1',
            'config_hash': 'a' * 64, 'sample_kind': 'synthetic', 'sample_description': 'test',
            'environment': 'test', 'tools': ['test'], 'started_at': 'start', 'ended_at': 'end',
            'wall_seconds': measured(60), 'active_seconds': measured(30), 'paused_seconds': measured(30),
            'first_delivery_seconds': measured(None), 'first_five_seconds': measured(None),
            'delivery_milestones': [], 'candidates_deduplicated': 0, 'admitted': 0, 'deliverable': 0,
            'failures': {}, 'missing_fields': {}, 'stages_seconds': {}, 'calls': {},
            'returned_characters': measured(100), 'agent_turns': measured(1),
            'tokens': {'input': measured(None), 'output': measured(None), 'cache': measured(None),
                       'cache_is_input_subset': None, 'billing_basis': 'unavailable'}}


def test_unknown_tokens_and_zero_delivery_are_not_estimated():
    result = baseline.summarize(baseline.Report.model_validate(report()))
    assert result['deliverable_per_active_hour'] == 0
    assert result['input_plus_output_tokens_per_deliverable'] is None
    assert result['g0_passed'] is False


def test_cache_not_added_and_scope_mismatch_not_divided():
    data = report()
    data.update(candidates_deduplicated=5, admitted=5, deliverable=5, first_five_seconds=measured(20))
    data['tokens'].update(input=measured(100), output=measured(50), cache=measured(40), cache_is_input_subset=True)
    assert baseline.summarize(baseline.Report.model_validate(data))['input_plus_output_tokens_per_deliverable'] == 30
    data['tokens']['output'] = measured(50, scope='one call')
    assert baseline.summarize(baseline.Report.model_validate(data))['input_plus_output_tokens_per_deliverable'] is None
    data['tokens']['output'] = measured(50)
    data['tokens']['cache_is_input_subset'] = False
    assert baseline.summarize(baseline.Report.model_validate(data))['input_plus_output_tokens_per_deliverable'] is None


def test_invalid_funnel_missing_reason_and_sensitive_extra_rejected():
    data = report()
    data['deliverable'] = 1
    with pytest.raises(ValidationError):
        baseline.Report.model_validate(data)
    data = report()
    data['tokens']['input']['missing_reason'] = None
    with pytest.raises(ValidationError):
        baseline.Report.model_validate(data)
    data = report()
    data['exclusions'] = ['private']
    with pytest.raises(ValidationError):
        baseline.Report.model_validate(data)
