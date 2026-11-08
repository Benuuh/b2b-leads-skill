"""discover 服务层：未核实候选池、信封落库、失败矩阵（spec 2.6 §3.1）。

硬约束（§3.1.3）：``source_candidates`` 不是候选公司。不进合格计数、
不进去重池。只有 ``advance`` 之后才算。
"""
from __future__ import annotations

from sqlalchemy import select

from ..errors import AppError
from ..persistence import transaction, uid, utcnow
from ..persistence.models import ProspectSourceCandidate, ProspectSourceEnvelope, Run
from . import adapters as adapter_module
from . import envelopes

REJECT_REASONS_REQUIRED = 'reason 必填（硬规矩 #12：事后要能复盘）'


def _run_exists(run_id: str) -> bool:
    with transaction() as session:
        return session.get(Run, run_id) is not None


def _adapter(source: str):
    """动态查找 adapter，保证测试可替换、未来可插拔（不经 ADAPTERS 静态绑定）。"""
    adapter = getattr(adapter_module, 'discover_' + source, None)
    if adapter is None:
        raise AppError('VALIDATION_ERROR', f'未知数据源: {source}',
                       details={'supported': list(adapter_module.SOURCES)})
    return adapter


def discover(run_id: str, source: str, limit: int = 50) -> dict:
    """跑一个 adapter，候选与信封落库。单源失败记原因，不抛断批。"""
    if source not in adapter_module.SOURCES:
        raise AppError('VALIDATION_ERROR', f'未知数据源: {source}',
                       details={'supported': list(adapter_module.SOURCES)})
    if not _run_exists(run_id):
        raise AppError('RUN_NOT_FOUND', f'批次不存在: {run_id}', 404)
    query = {'countries': [], 'industry': '', 'keywords': [], 'limit': int(limit)}
    with transaction() as session:
        run = session.get(Run, run_id)
        spec = run.spec_json or {}
        segment = spec.get('segment') or spec
        query['countries'] = [str(item).upper()[:2] for item in (segment.get('countries') or [])]
        query['industry'] = str(segment.get('industry') or '')
        query['keywords'] = list(segment.get('keywords') or [])[:10]
    query['limit'] = max(1, min(int(limit), 200))
    try:
        candidates = _adapter(source)(query)
        failure = None
    except AppError as error:
        candidates = []
        failure = {'code': 'source_limit' if error.code == 'SOURCE_LIMIT' else 'source_error',
                   'message': error.message}
    except Exception as error:  # 单源失败不阻塞其他源（§3.1.6）
        candidates = []
        failure = {'code': 'source_error', 'message': type(error).__name__}
    stored, error_status = [], failure
    with transaction() as session:
        if candidates:
            # 同 run 同源去重：source_ref 已存在的不重复落池。
            existing = set(session.scalars(
                select(ProspectSourceCandidate.source_ref).where(
                    ProspectSourceCandidate.run_id == run_id,
                    ProspectSourceCandidate.source == source)).all())
            for item in candidates:
                ref = str(item.get('source_ref') or '')
                if not ref or ref in existing:
                    continue
                candidate_id = uid('psc')
                envelope_payload = {'query': query, 'raw': item.get('raw')}
                try:
                    ciphertext, sha256, mode = envelopes.seal(envelope_payload)
                except envelopes.EnvelopeSecretUnavailable:
                    # §3.5 开发降级：明文存信封并打 dev-insecure 标记。
                    ciphertext, sha256, mode = envelopes.seal(envelope_payload, secret='')
                    mode = 'dev-insecure'
                session.add(ProspectSourceCandidate(
                    id=candidate_id, run_id=run_id, source=source, source_ref=ref,
                    name=str(item.get('name') or '')[:500], domain=item.get('domain'),
                    country=item.get('country'), city=item.get('city'), status='pending',
                    raw_json={'query': {key: value for key, value in query.items() if key != 'limit'},
                              'seal_mode': mode}))
                session.add(ProspectSourceEnvelope(
                    id=uid('pse'), run_id=run_id, source=source, source_ref=ref,
                    envelope_ciphertext=ciphertext, envelope_sha256=sha256, fetched_at=utcnow()))
                stored.append(candidate_id)
                existing.add(ref)
        elif error_status is None:
            error_status = {'code': 'source_empty', 'message': '源返回空结果，不算失败'}
    return {'run_id': run_id, 'source': source, 'stored': len(stored),
            'candidate_ids': stored, 'failure': error_status}


