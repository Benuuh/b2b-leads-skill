"""状态视图、指标、导出与抑制名单查询（spec 2.6 S10 拆分目标 runs/query.py）。"""
import hashlib
import json
import os
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from sqlalchemy import select
from app.capabilities import METRICS_VERSION
from app.errors import AppError
from app.release import release_identity, CORE_VERSION
from app.persistence import (transaction, utcnow, uid, put, artifact_path, queue_artifact_delete)
from app.persistence.models import Run, Company, Export, Observation
from app.schemas import spec_version
from ._core import (_hash, _iso, _run, _audit, _delivery_lock, _operation, _rows,
                    _suppressions, _summary, _view, _list_view, RunProjectionContext)


def list_runs():
    with transaction() as session:
        return [_list_view(session, run)
                for run in session.scalars(select(Run).order_by(Run.created_at.desc()))]


def _export_view(row, current_progress=None):
    if row.revoked_at or not row.artifact_ref or row.expires_at <= utcnow():
        raise AppError('EXPORT_REVOKED', '导出已撤销或过期，请使用新的 export-key 重新导出', 409)
    path = artifact_path(row.artifact_ref)
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != row.artifact_hash:
        raise AppError('ARTIFACT_MISSING', '导出文件缺失或校验失败，请重新导出', 409)
    if row.report_ref:
        report_path = artifact_path(row.report_ref)
        if not report_path.is_file():
            raise AppError('ARTIFACT_MISSING', '导出报告缺失或校验失败，请重新导出', 409)
        report_bytes = report_path.read_bytes()
        try:
            valid_report = json.loads(report_bytes) == row.manifest_json
        except (ValueError, UnicodeError):
            valid_report = False
        if not valid_report or (row.report_hash and hashlib.sha256(report_bytes).hexdigest() != row.report_hash):
            raise AppError('ARTIFACT_MISSING', '导出报告缺失或校验失败，请重新导出', 409)
    research = row.manifest_json.get('research_summary', {})
    counts = row.manifest_json.get('row_counts', {})
    delivery = row.manifest_json.get('delivery', {})
    exported = counts.get('companies', 0)
    summary = {key: research.get(key) for key in ('discovered', 'matched', 'excluded', 'uncertain', 'missing')}
    summary.update(contactable=exported, exported_count=exported,
        target_quantity=research.get('target_quantity'),
        target_shortfall=max(0, research.get('target_quantity', 0) - exported))
    return dict(id=row.id, export_id=row.id, run_id=row.run_id, format=row.format, filename=row.filename,
        file=str(path), path=str(path), report_file=str(artifact_path(row.report_ref)) if row.report_ref else None,
        artifact_hash=row.artifact_hash, manifest=deepcopy(row.manifest_json),
        summary=summary, snapshot_at=row.manifest_json.get('generated_at'),
        delivery_policy=deepcopy(delivery), delivery_policy_version=delivery.get('policy_version'),
        current_progress=current_progress,
        external_tool_usage=deepcopy(research.get('external_tool_usage', {})),
        quality_report=deepcopy(row.manifest_json.get('quality_report') or {
            'metrics': row.manifest_json.get('metrics'), 'delivery': row.manifest_json.get('delivery')}))


