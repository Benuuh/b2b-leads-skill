"""Spec b2b-leads-2.6 §3.1 数据源适配器：候选池、信封、失败矩阵、降级。"""
import pytest

from app import discovery
from app.discovery import envelopes, service
from app.errors import AppError
from app.persistence import transaction
from app.persistence.models import Run


@pytest.fixture
def run_id(clean_db, packaging_spec):
    from app import runs
    runs.create('disc-run', packaging_spec)
    return 'disc-run'


@pytest.fixture
def fake_adapter(monkeypatch):
    """替换三个 adapter，不联网。"""
    calls = []

    def make(name, results=None, error=None):
        def adapter(query):
            calls.append((name, query))
            if error is not None:
                raise error
            return (results or [])[:max(1, int(query.get('limit', 50)))]
        return adapter

    good = [{'name': f'Buyer {index} GmbH', 'domain': None, 'country': 'DE', 'city': 'Köln',
             'source': 'eu_ted', 'source_ref': f'ted:{index}', 'raw': {'n': index}}
            for index in range(5)]
    monkeypatch.setattr(service.adapter_module, 'discover_eu_ted', make('eu_ted', good))
    monkeypatch.setattr(service.adapter_module, 'discover_eurostat_comext', make('eurostat_comext', []))
    monkeypatch.setattr(service.adapter_module, 'discover_google_places',
                        make('google_places', error=AppError('SOURCE_LIMIT', 'no key',
                                                             details={'source': 'google_places'})))
    return calls


def test_envelope_roundtrip_with_secret():
    ciphertext, sha256, mode = envelopes.seal({'raw': 'payload 数据', 'n': 1}, secret='unit-secret-key')
    assert mode == 'encrypted'
    opened = envelopes.open_envelope(ciphertext, secret='unit-secret-key')
    assert opened['status'] == 'ok' and opened['payload'] == {'raw': 'payload 数据', 'n': 1}
    assert opened['mode'] == 'encrypted'


def test_envelope_wrong_secret_returns_status_never_raises():
    ciphertext, sha256, _ = envelopes.seal({'raw': 'secret'}, secret='correct-key')
    result = envelopes.open_envelope(ciphertext, secret='wrong-key')
    assert result['status'] == 'undecryptable' and result['reason'] == 'key_mismatch'
    assert 'payload' not in result


def test_envelope_dev_plaintext_mode():
    ciphertext, sha256, mode = envelopes.seal({'raw': 'x'}, secret='')
    assert mode == 'plaintext'
    opened = envelopes.open_envelope(ciphertext)
    assert opened['status'] == 'ok' and opened['payload'] == {'raw': 'x'}


def test_discover_stores_unverified_pool(run_id, fake_adapter):
    result = service.discover(run_id, 'eu_ted', limit=10)
    assert result['stored'] == 5 and result['failure'] is None
    listing = service.list_candidates(run_id, status='pending')
    assert listing['count'] == 5
    assert all(item['status'] == 'pending' for item in listing['items'])
    assert '不进合格计数' in listing['note']
    # 信封已落库
    with transaction() as session:
        from app.persistence.models import ProspectSourceEnvelope
        assert len(list(session.scalars(__import__('sqlalchemy').select(
            ProspectSourceEnvelope))) ) == 5


def test_discover_is_idempotent_per_source_ref(run_id, fake_adapter):
    service.discover(run_id, 'eu_ted', limit=10)
    again = service.discover(run_id, 'eu_ted', limit=10)
    assert again['stored'] == 0


def test_discover_empty_source_is_not_failure(run_id, fake_adapter):
    result = service.discover(run_id, 'eurostat_comext')
    assert result['stored'] == 0
    assert result['failure']['code'] == 'source_empty'


def test_discover_paid_limit_stops_source_without_retry(run_id, fake_adapter):
    result = service.discover(run_id, 'google_places')
    assert result['failure']['code'] == 'source_limit'


def test_all_sources_fail_marks_partial_no_source(run_id, monkeypatch):
    def broken(query):
        raise ConnectionError('network down')
    for name in service.adapter_module.SOURCES:
        monkeypatch.setattr(service.adapter_module, name.replace('eu_ted', 'discover_eu_ted')
                            .replace('eurostat_comext', 'discover_eurostat_comext')
                            .replace('google_places', 'discover_google_places'), broken)
    result = service.discover_all(run_id, limit=5)
    assert result['partial_no_source'] is True and result['stored'] == 0
    with transaction() as session:
        stats = session.get(Run, run_id).stats_json or {}
    assert stats['partial_no_source']['note'] == '本轮没有可用数据源；保留已有候选，暂停 discover'


def test_reject_requires_reason(run_id, fake_adapter):
    service.discover(run_id, 'eu_ted', limit=3)
    candidate_id = service.list_candidates(run_id)['items'][0]['id']
    with pytest.raises(AppError) as error:
        service.reject_candidate(run_id, candidate_id, '   ')
    assert 'reason 必填' in error.value.message
    result = service.reject_candidate(run_id, candidate_id, '买方是公共机构，不是目标客户')
    assert result['status'] == 'rejected'


def test_explain_requires_confirmation(run_id, fake_adapter):
    service.discover(run_id, 'eu_ted', limit=2)
    candidate_id = service.list_candidates(run_id)['items'][0]['id']
    with pytest.raises(AppError) as error:
        service.explain_envelope(run_id, candidate_id, confirmed=False)
    assert error.value.code == 'CONFIRMATION_REQUIRED'


def test_explain_undecryptable_keeps_envelope(run_id, fake_adapter, monkeypatch):
    monkeypatch.setenv('PROSPECT_SOURCE_RAW_ENVELOPE_SECRET', 'key-a')
    service.discover(run_id, 'eu_ted', limit=1)
    candidate_id = service.list_candidates(run_id)['items'][0]['id']
    monkeypatch.setenv('PROSPECT_SOURCE_RAW_ENVELOPE_SECRET', 'key-b')  # 密钥换了
    result = service.explain_envelope(run_id, candidate_id, confirmed=True)
    assert result['status'] == 'undecryptable' and result['reason'] == 'key_mismatch'
    # 原密文保留：数据库里的信封未被删除或改写。
    with transaction() as session:
        from app.persistence.models import ProspectSourceEnvelope
        from sqlalchemy import select
        envelope = session.scalars(select(ProspectSourceEnvelope)).first()
        assert envelope.envelope_sha256 == result['envelope_sha256']


def test_legacy_mode(run_id):
    result = service.enable_legacy(run_id)
    assert result['mode'] == 'legacy'
    with transaction() as session:
        stats = session.get(Run, run_id).stats_json or {}
    assert stats['mode'] == 'legacy' and '人工搜索' in stats['legacy_note']


def test_sources_status_offline():
    status = service.sources_status()
    assert set(('eu_ted', 'eurostat_comext', 'google_places')) <= set(status)
    assert status['google_places']['credential'] == 'PROVIDER_CREDENTIAL_KEY'


def test_discover_unknown_source_rejected(run_id):
    with pytest.raises(AppError):
        service.discover(run_id, 'not_a_source')


def test_discover_unknown_run_rejected(clean_db):
    with pytest.raises(AppError) as error:
        service.discover('missing-run', 'eu_ted')
    assert error.value.code == 'RUN_NOT_FOUND'
