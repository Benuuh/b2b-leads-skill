"""Offline comparison: manual EvidenceBundle vs staged draft from the same observations.

The manual side reproduces what an Agent submits today (candidate JSON assembled by
hand); the staged side runs the same observations through
``evidence_stage_prototype.draft``.  Both sides then go through the production
``validate_input`` precheck and, when admissible, the frozen qualification policy.

No network, no database, no writes outside the requested report path.  A case where
the staged path accepts something the manual path rejects is a defect and fails the
comparison.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import evidence_stage_prototype as stage  # noqa: E402

FORMAT = 'stage-comparison-1'
AS_OF = '2026-11-03T00:00:00+00:00'

SPEC = {
    'spec_version': 2,
    'name': 'comparison fixture',
    'industry': 'industrial packaging',
    'countries': ['DE'],
    'quantity': 50,
    'paid_sources': False,
    'budget': 0,
    'qualification_policy': {
        'country_scope': 'headquarters',
        'required_all': [
            {'id': 'hq', 'field': 'entity.headquarters_country', 'operator': 'in', 'value': ['DE']},
            {'id': 'maker', 'field': 'business.models', 'operator': 'includes', 'value': 'manufacturer'},
        ],
        'exclude_any': [],
        'contact_policy': {'mode': 'any', 'allowed_types': ['business_email'],
                           'named_person_required': False, 'required_roles': []},
    },
    'evidence_policy': {'freshness_days': 180},
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _at(days_before: int) -> str:
    moment = datetime.fromisoformat(AS_OF) - timedelta(days=days_before)
    return moment.isoformat()


def source(source_id: str, url: str, text: str, *,
           collected_at: str | None = None, source_type: str = 'directory') -> dict:
    return {'source_id': source_id, 'url': url, 'source_type': source_type,
            'collected_at': collected_at or _at(1), 'text': text, 'sha256': _sha(text)}


def entry(candidate_id: str, source_id: str, subject: str, text: str) -> dict:
    start = text.index(subject)
    boundary = text.find('\n\n', start)
    end = boundary if boundary != -1 else len(text)
    return {'candidate_id': candidate_id, 'source_id': source_id, 'subject': subject,
            'start': start, 'end': end}


def observation(candidate_id: str, source_id: str, subject: str, field: str, value: str,
                excerpt: str, *, kind: str = 'evidence',
                method: str = 'agent_page_observation') -> dict:
    return {'candidate_id': candidate_id, 'source_id': source_id, 'subject': subject,
            'field': field, 'value': value, 'method': method, 'excerpt': excerpt, 'kind': kind}


def artifact(run_id: str, sources: list, entries: list, observations: list) -> dict:
    return {'version': stage.VERSION, 'run_id': run_id, 'sources': sources,
            'entries': entries, 'observations': observations}


def manual_candidate(candidate_id: str, observations: list, sources: dict) -> dict:
    """Mirror ``stage.draft`` without its reviews: what a hand builder submits."""
    own = [item for item in observations if item['candidate_id'] == candidate_id]
    name = next(item for item in own if item['field'] == 'company_name')
    origin = sources[name['source_id']]
    item = {'company_name': name['subject'], 'source_url': origin['url'],
            'source_type': origin['source_type'], 'retrieved_at': origin['collected_at'],
            'source_excerpt': name['excerpt'], 'sources': []}
    for obs in own:
        if obs['field'] not in stage.FIELD_MAP:
            continue
        target, multiple = stage.FIELD_MAP[obs['field']]
        if multiple:
            item.setdefault(target, []).append(obs['value'])
        elif target not in item:
            item[target] = obs['value']
        source_row = sources[obs['source_id']]
        item['sources'].append({'field': obs['field'], 'value': obs['value'],
                                'source_url': source_row['url'],
                                'source_type': source_row['source_type'],
                                'retrieved_at': source_row['collected_at'],
                                'method': obs['method'], 'source_excerpt': obs['excerpt']})
    return item


ATLAS = 'Atlas Example LLC'
BOREALIS = 'Borealis Packaging GmbH'
ATLAS_TEXT = ('Atlas Example LLC, Düsseldorf.\n'
              'Atlas Example LLC headquarters DE\n'
              'Atlas Example LLC is a manufacturer of reusable industrial packaging\n'
              'Atlas Example LLC email sales@atlas.example.com\n'
              'Atlas Example LLC website https://atlas.example.com/\n'
              'Atlas Example LLC score 42')
BOREALIS_TEXT = ('Borealis Packaging GmbH, Bremen.\n'
                 'Borealis Packaging GmbH headquarters DE\n'
                 'Borealis Packaging GmbH is a manufacturer of reusable industrial packaging\n'
                 'Borealis Packaging GmbH email sales@borealis.example.com')


def case_valid() -> dict:
    run_id, url = 'cmp-valid', 'https://directory.example.com/atlas'
    src = source('s1', url, ATLAS_TEXT)
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE',
                        f'{ATLAS} headquarters DE'),
            observation('a', 's1', ATLAS, 'business.models', 'manufacturer',
                        f'{ATLAS} is a manufacturer of reusable industrial packaging'),
            observation('a', 's1', ATLAS, 'offering.products', 'reusable industrial packaging',
                        f'{ATLAS} is a manufacturer of reusable industrial packaging'),
            observation('a', 's1', ATLAS, 'email', 'sales@atlas.example.com',
                        f'{ATLAS} email sales@atlas.example.com')]
    payload = artifact(run_id, [src], [entry('a', 's1', ATLAS, ATLAS_TEXT)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src})},
            'expect': 'equivalent_accepted'}


def case_two_subjects() -> dict:
    run_id, url = 'cmp-two-subjects', 'https://directory.example.com/members'
    text = ATLAS_TEXT + '\n\n' + BOREALIS_TEXT
    src = source('s1', url, text)
    rows = [
        observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
        observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE'),
        observation('a', 's1', ATLAS, 'business.models', 'manufacturer',
                    f'{ATLAS} is a manufacturer of reusable industrial packaging'),
        observation('a', 's1', ATLAS, 'email', 'sales@atlas.example.com',
                    f'{ATLAS} email sales@atlas.example.com'),
        observation('b', 's1', BOREALIS, 'company_name', BOREALIS, BOREALIS),
        observation('b', 's1', BOREALIS, 'entity.headquarters_country', 'DE',
                    f'{BOREALIS} headquarters DE'),
        observation('b', 's1', BOREALIS, 'business.models', 'manufacturer',
                    f'{BOREALIS} is a manufacturer of reusable industrial packaging'),
        observation('b', 's1', BOREALIS, 'email', 'sales@borealis.example.com',
                    f'{BOREALIS} email sales@borealis.example.com'),
    ]
    payload = artifact(run_id, [src],
                       [entry('a', 's1', ATLAS, text), entry('b', 's1', BOREALIS, text)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a', 'b'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src}),
                       'b': manual_candidate('b', rows, {'s1': src})},
            'expect': 'equivalent_accepted'}


def case_ambiguous_excerpt() -> dict:
    """b's email excerpt mentions the other known enterprise, so b is withheld."""
    run_id, url = 'cmp-ambiguous', 'https://directory.example.com/ambiguous'
    tail = ('Borealis Packaging GmbH, Bremen.\n'
            'Borealis Packaging GmbH email sales@borealis.example.com as listed beside Atlas Example LLC')
    text = ATLAS_TEXT + '\n\n' + tail
    src = source('s1', url, text)
    rows = [
        observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
        observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE'),
        observation('a', 's1', ATLAS, 'business.models', 'manufacturer',
                    f'{ATLAS} is a manufacturer of reusable industrial packaging'),
        observation('a', 's1', ATLAS, 'email', 'sales@atlas.example.com',
                    f'{ATLAS} email sales@atlas.example.com'),
        observation('b', 's1', BOREALIS, 'company_name', BOREALIS, BOREALIS),
        observation('b', 's1', BOREALIS, 'email', 'sales@borealis.example.com',
                    'Borealis Packaging GmbH email sales@borealis.example.com as listed beside Atlas Example LLC'),
    ]
    payload = artifact(run_id, [src],
                       [entry('a', 's1', ATLAS, text), entry('b', 's1', BOREALIS, text)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a', 'b'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src}),
                       'b': manual_candidate('b', rows, {'s1': src})},
            'expect': 'staged_withheld',
            'expect_by_candidate': {'a': 'equivalent_accepted', 'b': 'staged_withheld'}}


