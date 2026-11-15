"""Execution cost report written after finish/export (spec P1-5).

The report combines already-observed counters with the run's frozen estimate.
Tokens are recorded only when the runtime supplies them; otherwise they stay
``null`` with ``token_source: unavailable`` so an estimate is never passed off as
a measurement.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

REPORT_VERSION = 'run-report-1'
TOKEN_ENV = 'B2B_TOKEN_USAGE_JSON'


def report_path(run_id):
    """Managed artifact path; a plain file beside exports for humans to open."""
    return Path(settings.artifact_root).resolve() / 'reports' / f'run-{run_id}.json'


def _time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def runtime_tokens():
    """Token usage the running environment chose to publish, or ``None``."""
    raw = os.getenv(TOKEN_ENV)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    usage = {key: _number(data.get(key)) for key in ('input', 'output', 'cache')}
    total = _number(data.get('total'))
    if total is None and usage['input'] is not None and usage['output'] is not None:
        total = usage['input'] + usage['output']
    if total is None and all(value is None for value in usage.values()):
        return None
    usage['total'] = total
    usage['token_scope'] = str(data.get('token_scope') or 'unspecified')
    return usage


def _duration(seconds):
    if seconds is None:
        return '未取到'
    seconds = float(seconds)
    if seconds < 60:
        return f'{seconds:.1f} 秒'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分 {seconds % 60:.1f} 秒'
    return f'{int(seconds // 3600)} 小时 {int(seconds % 3600 // 60)} 分'


def summary_text(report):
    """Fixed chat line: elapsed time, then measured tokens or the reason."""
    elapsed = _duration(report.get('elapsed_wall_seconds'))
    tokens = report.get('actual', {}).get('tokens')
    if tokens and tokens.get('total') is not None:
        total = f"{tokens['total']}（{tokens.get('token_scope') or 'unspecified'}）"
    else:
        total = '未取到（运行环境未提供；未用估算冒充实测）'
    return f'总用时 {elapsed}；总 token {total}'


def build_report(run_id, *, status, spec, plan, research, stats, started_at, finished_at,
                 exported=None, now=None, tokens=None):
    """Pure assembly from counters that are already on the run."""
    from app.segment_brief import estimate

    now = now or datetime.now(timezone.utc)
    started = _time(started_at)
    ended = _time(finished_at)
    elapsed = max(0.0, ((ended or now) - started).total_seconds()) if started else None
    research = research or {}
    stats = stats or {}
    usage = tokens if tokens is not None else runtime_tokens()
    version = spec.get('spec_version', 1) if isinstance(spec, dict) else 1
    projection = estimate(spec or {}, version)
    report = {
        'report_version': REPORT_VERSION,
        'run_id': run_id,
        'status': status,
        'started_at': started.isoformat() if started else None,
        'finished_at': ended.isoformat() if ended else None,
        'generated_at': now.isoformat(),
        'elapsed_wall_seconds': round(elapsed, 3) if elapsed is not None else None,
        'active_seconds': research.get('active_wall_seconds'),
        'paused_seconds': stats.get('paused_seconds'),
        'pages': stats.get('pages'),
        'candidates': stats.get('append_ready') or 0,
        'admitted': research.get('discovered'),
        'qualified': research.get('matched'),
        'contactable': research.get('contactable_count'),
        'exported': exported,
        'estimate': {
            'method': projection['method'],
            'label': projection['method_label'],
            'time_hours': projection['time_hours'],
            'time_minutes': projection['time_minutes'],
            'tokens': projection['tokens'],
            'note': projection['note'],
            'uncalibrated': True,
        },
        'actual': {
            'hours': round(elapsed / 3600, 3) if elapsed is not None else None,
            'tokens': usage,
        },
        'token_scope': usage.get('token_scope') if usage else None,
        'token_source': 'runtime' if usage else 'unavailable',
        'path': str(report_path(run_id)),
    }
    report['summary_text'] = summary_text(report)
    return report


def from_view(view, *, exported=None, now=None):
    """Build a report from a ``runs`` view already returned to the caller."""
    return build_report(
        view.get('id'), status=view.get('status'), spec=view.get('spec') or {},
        plan=view.get('plan') or {}, research=view.get('research') or {},
        stats=view.get('stats') or {}, started_at=view.get('created_at'),
        finished_at=view.get('finished_at'), exported=exported, now=now)


def publish(report):
    """Write the report file; the caller decides whether a write failure matters."""
    path = Path(report['path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                    encoding='utf-8')
    return report


def read_report(run_id):
    path = report_path(run_id)
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
