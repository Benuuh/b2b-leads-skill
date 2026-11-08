"""Reproducible controlled benchmark for the live page, projection and export.

This measures the local projection/export path only. It uses synthetic evidence
inside an isolated database and forbids every external socket call. Results say
nothing about real-world first-customer or full-batch time: those need an
explicit real task and are reported separately when available.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import statistics
import sys
import tempfile
import time

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def candidate(index, at):
    name = 'Benchmark Packaging %d GmbH' % index
    website = 'https://benchmark-packaging-%d.de/' % index
    source = 'https://packaging-association.org/members/benchmark-%d' % index
    email = 'sales@benchmark-packaging-%d.de' % index
    fields = [
        ('company_name', name, '%s, Germany' % name),
        ('website_url', website, '%s; official website %s' % (name, website)),
        ('entity.headquarters_country', 'DE', '%s, headquartered in Germany' % name),
        ('business.models', 'manufacturer', '%s manufactures reusable industrial packaging' % name),
        ('offering.products', 'reusable industrial packaging', '%s manufactures reusable industrial packaging' % name),
        ('email', email, '%s — Business email: %s' % (name, email)),
    ]
    return {
        'company_name': name, 'website_url': website, 'country_code': 'DE',
        'headquarters_country': 'DE', 'business_models': ['manufacturer'],
        'offering_products': ['reusable industrial packaging'], 'industry_text': 'industrial packaging',
        'source_url': source, 'source_type': 'directory', 'retrieved_at': at, 'email': email,
        'source_excerpt': '%s, Germany, manufactures reusable industrial packaging.' % name,
        'sources': [{'field': field, 'value': value, 'source_url': source, 'source_type': 'directory',
                     'retrieved_at': at, 'method': 'benchmark_page_observation', 'source_excerpt': excerpt}
                    for field, value, excerpt in fields],
    }


def spec(quantity):
    return {
        'spec_version': 2, 'name': 'Synthetic live page benchmark', 'industry': 'industrial packaging',
        'countries': ['DE'], 'quantity': max(50, quantity), 'paid_sources': False, 'budget': 0,
        'qualification_policy': {
            'country_scope': 'headquarters',
            'required_all': [
                {'id': 'hq', 'field': 'entity.headquarters_country', 'operator': 'in', 'value': ['DE']},
                {'id': 'maker', 'field': 'business.models', 'operator': 'includes', 'value': 'manufacturer'},
                {'id': 'product', 'field': 'offering.products', 'operator': 'provides',
                 'value': 'reusable industrial packaging'}],
            'exclude_any': [],
            'contact_policy': {'mode': 'any',
                               'allowed_types': ['business_email', 'business_phone', 'contact_form'],
                               'named_person_required': False}},
        'evidence_policy': {'freshness_days': 180},
        'execution_policy': {'company_workers': 2, 'native_inflight': 2,
                             'snapshot_every_new_qualified': 10, 'snapshot_interval_seconds': 900},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', required=True, type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--batches', type=int, default=3)
    parser.add_argument('--per-batch', type=int, default=5)
    parser.add_argument('--poll-seconds', type=float, default=2.0,
                        help='Page polling interval used to report worst-case visibility')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.backend.resolve()))

    from app.config import settings
    from app import persistence, runs
    from app.live import projection as live

    url = make_url(settings.database_url)
    name = 'sca_bench_live_' + secrets.token_hex(4)
    admin = url.set(drivername='postgresql', database='postgres')
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    settings.database_url = url.set(database=name).render_as_string(hide_password=False)
    settings.fixture_mode = True
    settings.artifact_root = Path(tempfile.mkdtemp(prefix='sca-bench-live-'))
    persistence.reset_engine()
    persistence.initialize()

    # A benchmark must fail loudly instead of touching a real website.
    import app.web.crawler as crawler
    import app.web.dns as dns

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Benchmark attempted external access')

    crawler.safe_http = forbidden
    dns.lookup_mx = lambda _host: 'unknown'

    total = args.batches * args.per_batch
    at = datetime.now(timezone.utc).isoformat()
    runs.create('bench-live', spec(total))
    from app.persistence import transaction
    from app.persistence.models import Run
    with transaction() as session:
        session.get(Run, 'bench-live').demo_only = False

    batches, ready_seconds = [], []
    started_all = time.perf_counter()
    for number in range(args.batches):
        values = [candidate(number * args.per_batch + index, at) for index in range(args.per_batch)]
        runs.append('bench-live', values, chunk_id='batch-%d' % number)
        started = time.perf_counter()
        result = runs.process('bench-live', max_jobs=args.per_batch + 2, max_seconds=120, workers=1)
        process_seconds = time.perf_counter() - started
        ready = time.perf_counter()
        expected = (number + 1) * args.per_batch
        while time.perf_counter() - ready < 10:
            page = live.read_projection('bench-live') or {}
            if (page.get('counts') or {}).get('deliverable') == expected:
                break
            time.sleep(0.05)
        ready_seconds.append(time.perf_counter() - ready)
        batches.append({'batch': number + 1, 'companies': expected, 'process_seconds': process_seconds,
                        'projection_ready_seconds': ready_seconds[-1],
                        'deliverable': (live.read_projection('bench-live') or {}).get('counts', {}).get('deliverable'),
                        'contactable': result.get('summary', {}).get('contactable')})
    total_seconds = time.perf_counter() - started_all

    builds = []
    for _ in range(5):
        mark = time.perf_counter()
        page = live.build_projection('bench-live')
        builds.append(time.perf_counter() - mark)
    write_mark = time.perf_counter()
    live.refresh('bench-live', force=True)
    write_seconds = time.perf_counter() - write_mark
    payload_bytes = live.projection_path('bench-live').stat().st_size

    export_mark = time.perf_counter()
    exported = runs.export('bench-live')
    export_seconds = time.perf_counter() - export_mark

    delivered = page['deliverables']
    quality = {
        'deliverable': len(delivered),
        'with_sources': sum(bool(item['sources']) for item in delivered),
        'with_channel': sum(bool(item['channels']) for item in delivered),
        'with_match_reason': sum(bool(item['match_reasons']) for item in delivered),
        'pending': len(page['pending']),
        'consistency': page['consistency'],
    }
    report = {
        'synthetic_only': True,
        'external_requests': 0,
        'note': 'Controlled fixture benchmark of projection/export; not a real research baseline.',
        'batches': args.batches, 'per_batch_size': args.per_batch, 'companies': total,
        'first_batch_seconds': batches[0]['process_seconds'] if batches else None,
        'full_batch_seconds': total_seconds,
        'per_batch': batches,
        'projection_ready_p50_seconds': statistics.median(ready_seconds) if ready_seconds else None,
        'projection_ready_p95_seconds': sorted(ready_seconds)[-1] if ready_seconds else None,
        'page_worst_case_visible_seconds': (max(ready_seconds) + args.poll_seconds) if ready_seconds else None,
        'projection_build_p50_seconds': statistics.median(builds),
        'projection_build_p95_seconds': sorted(builds)[-1],
        'projection_write_seconds': write_seconds,
        'payload_bytes': payload_bytes,
        'export_seconds': export_seconds,
        'export_rows': exported['manifest']['row_counts']['companies'],
        'quality': quality,
        'release': {'core_version': __import__('app').__version__,
                    'schema_revision': persistence.SCHEMA_REVISION},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    persistence.reset_engine()
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as connection:
        connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
    print(json.dumps({key: value for key, value in report.items() if key != 'per_batch'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