def case_duplicate() -> dict:
    run_id = 'cmp-duplicate'
    first, second = 'https://directory.example.com/atlas', 'https://directory.example.com/atlas-de'
    src_a, src_b = source('s1', first, ATLAS_TEXT), source('s2', second, ATLAS_TEXT)
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE'),
            observation('b', 's2', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('b', 's2', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE')]
    payload = artifact(run_id, [src_a, src_b],
                       [entry('a', 's1', ATLAS, ATLAS_TEXT), entry('b', 's2', ATLAS, ATLAS_TEXT)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a', 'b'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src_a, 's2': src_b}),
                       'b': manual_candidate('b', rows, {'s1': src_a, 's2': src_b})},
            'expect': 'staged_withheld'}


def case_conflict() -> dict:
    run_id = 'cmp-conflict'
    at_text = ATLAS_TEXT.replace('headquarters DE', 'headquarters AT')
    src_a = source('s1', 'https://directory.example.com/atlas', ATLAS_TEXT)
    src_b = source('s2', 'https://atlas.example.com/about', at_text, source_type='official_site')
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE'),
            observation('a', 's2', ATLAS, 'entity.headquarters_country', 'AT', f'{ATLAS} headquarters AT')]
    payload = artifact(run_id, [src_a, src_b],
                       [entry('a', 's1', ATLAS, ATLAS_TEXT), entry('a', 's2', ATLAS, at_text)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src_a, 's2': src_b})},
            'expect': 'staged_withheld'}


