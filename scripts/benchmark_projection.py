"""Fixed-fixture A/B benchmark for the run projection and export path.

The old and new trees must be passed explicitly so the same script can measure
the frozen 2.6.0 baseline and the candidate implementation without copying
results by hand:

    python scripts/benchmark_projection.py --app-root .tmp/baseline-2.6.0/backend \
        --scenario append --cohort 600 --repeats 5 --out bench/old-append-600.json

Each invocation creates an isolated PostgreSQL database (``sca_bench_*``), seeds
the same deterministic cohort, and drops the database afterwards. The report
contains raw per-round timings, p50/p95, SQL statement counts, selection call
counts and peak Python allocation (tracemalloc) plus process RSS.
"""
import argparse
import ctypes
import gc
import hashlib
import json
import os
import secrets
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _base_url():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    return os.getenv('SCA_DATABASE_URL') or os.getenv('DATABASE_URL')


def _admin_and_name(base_url):
    from sqlalchemy.engine import make_url
    url = make_url(base_url)
    admin = url.set(drivername='postgresql', database='postgres')
    return admin.render_as_string(hide_password=False), 'sca_bench_' + secrets.token_hex(6)


def _create_database(admin_dsn, name):
    import psycopg
    from psycopg import sql
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))


def _drop_database(admin_dsn, name):
    import psycopg
    from psycopg import sql
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
    except Exception:
        pass


def _rss_bytes():
    if os.name != 'nt':
        try:
            import resource
            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
        except Exception:
            return None

    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    try:
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return int(counters.PeakWorkingSetSize)
    except Exception:
        return None


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _truncate(metadata):
    from sqlalchemy import text
    from app.persistence import transaction
    names = [name for name in metadata.tables if 'schema' not in name]
    with transaction() as session:
        session.execute(text('TRUNCATE ' + ', '.join('"' + name + '"' for name in names) + ' CASCADE'))