def discover_all(run_id: str, limit: int = 50) -> dict:
    """顺序跑全部三源（§3.1.6）：单源失败不阻塞；三源全挂 → partial_no_source。"""
    outcomes = {source: discover(run_id, source, limit) for source in adapter_module.SOURCES}
    total = sum(outcome['stored'] for outcome in outcomes.values())
    all_failed = total == 0 and all(outcome['failure'] for outcome in outcomes.values())
    if all_failed:
        _mark_partial_no_source(run_id, outcomes)
    return {'run_id': run_id, 'sources': outcomes, 'stored': total,
            'partial_no_source': all_failed}


def _mark_partial_no_source(run_id: str, outcomes: dict) -> None:
    """硬规矩 #4：三源全挂 → run 置 partial_no_source，保留已有候选，不再空转。"""
    with transaction() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        stats = dict(run.stats_json or {})  # 拷贝：原地改 JSONB 不会被 flush（硬坑）。
        stats['partial_no_source'] = {
            'at': utcnow().isoformat(),
            'sources': {source: outcome['failure'] for source, outcome in outcomes.items()},
            'note': '本轮没有可用数据源；保留已有候选，暂停 discover'}
        run.stats_json = stats


def list_candidates(run_id: str, source: str | None = None,
                    status: str | None = None, cursor: str | None = None,
                    limit: int = 100) -> dict:
    """discover --list：有界分页查看未核实池。不计入 discovered/qualified。"""
    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        raise AppError('VALIDATION_ERROR', 'cursor 必须是非负整数') from None
    page = max(1, min(int(limit or 100), 200))
    statement = select(ProspectSourceCandidate).where(ProspectSourceCandidate.run_id == run_id)
    if source:
        statement = statement.where(ProspectSourceCandidate.source == source)
    if status:
        statement = statement.where(ProspectSourceCandidate.status == status)
    with transaction() as session:
        rows = session.scalars(statement.order_by(ProspectSourceCandidate.created_at,
            ProspectSourceCandidate.id).offset(offset).limit(page + 1)).all()
        has_more = len(rows) > page
        rows = rows[:page]
        items = [{'id': row.id, 'source': row.source, 'name': row.name, 'domain': row.domain,
                  'country': row.country, 'city': row.city, 'status': row.status,
                  'source_ref': row.source_ref,
                  'reject_reason': row.reject_reason} for row in rows]
    return {'run_id': run_id, 'items': items, 'count': len(items),
            'cursor': str(offset + len(items)) if has_more else None,
            'has_more': has_more, 'limit': page,
            'note': '未核实候选不进合格计数、不进去重池（§3.1.3）'}


def reject_candidate(run_id: str, candidate_id: str, reason: str) -> dict:
    """--reject：reason 必填，空串报错（硬规矩 #12）。"""
    if not str(reason or '').strip():
        raise AppError('VALIDATION_ERROR', REJECT_REASONS_REQUIRED)
    with transaction() as session:
        row = session.get(ProspectSourceCandidate, candidate_id)
        if row is None or row.run_id != run_id:
            raise AppError('NOT_FOUND', f'未核实候选不存在: {candidate_id}', 404)
        row.status = 'rejected'
        row.reject_reason = str(reason).strip()
    return {'id': candidate_id, 'status': 'rejected', 'reason': str(reason).strip()}