def case_expired() -> dict:
    run_id, url = 'cmp-expired', 'https://directory.example.com/atlas-old'
    src = source('s1', url, ATLAS_TEXT, collected_at=_at(240))
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE')]
    payload = artifact(run_id, [src], [entry('a', 's1', ATLAS, ATLAS_TEXT)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src})},
            'expect': 'staged_withheld'}


def case_identity_mismatch() -> dict:
    run_id, url = 'cmp-identity', 'https://directory.example.com/atlas-alias'
    src = source('s1', url, ATLAS_TEXT)
    rows = [observation('a', 's1', ATLAS, 'company_name', 'Atlas Example', ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE')]
    payload = artifact(run_id, [src], [entry('a', 's1', ATLAS, ATLAS_TEXT)], rows)
    manual = manual_candidate('a', rows, {'s1': src})
    manual['company_name'] = 'Atlas Example'
    manual['sources'][0]['value'] = 'Atlas Example'
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual}, 'expect': 'staged_withheld'}


def case_search_snippet() -> dict:
    run_id, url = 'cmp-snippet', 'https://directory.example.com/search'
    src = source('s1', url, ATLAS_TEXT, source_type='search_snippet')
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE')]
    payload = artifact(run_id, [src], [entry('a', 's1', ATLAS, ATLAS_TEXT)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src})},
            'expect': 'equivalent_rejected'}


def case_unsupported_field() -> dict:
    run_id, url = 'cmp-unsupported', 'https://directory.example.com/atlas-score'
    src = source('s1', url, ATLAS_TEXT)
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE'),
            observation('a', 's1', ATLAS, 'evil.score', '42', f'{ATLAS} score 42')]
    payload = artifact(run_id, [src], [entry('a', 's1', ATLAS, ATLAS_TEXT)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src})},
            'expect': 'staged_withheld'}


def case_tampered_excerpt() -> dict:
    run_id, url = 'cmp-tampered', 'https://directory.example.com/atlas-tampered'
    src = source('s1', url, ATLAS_TEXT)
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'CH',
                        f'{ATLAS} headquarters CH')]
    payload = artifact(run_id, [src], [entry('a', 's1', ATLAS, ATLAS_TEXT)], rows)
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual_candidate('a', rows, {'s1': src})},
            'expect': 'staged_withheld'}


