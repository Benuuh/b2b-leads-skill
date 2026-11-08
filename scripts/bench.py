"""Spec b2b-leads-2.6 §4.2 speed benchmark on a frozen candidate snapshot.

基线挂在候选快照上，不挂在 run 上（硬规矩 #7）：run 在跑时候选池在变，
第二次就不是同一批。先 ``--export-snapshot`` 从已保存 run 导出一次
``bench/<日期>-candidates.json``（id + 域名 + 国家），之后每次对比都喂同一个
文件，不现取数据库。

用法：
    python scripts/bench.py export --run RUN --limit 20 --out bench/2026-11-04-candidates.json
    python scripts/bench.py run --snapshot bench/2026-11-04-candidates.json \
        --repeats 3 --out bench/2026-11-04-baseline.json

真实联网基准：只抓公开页面，走 core 的 robots/限流/安全路径；对快照里的
域名失效允许换，但报告里必须标注（硬规矩 #7）。
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'backend'))

SLOW_PAGES_THRESHOLD = 3


def _spec():
    """Minimal contacts-stage segment; bench measures transport, not qualification."""
    return {
        'spec_version': 2, 'name': 'Bench snapshot', 'industry': 'freight forwarding',
        'countries': ['US'], 'quantity': 200,
        'qualification_policy': {'country_scope': 'headquarters', 'required_all': [],
                                 'exclude_any': [], 'contact_policy': {
                                     'mode': 'any',
                                     'allowed_types': ['business_email', 'business_phone', 'contact_form']}},
    }


def export_snapshot(run_id, limit, out_path):
    from app.persistence import transaction
    from app.persistence.models import Company
    with transaction() as session:
        rows = (session.query(Company)
                .filter(Company.run_id == run_id)
                .limit(max(1, int(limit))).all())
        entries = []
        for row in rows:
            data = row.data_json or {}
            website = str(data.get('website_url') or '')
            host = urlsplit(website).hostname or ''
            entries.append({
                'company_id': row.id,
                'company_name': data.get('company_name'),
                'domain': host,
                'country': data.get('headquarters_country') or data.get('country_code'),
            })
    Path(out_path).write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding='utf-8')
    return {'snapshot': str(out_path), 'companies': len(entries)}


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def run_benchmark(snapshot_path, repeats, out_path, max_pages, timeout_seconds, workers=1):
    entries = json.loads(Path(snapshot_path).read_text(encoding='utf-8'))
    if not isinstance(entries, list) or not entries:
        raise SystemExit('快照为空或格式错误；先 export --run 导出候选快照')
    spec = _spec()
    runs = []
    from concurrent.futures import ThreadPoolExecutor

    def measure(entry):
        domain = str(entry.get('domain') or '').strip()
        website = f'https://{domain}/' if domain else ''
        candidate = {'company_id': entry.get('company_id') or domain, 'company_name':
                     entry.get('company_name') or domain, 'website_url': website,
                     'headquarters_country': entry.get('country')}
        outcome = {'company_id': candidate['company_id'], 'domain': domain,
                   'website_url': website}
        page_started = time.perf_counter()
        if not domain:
            outcome.update(pages=0, failed_pages=0, seconds=0.0, error='no_domain')
            return outcome
        from app.web.crawler import fetch_company
        from app.web.http import request_deadline
        from app.web.observation import observation_context
        observed = {'started': 0, 'finished': 0, 'dispatched': 0}

        def sink(event, payload):
            if event == 'start':
                observed['started'] += 1
            elif event == 'finish':
                observed['finished'] += 1
                if payload.get('dispatched'):
                    observed['dispatched'] += 1

        try:
            # ``timeout_seconds`` is a real command budget, not a report label:
            # every physical request inside this company shares the deadline.
            with request_deadline(timeout_seconds):
                with observation_context(sink=sink):
                    pages = fetch_company(candidate, spec, max_pages=max_pages, stage='contacts')
            failed = sum(1 for page in pages if page.get('fetch_status') not in ('ok',))
            outcome.update(pages=len(pages), failed_pages=failed,
                           seconds=round(time.perf_counter() - page_started, 3),
                           http_attempts=observed['started'],
                           http_dispatched=observed['dispatched'],
                           http_finished=observed['finished'],
                           error=None)
        except Exception as error:  # 单家失败不中断整批基准
            outcome.update(pages=0, failed_pages=0,
                           seconds=round(time.perf_counter() - page_started, 3),
                           http_attempts=observed['started'],
                           http_dispatched=observed['dispatched'],
                           http_finished=observed['finished'],
                           error=type(error).__name__,
                           error_code=getattr(error, 'code', None))
        return outcome

    for repeat in range(max(1, int(repeats))):
        started = time.perf_counter()
        if workers > 1:
            # O6 并发核实（跨域名并发；同域名由 core 的 host lock 串行化，单域名 ≤2）。
            with ThreadPoolExecutor(max_workers=workers) as pool:
                companies = list(pool.map(measure, entries))
        else:
            companies = [measure(entry) for entry in entries]
        elapsed = round(time.perf_counter() - started, 3)
        seconds = [item['seconds'] for item in companies if item['error'] is None]
        runs.append({
            'repeat': repeat + 1, 'total_seconds': elapsed,
            'companies': len(companies), 'failures': sum(1 for item in companies if item['error']),
            'failed_pages': sum(item['failed_pages'] for item in companies),
            'physical_http_attempts': sum(item['http_attempts'] for item in companies),
            'http_dispatched': sum(item['http_dispatched'] for item in companies),
            'pages_total': sum(item['pages'] for item in companies),
            'seconds_median': round(statistics.median(seconds), 3) if seconds else None,
            'seconds_p90': _percentile(seconds, 0.9),
            'results': companies,
        })
    baseline = {
        'snapshot': str(snapshot_path),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'repeats': len(runs), 'max_pages': max_pages, 'timeout_seconds': timeout_seconds,
        'timeout_effective': True, 'timeout_scope': 'per_company_request_deadline',
        'workers': workers,
        'runs': runs,
        'note': '快照里域名失效允许换，但必须在报告里标注（spec 4.2.1）；'
                'failures 只统计异常，failed_pages 统计已返回但失败的页面，'
                'physical_http_attempts 来自 observation sink 的 start 事件。',
    }
    Path(out_path).write_text(json.dumps(baseline, ensure_ascii=False, indent=1), encoding='utf-8')
    summary = [{key: run[key] for key in ('repeat', 'total_seconds', 'failures', 'failed_pages',
                                          'physical_http_attempts', 'http_dispatched', 'pages_total',
                                          'seconds_median', 'seconds_p90')} for run in runs]
    return {'baseline': str(out_path), 'summary': summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export', help='从已保存 run 导出候选快照（不现取数据库跑基准）')
    export.add_argument('--run', required=True)
    export.add_argument('--limit', type=int, default=20)
    export.add_argument('--out', required=True, type=Path)
    run = commands.add_parser('run', help='在固定快照上重复跑 contact 抓取基准')
    run.add_argument('--snapshot', required=True, type=Path)
    run.add_argument('--repeats', type=int, default=3)
    run.add_argument('--workers', type=int, default=1,
                     help='跨域名并发 worker 数（O6）；同域名由 core 串行化保证 ≤2')
    run.add_argument('--max-pages', type=int, default=4, help='每家页面预算，默认 4（对齐 2026-11-02 实测）')
    run.add_argument('--timeout-seconds', type=int, default=30)
    run.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'export':
        result = export_snapshot(args.run, args.limit, args.out)
    else:
        result = run_benchmark(args.snapshot, args.repeats, args.out,
                               args.max_pages, args.timeout_seconds, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
