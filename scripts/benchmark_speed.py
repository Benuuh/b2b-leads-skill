"""Reproducible synthetic pipeline benchmark; never accesses the network or database."""
import argparse
import cProfile
import json
from pathlib import Path
import socket
import statistics
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--companies', type=int, default=6)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--delay', type=float, default=.05, help='Simulated request delay, seconds')
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.backend.resolve()))
    from app.pipeline import process_company
    from app.schemas import parse_segment
    import app.web.crawler as crawler
    import app.web.dns as dns

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Synthetic benchmark attempted external access')
    socket.getaddrinfo = socket.create_connection = forbidden
    socket.socket.connect = forbidden
    dns.lookup_mx = lambda _: 'unknown'
    at = datetime.now(timezone.utc).isoformat()
    spec, _ = parse_segment({
        'spec_version': 2, 'name': 'Synthetic speed benchmark', 'industry': 'industrial packaging',
        'countries': ['DE'], 'quantity': 200,
        'qualification_policy': {'country_scope': 'headquarters', 'required_all': [
            {'id': 'hq', 'field': 'entity.headquarters_country', 'operator': 'in', 'value': ['DE']},
            {'id': 'maker', 'field': 'business.models', 'operator': 'includes', 'value': 'manufacturer'},
            {'id': 'product', 'field': 'offering.products', 'operator': 'provides',
             'value': 'reusable industrial packaging'}], 'exclude_any': [],
            'contact_policy': {'mode': 'any', 'allowed_types': ['business_email', 'business_phone', 'contact_form']}}})
    request_urls, durations, outcomes, batches = [], [], [], []
    samples = []
    profile = cProfile.Profile() if args.profile else None
    if profile:
        profile.enable()
    for repeat in range(args.repeats):
        batch_start = time.perf_counter()
        for index in range(args.companies):
            number = repeat * args.companies + index
            name = f'Benchmark Packaging {number} GmbH'
            website = f'https://benchmark-packaging-{number}.de/'
            source = f'https://packaging-association.org/members/benchmark-{number}'
            candidate = {'company_id': f'benchmark-{number}', 'company_name': name,
                         'website_url': website, 'source_url': source, 'source_type': 'directory',
                         'retrieved_at': at, 'country_code': 'DE', 'headquarters_country': 'DE',
                         'business_models': ['manufacturer'], 'offering_products': ['reusable industrial packaging'],
                         'source_excerpt': f'{name}, Germany, manufactures reusable industrial packaging.', 'sources': []}
            for field, value, excerpt in (
                ('company_name', name, name + ', Germany'),
                ('website_url', website, name + '; official website ' + website),
                ('entity.headquarters_country', 'DE', name + ', headquartered in Germany'),
                ('business.models', 'manufacturer', name + ' manufactures reusable industrial packaging'),
                ('offering.products', 'reusable industrial packaging', name + ' manufactures reusable industrial packaging'),
            ):
                candidate['sources'].append({'field': field, 'value': value, 'source_url': source,
                    'source_type': 'directory', 'retrieved_at': at, 'source_excerpt': excerpt})
            filler = ''.join(f'<div class="product"><h3>Packaging {n}</h3><p>Reusable industrial shipping systems and product details.</p></div>' for n in range(80))

            def fake_http(url, **_kwargs):
                request_urls.append(url)
                if args.delay:
                    time.sleep(args.delay)
                path = urlsplit(url).path
                contact = '<a href="mailto:sales@benchmark-packaging.de">sales@benchmark-packaging.de</a>' if path == '/contact' else ''
                links = '<a href="/team">Team</a><a href="/about">About</a><a href="/contact">Contact</a>'
                html = f'<html><head><meta property="og:site_name" content="{name}"></head><body><h1>{name}</h1>{links}{filler}{contact}</body></html>'
                return {'final_url': url, 'html': html, 'text': name, 'fetched_at': at,
                        'fetch_status': 'ok', 'source_type': 'official_site', 'http_status': 200}
            crawler.safe_http = fake_http
            pages = []
            started = time.perf_counter()
            result = process_company({'input': candidate, 'segment': spec, 'demo_only': False,
                'plan': {'page_limit_per_company': 10, 'page_limit_total': 10000},
                'pages_used': 0, 'suppressions': []}, pages)
            durations.append(time.perf_counter() - started)
            outcomes.append({'status': result['status'], 'channels': result.get('delivery_channels'),
                             'stop_reason': result['pipeline']['stop_reason'], 'pages': len(pages)})
            samples.append(result['pipeline'])
        batches.append(time.perf_counter() - batch_start)
    if profile:
        profile.disable()
        profile.dump_stats(str(args.profile))
    ordered = sorted(durations)
    report = {'synthetic_only': True, 'external_requests': 0, 'backend': str(args.backend),
              'companies_per_repeat': args.companies, 'repeats': args.repeats,
              'simulated_seconds_per_request': args.delay, 'simulated_page_requests': len(request_urls),
              'median_batch_seconds': statistics.median(batches), 'batch_seconds': batches,
              'company_p50_seconds': statistics.median(durations),
              'company_p95_seconds': ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
              'outcomes': outcomes, 'pipeline_samples': samples}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('outcomes', 'pipeline_samples')}, indent=2))


if __name__ == '__main__':
    main()