def _metrics_context(session, run, now, projection=None, prepared=None):
    rows = list(session.scalars(select(Observation).where(Observation.run_id == run.id, Observation.started_at <= now)))
    http = [row for row in rows if row.kind == 'http']
    unfinished = [row for row in http if row.state == 'started' or not row.finished_at or row.finished_at > now]
    unfinished_ids = {row.id for row in unfinished}
    settled = [row for row in http if row.id not in unfinished_ids]
    denied = [row for row in settled if row.data_json.get('dispatched') is False]
    # Settled observed-1 rows predate admission/dispatch separation and retain
    # their original physical-attempt contract. Explicit new unknowns never do.
    dispatched = [row for row in settled if row.data_json.get('dispatched') is True
                  or ('dispatched' not in row.data_json
                      and row.data_json.get('protocol_version') in {None, 'observed-1'})]
    unknown = len(http) - len(denied) - len(dispatched)
    pruned = run.stats_json.get('observation_pruned', False)
    lookup_pruned = run.stats_json.get('lookup_observation_pruned', False)
    summary = dict(enabled=True, protocol_version='observed-2',
        actual_http_attempts=None if pruned or unknown else len(dispatched), recorded_attempts=len(http),
        confirmed_http_attempts=None if pruned else len(dispatched),
        dispatch_denied_attempts=None if pruned else len(denied),
        dispatch_unknown_attempts=None if pruned else unknown,
        by_purpose=dict(Counter(row.data_json.get('purpose', 'unknown') for row in dispatched)),
        source_counts=dict(Counter(row.data_json.get('source_id', 'unknown') for row in dispatched)),
        query_counts=dict(Counter(row.data_json.get('query_id', 'unknown') for row in dispatched)),
        status_counts=dict(Counter(str(row.data_json.get('http_status') or row.data_json.get('error_code') or 'unknown') for row in dispatched)),
        retry_attempts=sum(row.data_json.get('attempt', 1) > 1 for row in dispatched),
        unfinished_attempts=len(unfinished), outcome_unknown=unknown,
        completeness='historical_missing' if pruned else 'incomplete' if unknown else 'observed',
        cache_hits=None, throttle_wait_seconds=sum(row.data_json.get('throttle_seconds', 0) for row in http),
        response_bytes=sum(row.data_json.get('response_bytes') or 0 for row in dispatched) if not unknown and not pruned else None)
    for kind in ('dns', 'mx'):
        selected = [row for row in rows if row.kind == kind]
        pending = sum(row.state == 'started' or not row.finished_at or row.finished_at > now for row in selected)
        summary[kind + '_attempts'] = None if lookup_pruned or pending else len(selected)
        summary[kind + '_recorded_attempts'] = len(selected)
        summary[kind + '_unknown_count'] = pending
    summary['dns_mx_completeness'] = 'historical_missing' if lookup_pruned else 'incomplete' if any(
        summary[kind + '_unknown_count'] for kind in ('dns', 'mx')) else 'observed'
    state = projection.summary(False) if projection is not None else _summary(session, run, now)
    context = dict(as_of=now, started_at=run.created_at, finished_at=run.finished_at, batch_status=run.status,
        paused_seconds=run.stats_json.get('paused_seconds', 0), paused_at=run.stats_json.get('paused_at'),
        task_processing_seconds=run.stats_json.get('active_seconds', 0),
        stage_processing_seconds=run.stats_json.get('stage_processing_seconds', {}),
        raw_candidate_count=state['admitted_count'] + run.stats_json.get('duplicates', 0),
        discovered=state['admitted_count'], failed=state['failed'], observation_summary=summary,
        metrics_version=METRICS_VERSION,
        research=_research_metrics(session, run, state, summary,
            rows=projection.rows if projection is not None else None))
    if prepared is not None:
        from app.delivery.selection import eligibility
        context['prepared_rows'] = prepared['coalesced']
        context['qualified_rows'] = [row for row in prepared['coalesced']
                                     if eligibility(row)[0] == 'qualified']
        if projection is not None and spec_version(run.spec_json) == 2:
            context['batch_channels'] = projection.qualified
    return context


def _research_metrics(session, run, state, observation_summary, rows=None):
    """Versioned research metrics; unknown values stay null, never zero."""
    stats = run.stats_json
    if rows is None:
        rows = [row.data_json for row in session.scalars(
            select(Company).where(Company.run_id == run.id))]
    decisions = Counter((row.get('qualification') or {}).get('decision') for row in rows)
    origins = deepcopy(stats.get('qualified_by_origin') or {})
    return {
        'metrics_version': METRICS_VERSION,
        'spec_version': spec_version(run.spec_json),
        'discovery_seen': stats.get('discovery_seen'),
        'append_ready': stats.get('append_ready'),
        'admitted': state['admitted_count'],
        'qualified': decisions['qualified'],
        'excluded': decisions['excluded'],
        'needs_review': decisions['uncertain'],
        'qualified_by_origin': origins or None,
        'new_qualified': origins.get('live_research'),
        'supplement_qualified': origins.get('supplemented_evidence'),
        'offline_reassess_qualified': origins.get('saved_evidence_reassessed'),
        'offline_evidence_qualified': origins.get('offline_evidence'),
        'missing_items': state.get('missing_items') or None,
        'failure_categories': stats.get('failure_categories') or None,
        'tool_invocations': state.get('external_tool_usage') or None,
        'physical_http_attempts': observation_summary.get('actual_http_attempts'),
        'dns_attempts': observation_summary.get('dns_attempts'),
        'mx_attempts': observation_summary.get('mx_attempts'),
        'cache_hits': observation_summary.get('cache_hits'),
        'throttle_wait_seconds': observation_summary.get('throttle_wait_seconds'),
        'snapshots': deepcopy(stats.get('snapshots') or None),
        'reserved_pages': stats.get('reserved_pages'),
        'queue_wait_seconds': stats.get('queue_wait_seconds'),
        'stage_processing_seconds': deepcopy(stats.get('stage_processing_seconds') or None),
        'units': {'counts': 'enterprises', 'timing': 'seconds', 'requests': 'physical_attempts'},
        'semantics': 'discovered=admitted_count unchanged; discovery_seen counts submitted candidates; '
                     'tool_invocations are Agent-side calls and never substitute physical HTTP attempts',
    }


