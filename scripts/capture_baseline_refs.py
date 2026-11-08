"""Record verified opaque references to an existing run's retained page artifacts.

This read-only database operation copies neither pages, URLs, spec, nor
suppression lists. It does not assert human truth or make a G0 determination.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.persistence import artifact_path, transaction
from app.persistence.models import Company, Run, Snapshot


def capture(run_id: str, limit: int) -> dict:
    if not 1 <= limit <= 30:
        raise ValueError('limit must be 1..30')
    now = datetime.now(timezone.utc)
    selected = []
    with transaction() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError('run not found')
        rows = session.execute(select(Snapshot, Company.id).join(Company, Company.id == Snapshot.company_id)
                               .where(Snapshot.run_id == run_id, Company.run_id == run_id,
                                      Snapshot.artifact_ref.is_not(None))
                               .order_by(Company.id, Snapshot.id)).all()
        seen = set()
        missing = 0
        for snap, company_id in rows:
            if company_id in seen or snap.expires_at <= now or not snap.artifact_hash:
                continue
            try:
                data = artifact_path(snap.artifact_ref).read_bytes()
            except (OSError, ValueError):
                missing += 1
                continue
            if hashlib.sha256(data).hexdigest() != snap.artifact_hash:
                missing += 1
                continue
            seen.add(company_id)
            selected.append({'company_id': company_id, 'snapshot_id': snap.id,
                             'artifact_ref': snap.artifact_ref, 'sha256': snap.artifact_hash,
                             'recorded_at': snap.created_at.isoformat(),
                             'collected_at': None,
                             'collected_at_missing_reason': 'not stored in snapshot metadata',
                             'expires_at': snap.expires_at.isoformat(),
                             'manual_truth': None})
            if len(selected) >= limit:
                break
        plan = run.plan_json or {}
        manifest = {'version': 'cost-baseline-page-refs-1', 'run_id': run_id,
                    'config_hash': run.spec_hash, 'release': plan.get('release'),
                    'rule_version': plan.get('rules_version'),
                    'selection': 'first company IDs, then first snapshot IDs, excluding expired or unverifiable artifacts',
                    'created_at': now.isoformat(), 'requested_companies': limit,
                    'unverifiable_artifacts_encountered': missing, 'samples': selected,
                    'truth_status': 'not reviewed; no conclusions about eligibility',
                    'token_status': 'not available from this artifact index'}
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'manifest': manifest, 'manifest_sha256': hashlib.sha256(canonical).hexdigest()}


def verify(wrapper: dict) -> dict:
    """Verify manifest and each retained reference without returning page contents."""
    manifest = wrapper['manifest']
    canonical = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if wrapper.get('manifest_sha256') != hashlib.sha256(canonical).hexdigest():
        raise ValueError('manifest hash mismatch')
    if manifest.get('version') != 'cost-baseline-page-refs-1':
        raise ValueError('unsupported manifest')
    errors = []
    now = datetime.now(timezone.utc)
    with transaction() as session:
        run = session.get(Run, manifest['run_id'])
        if run is None or run.spec_hash != manifest['config_hash']:
            raise ValueError('frozen run or configuration unavailable')
        if ((run.plan_json or {}).get('release') != manifest.get('release')
                or (run.plan_json or {}).get('rules_version') != manifest.get('rule_version')):
            raise ValueError('frozen release or rule version changed')
        for index, item in enumerate(manifest['samples']):
            snap = session.get(Snapshot, item['snapshot_id'])
            company = session.get(Company, item['company_id'])
            reason = None
            if (snap is None or company is None or company.run_id != manifest['run_id']
                    or snap.run_id != manifest['run_id'] or snap.company_id != item['company_id']
                    or snap.artifact_ref != item['artifact_ref'] or snap.artifact_hash != item['sha256']):
                reason = 'REFERENCE_CHANGED_OR_MISSING'
            elif snap.expires_at <= now:
                reason = 'EXPIRED'
            else:
                try:
                    actual = hashlib.sha256(artifact_path(snap.artifact_ref).read_bytes()).hexdigest()
                    if actual != item['sha256']:
                        reason = 'CONTENT_HASH_MISMATCH'
                except (OSError, ValueError):
                    reason = 'ARTIFACT_UNAVAILABLE'
            if reason:
                errors.append({'index': index, 'reason': reason})
    return {'version': manifest['version'], 'samples': len(manifest['samples']),
            'verified': len(manifest['samples']) - len(errors), 'errors': errors,
            'human_truth_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = capture(args.run, args.limit)
    # Exclusive create avoids replacing an earlier sample or arbitrary file.
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'saved_companies': len(result['manifest']['samples']),
                      'manifest_sha256': result['manifest_sha256']}))


if __name__ == '__main__':
    main()