def _seed(runs, models, run_id, cohort, spec):
    """Seed a deterministic run and cohort; avoids 20 prechecks per repeat."""
    from app.persistence import transaction, utcnow
    from datetime import datetime, timedelta, timezone
    from app.segment_brief import inspect_segment
    runs.create(run_id, spec, confirm_hash=inspect_segment(spec)['confirm_hash'])
    # Deterministic fixture time: old/new A/B must see byte-identical rows.
    now = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
    with transaction() as session:
        run = session.get(models.Run, run_id)
        run.demo_only = False
        run.plan_json = {**run.plan_json, 'research': {**run.plan_json['research'],
            'admitted_count': cohort}}
        suppression_version = hashlib.sha256(json.dumps(
            [], sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        run.stats_json = {**(run.stats_json or {}),
            'projection': {'version': run.version,
                           'as_of': datetime.now(timezone.utc).isoformat(),
                           'suppression_version': suppression_version,
                           'contactable': cohort, 'qualified': cohort},
            'counters': {'qualified': cohort, 'contactable': cohort, 'excluded': 0,
                         'uncertain': 0, 'missing': 0, 'queued': 0, 'running': 0,
                         'processed': cohort, 'failed': 0}}
        for index in range(cohort):
            company_id = f'bench-c-{index:05d}'
            domain = f'bench-{index}.example.com'
            phone = f'+1213555{index:04d}'
            name = f'Bench Company {index}'
            retrieved = (now - timedelta(days=1)).isoformat()
            source = {'entity_id': company_id + '-contact', 'field': 'phone', 'value': phone,
                      'source_url': f'https://{domain}/contact', 'source_type': 'official_site',
                      'retrieved_at': retrieved, 'method': 'extracted', 'confidence': .9,
                      'source_excerpt': f'{name} contact {phone}'}
            website_source = {'entity_id': company_id, 'field': 'website_url',
                              'value': f'https://{domain}', 'source_url': f'https://{domain}/',
                              'source_type': 'official_site', 'retrieved_at': retrieved,
                              'method': 'agent_page_observation',
                              'source_excerpt': f'{name} official website https://{domain}'}
            data = {'company_id': company_id, 'company_name': name,
                    'website_url': f'https://{domain}', 'domain': domain,
                    'country_code': 'US', 'industry_text': 'freight forwarding',
                    'status': 'qualified', 'website_binding_status': 'confirmed',
                    'website_binding_evidence': [{'source_url': f'https://{domain}/',
                        'retrieved_at': retrieved, 'method': 'benchmark_fixture'}],
                    'qualification': {'decision': 'qualified', 'confidence': .9, 'reasons': []},
                    'contacts': [{'contact_id': company_id + '-contact', 'company_id': company_id,
                        'phone': phone, 'phone_status': 'valid', 'phone_type': 'company_main',
                        'binding_status': 'company', 'retrieved_at': retrieved,
                        'source_urls': [f'https://{domain}/contact'], 'inferred': False,
                        'tags': ['public_business'], 'confidence': .9}],
                    'sources': [source, website_source], 'conflicts': [], 'missing': [],
                    'demo_only': False}
            created = now - timedelta(seconds=cohort - index)
            session.add(models.Company(id=company_id, run_id=run_id,
                identity_key=company_id, data_json=data,
                expires_at=now + timedelta(days=365), created_at=created, updated_at=created))
            session.add(models.Job(id=f'bench-j-{index:05d}', run_id=run_id,
                entity_key=company_id, input_json={'company_id': company_id},
                output_json={'company_id': company_id, 'pages_used': 0}, state='completed',
                attempt=1, available_at=created, created_at=created, updated_at=created))
    return now


def _seed_queued(models, run_id, count, start):
    from app.persistence import transaction, utcnow
    now = utcnow()
    with transaction() as session:
        for index in range(count):
            company_id = f'bench-new-{index:04d}'
            session.add(models.Job(id=f'bench-q-{index:04d}', run_id=run_id,
                entity_key=company_id, input_json={'company_id': company_id,
                    'company_name': f'Queued {index}', 'country_code': 'US',
                    'website_url': f'https://queued-{index}.example.com',
                    'source_url': 'https://directory.example.org/queued',
                    'source_excerpt': f'Queued {index} freight forwarder',
                    'retrieved_at': now.isoformat()},
                output_json={}, state='queued', attempt=0, available_at=now,
                created_at=now + timedelta(microseconds=index), updated_at=now))


def _start_sql_counter(engine):
    from sqlalchemy import event
    calls = []

    def before(conn, cursor, statement, parameters, context, executemany):
        calls.append(statement)

    event.listen(engine, 'before_cursor_execute', before)
    return calls


def _instrument_selection():
    from app.delivery import selection
    counters = Counter()

    def wrap(name):
        original = getattr(selection, name, None)
        if original is None:
            return
        def counted(*args, **kwargs):
            counters[name] += 1
            return original(*args, **kwargs)
        setattr(selection, name, counted)

    for name in ('coalesce_companies', 'filter_companies', 'delivery_companies',
                 'channel_status', 'select_export_companies', 'prepare_cohort',
                 '_prepare_cohort', '_deliver_selected'):
        wrap(name)
    return counters


def _run_scenario(module, scenario, cohort, repeat_index, counters, sql_calls, jobs=None, workers=None):
    from app import runs
    import app.pipeline as pipeline
    from app.persistence import transaction, utcnow, models
    run_id = 'bench-' + scenario + '-' + secrets.token_hex(3)
    spec = {'spec_version': 2, 'name': 'Bench ' + scenario, 'industry': 'freight forwarding',
            'countries': ['US'], 'quantity': max(200, min(1000, cohort + (20 if scenario == 'append' else 0))),
            'qualification_policy': {'country_scope': 'headquarters', 'required_all': [],
                'exclude_any': [], 'contact_policy': {'mode': 'any',
                    'allowed_types': ['business_email', 'business_phone', 'contact_form'],
                    'named_person_required': False}},
            'evidence_policy': {'freshness_days': 180},
            'execution_policy': {'company_workers': 2, 'snapshot_every_new_qualified': 0,
                'snapshot_interval_seconds': 60}}
    _seed(runs, models, run_id, cohort, spec)
    if scenario == 'append':
        now = datetime.now(timezone.utc).isoformat()
        candidates = [{'company_name': f'Append {index}', 'website_url': f'https://append-{index}.example.com',
                       'country_code': 'US', 'source_url': f'https://directory.example.org/append-{index}',
                       'source_excerpt': f'Append {index} freight forwarder', 'retrieved_at': now}
                      for index in range(20)]
        return lambda: runs.append(run_id, candidates, chunk_id='bench-chunk-' + secrets.token_hex(3))
    if scenario == 'status':
        return lambda: runs.status(run_id)
    if scenario == 'export':
        return lambda: runs.export(run_id, format='xlsx')
    if scenario in {'process', 'complete'}:
        _seed_queued(models, run_id, 20, datetime.now(timezone.utc))
        def fake(task, pages):
            company_id = task['input']['company_id']
            return {'company_id': company_id, 'company_name': task['input']['company_name'],
                    'domain': 'queued.example.com', 'country_code': 'US', 'status': 'qualified',
                    'qualification': {'decision': 'qualified', 'confidence': .9, 'reasons': []},
                    'contacts': [], 'sources': [], 'demo_only': False,
                    'pipeline': {'stop_reason': 'benchmark_fake'}}
        import app.pipeline
        app.pipeline.process_company = fake
        limit = 1 if scenario == 'complete' else (jobs or 20)
        # One worker isolates the completion cost; concurrent claims against a
        # slow authoritative refresh would measure lock waiting, not projection.
        chosen = 1 if scenario == 'complete' else (workers or 2)
        return lambda: runs.process(run_id, max_jobs=limit, workers=chosen)
    raise SystemExit('unknown scenario: ' + scenario)


def _measure(module, scenario, cohort, repeats, metadata, jobs=None, workers=None,
             trace_memory=False):
    counters = _instrument_selection()
    from app.persistence import engine as persistence_engine
    sql_calls = _start_sql_counter(persistence_engine())
    rounds = []
    for index in range(repeats):
        _truncate(metadata)
        work = _run_scenario(module, scenario, cohort, index, counters, sql_calls, jobs, workers)
        counters.clear()
        sql_start = len(sql_calls)
        gc.collect()
        if trace_memory:
            tracemalloc.start()
        started = time.perf_counter()
        result = work()
        elapsed = time.perf_counter() - started
        peak = None
        if trace_memory:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        rounds.append({'repeat': index + 1, 'seconds': round(elapsed, 6),
                       'sql_calls': len(sql_calls) - sql_start,
                       'selection_calls': dict(counters), 'tracemalloc_peak': peak,
                       'rss_peak': _rss_bytes(), 'result_keys': sorted(result) if isinstance(result, dict) else None})
    seconds = [item['seconds'] for item in rounds]
    return {'scenario': scenario, 'cohort': cohort, 'repeats': repeats,
            'p50_seconds': statistics.median(seconds), 'p95_seconds': _percentile(seconds, 0.95),
            'mean_seconds': statistics.mean(seconds),
            'stdev_seconds': statistics.pstdev(seconds) if len(seconds) > 1 else 0.0,
            'peak_rss': max((item['rss_peak'] or 0) for item in rounds) or None,
            'tracemalloc_peak': max((item['tracemalloc_peak'] for item in rounds
                                     if item['tracemalloc_peak'] is not None), default=None),
            'sql_calls_p50': statistics.median([item['sql_calls'] for item in rounds]),
            'selection_calls_last': rounds[-1]['selection_calls'],
            'rounds': rounds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', required=True, help='backend directory containing app/')
    parser.add_argument('--scenario', required=True,
                        choices=('append', 'status', 'export', 'process', 'complete'))
    parser.add_argument('--cohort', type=int, nargs='+', default=[600])
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--jobs', type=int, default=None,
                        help='process scenario only; default 20 (complete is always 1)')
    parser.add_argument('--workers', type=int, default=None,
                        help='process scenario only; default 2 (complete is always 1)')
    parser.add_argument('--trace-memory', action='store_true',
                        help='also start tracemalloc (distorts wall time; use a separate run)')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()

    base_url = _base_url()
    if not base_url:
        raise SystemExit('Set SCA_DATABASE_URL (or DATABASE_URL) for the isolated benchmark database')
    admin_dsn, database = _admin_and_name(base_url)
    _create_database(admin_dsn, database)
    artifact_root = tempfile.mkdtemp(prefix='sca-bench-artifacts-')
    os.environ['SCA_DATABASE_URL'] = __import__('sqlalchemy').engine.make_url(base_url).set(
        drivername='postgresql+psycopg', database=database).render_as_string(hide_password=False)
    os.environ['SCA_ARTIFACT_ROOT'] = artifact_root
    sys.path.insert(0, str(Path(args.app_root).resolve()))
    report = {'app_root': str(Path(args.app_root).resolve()), 'scenario': args.scenario,
              'repeats': args.repeats, 'cohorts': [], 'generated_at': datetime.now(timezone.utc).isoformat(),
              'python': sys.version, 'note': 'isolated sca_bench database; fixed deterministic cohort'}
    try:
        import app
        import app.persistence
        app.persistence.initialize()
        from app.persistence.models import Base
        for cohort in args.cohort:
            report['cohorts'].append(_measure(app, args.scenario, cohort, args.repeats,
                                               Base.metadata, jobs=args.jobs,
                                               workers=args.workers,
                                               trace_memory=args.trace_memory))
        report['app_file'] = app.__file__
    finally:
        try:
            app.persistence.reset_engine()
        except Exception:
            pass
        _drop_database(admin_dsn, database)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('scenario', 'cohorts', 'app_file')},
                     ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