def export(run_id, format='xlsx', export_key=None, filters=None, channels=None):
    from app.delivery.exporting import build_export
    from app.delivery.selection import normalize_channels, normalize_filters
    if format not in {'xlsx', 'csv'}:
        raise AppError('VALIDATION_ERROR', '导出格式仅支持 xlsx 或 csv')
    filters = normalize_filters(filters)
    channels = normalize_channels(channels)
    with transaction() as session:
        _delivery_lock(session)
        run = _run(session, run_id)
        now = utcnow()
        records = _rows(session, run)
        suppressed = _suppressions(session, run.spec_json)
        version = spec_version(run.spec_json)
        policy = ((run.spec_json.get('qualification_policy') or {}).get('contact_policy')
                  if version == 2 else None)
        identity = 'v2' if version == 2 else 'legacy'
        freshness = run.spec_json.get('freshness_days', 180)
        from app.delivery.selection import prepare_cohort, select_export_companies
        prepared = prepare_cohort(records, filters=None, suppressions=suppressed,
            freshness_days=freshness, now=now, identity_version=identity)
        # One transaction-level projection context serves key, manifest, metrics
        # and the returned view. It reads the cohort once and reuses the export
        # selection's delivery rows as its contactable cohort.
        delivery_sink = {}
        projection, eligible_count = select_export_companies(records, mode='balanced',
            suppressions=suppressed, freshness_days=freshness, now=now,
            delivery_only=True, delivery_limit=run.spec_json['quantity'], policy=policy,
            identity_version=identity, filters=filters, channels=channels, prepared=prepared,
            delivery_sink=delivery_sink)
        projection_context = RunProjectionContext(session, run, now,
            rows=records, suppressions=suppressed, contactable=delivery_sink.get('rows'))
        key = export_key or 'auto:' + _hash({'projection': projection, 'version': run.version, 'format': format,
            'suppressions': suppressed, 'filters': filters, 'channels': channels})
        operation, replay = _operation(session, run, 'export', key,
            {'format': format, 'filters': filters, 'channels': channels})
        if replay:
            row = session.get(Export, operation.result_json['export_id'])
            if row and not row.revoked_at and row.artifact_ref and row.expires_at > now:
                return _export_view(row, projection_context.summary(False))
            if export_key:
                raise AppError('EXPORT_REVOKED', '导出已撤销或过期，请使用新的 export-key', 409)
        if run.demo_only:
            raise AppError('DEMO_EXPORT_BLOCKED', '演示数据不能正式导出', 409)
        if not records:
            raise AppError('EXPORT_EMPTY', '批次暂无可导出企业', 409)
        manifest = dict(batch_id=run.id, run_id=run.id, segment_hash=run.spec_hash,
            source_strategy=run.plan_json, generated_at=_iso(now), tool_version=f'skill-v{CORE_VERSION}',
            runtime_release=release_identity(), research_release=run.plan_json.get('release'),
            freshness_days=freshness, budget=0, cost=0,
            spec_version=version, metrics_version=METRICS_VERSION,
            research_summary=projection_context.summary(False))
        if version == 2:
            from app.rules.policy import policy_summary
            manifest['policy'] = policy_summary(run.spec_json)
            manifest['evidence_policy'] = deepcopy(run.spec_json.get('evidence_policy'))
        data, filename, media_type, manifest = build_export(records, manifest, format=format,
            mode='balanced', language='zh', suppressions=suppressed,
            now=now, delivery_only=True, delivery_limit=run.spec_json['quantity'],
            metrics_context=_metrics_context(session, run, now,
                                             projection=projection_context, prepared=prepared),
            policy=policy, identity_version=identity, filters=filters, channels=channels,
            selected=projection, eligible_delivery=eligible_count)
        if len(data) > 100 * 1024 ** 2:
            raise AppError('EXPORT_TOO_LARGE', '导出超过 100 MiB，请缩小批次')
        ref, digest = put(data, 'xlsx' if format == 'xlsx' else 'zip')
        report_ref, report_hash = put(json.dumps(manifest, ensure_ascii=False, default=str, indent=2).encode(), 'json')
        row = Export(id=uid('export'), run_id=run.id, format=format, filename=filename,
            artifact_ref=ref, artifact_hash=digest, report_ref=report_ref,
            report_hash=report_hash, manifest_json=manifest,
            expires_at=now + timedelta(days=int(os.getenv('B2B_EXPORT_RETENTION_DAYS', '30'))))
        session.add(row)
        session.flush()
        result = _export_view(row, projection_context.summary(False))
        operation.result_json = {'export_id': row.id}
        _audit(session, run.id, 'export.created', export_id=row.id)
        return result


def suppression_list():
    with transaction() as session:
        return _suppressions(session)


def _revoke_exports(session, run_id=None):
    query = select(Export).where(Export.revoked_at.is_(None))
    if run_id:
        query = query.where(Export.run_id == run_id)
    count = 0
    for row in session.scalars(query.with_for_update()):
        queue_artifact_delete(session, row.artifact_ref)
        queue_artifact_delete(session, row.report_ref)
        row.artifact_ref = row.report_ref = None
        row.revoked_at = utcnow()
        count += 1
    return count
