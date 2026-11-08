"""M0/M4 diagnostics: bench timeout, envelope linear keystream, policy cache stats."""
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bench_timeout_is_a_real_per_company_deadline(monkeypatch, tmp_path):
    bench = _load('bench_script', ROOT / 'scripts' / 'bench.py')
    seen = []

    def fake_fetch(candidate, segment, *, max_pages, stage, **kwargs):
        from app.web.http import remaining_seconds
        seen.append(remaining_seconds())
        return [{'fetch_status': 'ok'}]

    monkeypatch.setattr('app.web.crawler.fetch_company', fake_fetch)
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text('[{"company_id":"one","domain":"example.com","company_name":"One"}]',
                        encoding='utf-8')
    out = tmp_path / 'out.json'
    result = bench.run_benchmark(snapshot, repeats=1, out_path=out, max_pages=1,
                                 timeout_seconds=5, workers=1)
    assert seen and all(value is not None and 0 < value <= 5.01 for value in seen)
    assert result['summary'][0]['physical_http_attempts'] == 0


def test_bench_counts_failed_pages_separately_from_exceptions(monkeypatch, tmp_path):
    bench = _load('bench_script_failed', ROOT / 'scripts' / 'bench.py')

    def fake_fetch(candidate, segment, *, max_pages, stage, **kwargs):
        return [{'fetch_status': 'network_timeout'}]

    monkeypatch.setattr('app.web.crawler.fetch_company', fake_fetch)
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text('[{"company_id":"one","domain":"example.com","company_name":"One"}]',
                        encoding='utf-8')
    out = tmp_path / 'out.json'
    result = bench.run_benchmark(snapshot, repeats=1, out_path=out, max_pages=1,
                                 timeout_seconds=5, workers=1)
    summary = result['summary'][0]
    assert summary['failures'] == 0 and summary['failed_pages'] == 1


def test_envelope_keystream_large_payload_roundtrip():
    from app.discovery import envelopes
    payload = {'raw': 'x' * (1024 * 1024), 'n': 1}
    ciphertext, digest, mode = envelopes.seal(payload, secret='large-payload-key')
    assert mode == 'encrypted'
    opened = envelopes.open_envelope(ciphertext, secret='large-payload-key')
    assert opened['status'] == 'ok' and opened['payload'] == payload


def test_policy_cache_reports_hits_and_fingerprint_time():
    from app.rules import policy
    from app.rules.policy import assess_policy_cached, policy_cache_stats, reset_policy_cache_stats
    reset_policy_cache_stats()
    spec = {'spec_version': 2, 'qualification_policy': {'required_all': [], 'exclude_any': []},
            'freshness_days': 180}
    candidate = {'company_name': 'Cache Co', 'website_url': 'https://cache.example.com',
                 'country_code': 'US', 'source_url': 'https://cache.example.com/',
                 'source_excerpt': 'Cache Co', 'retrieved_at': '2026-11-01T00:00:00+00:00'}
    assess_policy_cached(candidate, [], spec)
    assess_policy_cached(candidate, [], spec)
    stats = policy_cache_stats()
    assert stats['hits'] == 1 and stats['misses'] == 1
    assert stats['fingerprints'] == 2 and stats['fingerprint_seconds'] >= 0
    assert stats['hit_rate'] == 0.5
    reset_policy_cache_stats()


def test_discovery_list_is_paginated(clean_db, packaging_spec, monkeypatch):
    from app import runs
    from app.discovery import service
    runs.create('paginate', packaging_spec)

    def adapter(query):
        return [{'name': f'Buyer {index}', 'domain': None, 'country': 'DE', 'city': None,
                 'source': 'eu_ted', 'source_ref': f'ted:{index}', 'raw': {'n': index}}
                for index in range(5)]

    monkeypatch.setattr(service.adapter_module, 'discover_eu_ted', adapter)
    service.discover('paginate', 'eu_ted', limit=5)
    first = service.list_candidates('paginate', limit=2)
    assert first['count'] == 2 and first['has_more'] is True and first['cursor'] == '2'
    second = service.list_candidates('paginate', limit=2, cursor=first['cursor'])
    assert second['count'] == 2 and second['has_more'] is True
    third = service.list_candidates('paginate', limit=2, cursor=second['cursor'])
    assert third['count'] == 1 and third['has_more'] is False and third['cursor'] is None