def verify_candidate(run_id: str, candidate_id: str) -> dict:
    """核实通过：置 verified。真正进入候选公司仍必须走 advance + 证据。"""
    with transaction() as session:
        row = session.get(ProspectSourceCandidate, candidate_id)
        if row is None or row.run_id != run_id:
            raise AppError('NOT_FOUND', f'未核实候选不存在: {candidate_id}', 404)
        row.status = 'verified'
        return {'id': candidate_id, 'status': 'verified'}


def explain_envelope(run_id: str, candidate_id: str, *, confirmed: bool = False) -> dict:
    """--explain：解密信封，需人确认（§3.1.5）。解密失败返回状态，不中断。"""
    if not confirmed:
        raise AppError('CONFIRMATION_REQUIRED',
                       '解密原始记录信封需人确认；重跑时加 --yes', 428,
                       details={'candidate_id': candidate_id})
    with transaction() as session:
        candidate = session.get(ProspectSourceCandidate, candidate_id)
        if candidate is None or candidate.run_id != run_id:
            raise AppError('NOT_FOUND', f'未核实候选不存在: {candidate_id}', 404)
        envelope = session.scalars(select(ProspectSourceEnvelope).where(
            ProspectSourceEnvelope.run_id == run_id,
            ProspectSourceEnvelope.source_ref == candidate.source_ref)).first()
    if envelope is None:
        return {'id': candidate_id, 'status': 'undecryptable', 'reason': 'envelope_missing'}
    from ..secrets import secrets_status
    status = secrets_status()
    if status['envelope'] == 'dev-insecure':
        return {'id': candidate_id, 'status': 'ok', 'mode': 'plaintext',
                'envelope_sha256': envelope.envelope_sha256,
                'payload': {'note': 'dev-insecure：开发环境信封为明文存储'},
                'source_ref': candidate.source_ref}
    result = envelopes.open_envelope(envelope.envelope_ciphertext)
    if result['status'] != 'ok':
        # 硬规矩 #3：解密失败返回状态；原密文保留（不动数据库），另存 hex。
        result.setdefault('envelope_sha256', envelope.envelope_sha256)
        result['ciphertext_hex'] = envelope.envelope_ciphertext  # base64 文本原样返回
        return {'id': candidate_id, **result}
    return {'id': candidate_id, 'status': 'ok', 'mode': result.get('mode'),
            'envelope_sha256': envelope.envelope_sha256,
            'payload': result['payload'], 'source_ref': candidate.source_ref}


def enable_legacy(run_id: str) -> dict:
    """--legacy-search（§3.6 降级模式）：跳过所有 adapter，直接人工发现流程。

    产出结构与新版一致，只是没有 source_ref；status 标 ``mode: legacy``，
    导出报告第一行写明。降级是保命，不是退步。
    """
    if not _run_exists(run_id):
        raise AppError('RUN_NOT_FOUND', f'批次不存在: {run_id}', 404)
    with transaction() as session:
        run = session.get(Run, run_id)
        stats = dict(run.stats_json or {})  # 拷贝：原地改 JSONB 不会被 flush（硬坑）。
        stats['mode'] = 'legacy'
        stats['legacy_note'] = '本轮为降级模式：三个数据源不可用或被明确指定跳过，发现环节回到人工搜索；候选无 source_ref。'
        run.stats_json = stats
    return {'run_id': run_id, 'mode': 'legacy',
            'note': '已跳过数据源适配器；请按 SKILL.md 的人工发现流程继续，候选仍走 advance + 证据提交。'}


def sources_status() -> dict:
    """sources 命令：各源可用性（离线判断 + 凭据状态）。"""
    import os
    from ..secrets import secrets_status
    secrets = secrets_status()
    return {
        'eu_ted': {'available': True, 'credential': 'public'},
        'eurostat_comext': {'available': True, 'credential': 'public'},
        'google_places': {'available': bool(os.getenv('PROVIDER_CREDENTIAL_KEY', '').strip()),
                          'credential': 'PROVIDER_CREDENTIAL_KEY'},
        'secrets': secrets,
        'note': '覆盖不到东南亚（§7 遗留）',
    }