def case_website_lead() -> dict:
    run_id, url = 'cmp-website-lead', 'https://search.example.com/results'
    src = source('s1', url, ATLAS_TEXT, source_type='other')
    rows = [observation('a', 's1', ATLAS, 'company_name', ATLAS, ATLAS),
            observation('a', 's1', ATLAS, 'entity.headquarters_country', 'DE', f'{ATLAS} headquarters DE'),
            observation('a', 's1', ATLAS, 'website_url', 'https://atlas.example.com/',
                        f'{ATLAS} website https://atlas.example.com/', kind='lead', method='search_result')]
    payload = artifact(run_id, [src], [entry('a', 's1', ATLAS, ATLAS_TEXT)], rows)
    manual = manual_candidate('a', rows, {'s1': src})
    manual['website_url'] = 'https://atlas.example.com/'
    return {'id': run_id, 'payload': payload, 'candidates': ['a'],
            'manual': {'a': manual}, 'expect': 'staged_withheld'}


CASES = [case_valid, case_two_subjects, case_ambiguous_excerpt, case_duplicate,
         case_conflict, case_expired, case_identity_mismatch, case_unsupported_field,
         case_tampered_excerpt, case_website_lead, case_search_snippet]


def _checked(candidate: dict, normalized: dict) -> dict:
    from app.runs import validate_input
    result = validate_input(SPEC, {'candidates': [candidate]})
    row = result['records'][0]
    decision = None
    if row['admissible']:
        from app.rules.policy import assess_policy
        decision = assess_policy(candidate, [], normalized)['decision']
    return {'admissible': bool(row['admissible']), 'reason_code': row['reason_code'],
            'decision': decision}


def compare() -> dict:
    from app.schemas import parse_segment
    normalized, _version = parse_segment(SPEC)
    rows, pairs = [], []
    for build in CASES:
        case = build()
        staged = stage.draft(case['payload'], SPEC, case['candidates'], as_of=AS_OF)
        accepted = {item['company_name']: item for item in staged['submittable']['candidates']}
        errors = {error['candidate_id']: error for error in staged['errors']}
        for candidate_id in case['candidates']:
            manual = case['manual'][candidate_id]
            name = manual['company_name']
            staged_item = accepted.get(name)
            manual_check = _checked(manual, normalized)
            if staged_item is not None:
                staged_check = _checked(staged_item, normalized)
                identical = staged_item == manual
            else:
                staged_check = {'admissible': False,
                                'reason_code': errors[candidate_id]['reason_code'],
                                'decision': None}
                identical = False
            if manual_check['admissible'] and staged_check['admissible']:
                outcome = 'equivalent_accepted' if identical else 'candidate_mismatch'
            elif not manual_check['admissible'] and not staged_check['admissible']:
                outcome = 'equivalent_rejected'
            elif manual_check['admissible']:
                outcome = 'staged_withheld'
            else:
                outcome = 'staged_accepted_manual_rejected'
            rows.append({'case': case['id'], 'candidate_id': candidate_id,
                         'company_name': name, 'outcome': outcome,
                         'manual': manual_check, 'staged': staged_check,
                         'candidate_identical': identical,
                         'expected': (case.get('expect_by_candidate') or {}).get(candidate_id, case['expect'])})
            if outcome == 'equivalent_accepted':
                pairs.append({'case': case['id'], 'candidate_id': candidate_id,
                              'company_name': name, 'manual': manual, 'staged': staged_item})
    totals = {}
    for row in rows:
        totals[row['outcome']] = totals.get(row['outcome'], 0) + 1
    report = {'format': FORMAT, 'as_of': AS_OF,
              'spec_sha256': hashlib.sha256(json.dumps(SPEC, sort_keys=True).encode()).hexdigest(),
              'prototype': stage.VERSION, 'cases': rows, 'pairs': pairs,
              'totals': dict(sorted(totals.items())),
              'ok': 'staged_accepted_manual_rejected' not in totals,
              'note': 'Precheck and policy decisions only; no admission, delivery or network.'}
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    report = compare()
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'ok': report['ok'], 'totals': report['totals']}, ensure_ascii=False))
    raise SystemExit(0 if report['ok'] else 2)


if __name__ == '__main__':
    main()
