"""Durable batch application service. No network request holds a database lock.

拆分为四个模块（spec b2b-leads-2.6 S10）：``_core``（共享内核）、``create``（创建/校验）、
``state``（状态机与推进）、``query``（视图/指标/导出）。本包显式 re-export 全部原
``app.runs`` 顶层名字——包括外部代码与测试引用的下划线辅助函数，一个不漏；
``import_module("app.runs")`` 与 ``from app import runs`` 行为不变。
"""
from ._core import (_hash, _iso, _run, _version, _audit, _delivery_lock, _operation, _rows, _suppression_view, _suppressions, _elapsed, _settle, _summary, _missing_items, _refresh, _view, _live_refresh, _live_signature, _live_remember, _live_forget, _live_refresh_existing, datetime_from)
from .create import (create, _identity, validate_input)
from .query import (list_runs, _export_view, _metrics_context, _research_metrics, export, suppression_list, _revoke_exports)
from .state import (append, advance, _action, status, _reclaim_expired, _claim, _heartbeat, _before_send, _complete, process, _snapshot_due, _maybe_snapshot, finish, reassess, SUPPLEMENT_IMMUTABLE, SUPPLEMENT_FACT_FIELDS, supplement, extend_research, _delete_company, delete_company, suppression_add, suppression_remove, maintenance)

# 兼容导出：拆分前这些名字是 ``app.runs`` 的模块属性（来自其 import），
# 外部代码按 ``runs.host_limit`` / ``runs.utcnow`` 直接读取的必须继续成立。
# 需要 monkeypatch 时请打到实际拥有它的子模块（例如 ``app.runs.query.utcnow``），
# 因为各子模块持有独立的 import 绑定。
from app.capabilities import LIMITS, METRICS_VERSION
from app.config import settings
from app.errors import AppError
from app.failures import classify, is_circuit_failure
from app.persistence import (transaction, utcnow, uid, put, artifact_path, observation_sink,
                             queue_artifact_delete, drain_pending_deletes)
from app.persistence.host_limits import limit as host_limit
from app.release import release_identity, CORE_VERSION
from app.schemas import parse_segment, spec_version

__all__ = [
    '_hash',
    '_iso',
    '_run',
    '_version',
    '_audit',
    '_delivery_lock',
    '_operation',
    '_rows',
    '_suppression_view',
    '_suppressions',
    '_elapsed',
    '_settle',
    '_summary',
    '_missing_items',
    '_refresh',
    '_view',
    '_live_refresh',
    '_live_signature',
    '_live_remember',
    '_live_forget',
    '_live_refresh_existing',
    'datetime_from',
    'create',
    '_identity',
    'validate_input',
    'list_runs',
    '_export_view',
    '_metrics_context',
    '_research_metrics',
    'export',
    'suppression_list',
    '_revoke_exports',
    'append',
    'advance',
    '_action',
    'status',
    '_reclaim_expired',
    '_claim',
    '_heartbeat',
    '_before_send',
    '_complete',
    'process',
    '_snapshot_due',
    '_maybe_snapshot',
    'finish',
    'reassess',
    'SUPPLEMENT_IMMUTABLE',
    'SUPPLEMENT_FACT_FIELDS',
    'supplement',
    'extend_research',
    '_delete_company',
    'delete_company',
    'suppression_add',
    'suppression_remove',
    'maintenance',
]
