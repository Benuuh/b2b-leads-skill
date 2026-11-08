"""Build the committed-state projection behind the local live results page.

The page owns no business rules. Every count and every row below is a detached
copy of the same delivery selection used by progress and export, so a page can
never admit an enterprise the core would refuse. Files are replaced atomically
and skipped when their content did not change: a reader never sees half a page,
and an idle batch does not rewrite the disk on every poll.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import settings
from app.errors import AppError
from app.persistence import transaction, utcnow

PROJECTION_VERSION = "live-2"  # bump whenever the item mapping changes: old files are not reused
STALE_AFTER_SECONDS = 120
HEARTBEAT_SECONDS = 60
MAX_SOURCES = 8
MAX_CONTACTS = 10
EXCERPT_LIMIT = 480
ITEMS_PER_LIST = 1000
RETENTION_DAYS = 30

STATUS_LABELS = {
    "running": "运行中",
    "paused": "已暂停",
    "completed": "已完成",
    "partial": "部分完成",
    "cancelled": "已取消",
    "failed": "失败/受阻",
    "blocked": "失败/受阻",
    "unknown": "未知",
}

STOP_REASONS = {
    "goal_reached": "已达目标数量",
    "time_limit": "研究时限耗尽",
    "candidate_limit": "候选上限耗尽",
    "input_finalized": "输入已结束",
    "circuit_open": "连续失败已熔断",
    "cancelled": "任务已取消",
}

CONDITION_STATUS = {"pass": "已确认", "fail": "不符合", "unknown": "待核实", "conflict": "来源冲突"}

CHANNEL_LABELS = {
    "email": "邮箱",
    "business_email": "企业邮箱",
    "phone": "电话",
    "business_phone": "企业电话",
    "contact_form": "联系表单",
}

# Identifiers and aggregates are not evidence a reader can verify; derived rows
# would repeat the same fact several times in the drawer.
NON_EVIDENCE_FIELDS = {
    "company_id", "contact_id", "entity_id", "status", "contactable", "source_count",
    "email_count", "phone_count", "named_contact_count", "qualification_score", "confidence",
    "lead_score", "score_breakdown", "missing", "conflicts", "main_email", "main_phone",
    "email_status", "phone_status", "last_verified_at", "inferred", "tags",
    "contact_priority", "priority_score", "catch_all_status", "smtp_status",
    "disposable_status", "is_role_account", "retrieved_at",
}

FIELD_LABELS = {
    "company_name": "企业名称",
    "website_url": "官网",
    "domain": "域名",
    "country_code": "国家",
    "entity.country_code": "经营国家",
    "entity.headquarters_country": "总部国家",
    "entity.registered_country": "注册国家",
    "business.models": "业务模式",
    "offering.products": "产品",
    "offering.services": "服务",
    "customer.industries": "客户行业",
    "industry_text": "行业",
    "email": "邮箱",
    "phone": "电话",
    "contact_form_url": "联系表单",
    "size.employees": "员工规模",
    "finance.revenue": "营业收入",
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")

_last_write: dict[str, float] = {}
_write_lock = threading.Lock()


def _text(value, limit=None):
    """Untrusted source text: strip control bytes, never interpret markup."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = _CONTROL.sub("", value).strip()
    if limit and len(value) > limit:
        value = value[:limit] + "…"
    return value or None


def safe_url(value):
    """Only absolute http(s) links leave the page; javascript:/data: are dropped."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(character in value for character in "\r\n\t "):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return None
    return value


def _iso(value):
    return value.isoformat() if value else None


def _parse(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _field_label(field):
    return FIELD_LABELS.get(str(field or ""), str(field or "未知字段"))


def _run_dir(run_id):
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise AppError("INVALID_RUN_ID", "run 仅支持 1 至 80 位字母、数字、点、横线和下划线")
    root = (Path(settings.artifact_root).resolve() / "live").resolve()
    path = (root / run_id).resolve()
    if path.parent != root:
        raise AppError("INVALID_RUN_ID", "run 目录不在受管理工件目录内", 409)
    return path


def projection_path(run_id):
    return _run_dir(run_id) / "projection.json"


def pending_path(run_id):
    return _run_dir(run_id) / "pending.json"


def _latest(record):
    values = []
    for source in record.get("sources") or []:
        moment = _parse(source.get("retrieved_at"))
        if moment:
            values.append(moment)
    for contact in record.get("contacts") or []:
        moment = _parse(contact.get("retrieved_at") or contact.get("last_verified_at"))
        if moment:
            values.append(moment)
    return max(values) if values else None


def _contact_items(record):
    items = []
    for contact in (record.get("contacts") or [])[:MAX_CONTACTS]:
        name = _text(contact.get("contact_name"), 120)
        title = _text(contact.get("title"), 120)
        email = _text(contact.get("email"), 200)
        if email and (contact.get("inferred") or str(contact.get("email_type") or "").casefold() == "inferred"
                      or contact.get("email_status") == "invalid"):
            email = None
        phone = _text(contact.get("phone") or contact.get("phone_raw"), 60)
        if phone and contact.get("phone_status") == "invalid":
            phone = None
        if email:
            # Observed is not verified: only an explicit verification may claim it.
            status = ("已确认" if contact.get("email_status") == "verified"
                      else "已观察" if contact.get("email_status") in {None, "", "unknown", "unverified"}
                      else "待核实")
            items.append({"kind": "email", "value": email, "label": "邮箱", "verification": status,
                          "name": name, "title": title})
        if phone:
            status = "已确认" if contact.get("phone_status") == "valid" else "待核实"
            items.append({"kind": "phone", "value": phone, "label": "电话", "verification": status,
                          "name": name, "title": title})
    return items


def _source_items(record):
    rows, seen = [], set()
    for source in record.get("sources") or []:
        url = safe_url(source.get("source_url"))
        field = _text(source.get("field"), 80)
        if not url or not field or field in {"contacts", "sources", "conflicts"}:
            continue
        if field in NON_EVIDENCE_FIELDS or field.endswith("_count"):
            continue
        if str(source.get("method") or "").startswith("derived_"):
            continue
        marker = (field, url, _text(source.get("value"), 200))
        if marker in seen:
            continue
        seen.add(marker)
        rows.append({"field": field, "label": _field_label(field),
                     "value": _text(source.get("value"), 300), "url": url,
                     "source_type": _text(source.get("source_type"), 40),
                     "excerpt": _text(source.get("source_excerpt"), EXCERPT_LIMIT),
                     "retrieved_at": _iso(_parse(source.get("retrieved_at"))),
                     "method": _text(source.get("method"), 60)})
        if len(rows) >= MAX_SOURCES:
            break
    return rows


def _match_reasons(record):
    reasons = []
    research = record.get("research") or {}
    for condition in research.get("conditions") or []:
        field = condition.get("field")
        reasons.append({"label": _field_label(field),
                        "status": CONDITION_STATUS.get(condition.get("status"), "待核实"),
                        "detail": _text(condition.get("reason"), 200)})
    for condition in research.get("exclusions") or []:
        if condition.get("status") in {"pass", "conflict"}:
            reasons.append({"label": "排除项：" + _field_label(condition.get("field")),
                            "status": CONDITION_STATUS.get(condition.get("status"), "待核实"),
                            "detail": _text(condition.get("reason"), 200)})
    if not reasons:
        for value in (record.get("match_reasons") or [])[:8]:
            reasons.append({"label": "匹配理由", "status": "已确认", "detail": _text(value, 200)})
    return [item for item in reasons if item["detail"] or item["status"] != "待核实"]


def _pending_reasons(record, deliverable, delivery_channels):
    reasons = []
    qualification = record.get("qualification") or {}
    if qualification.get("decision") == "uncertain":
        reasons.append("必要事实仍需核实")
    for missing in (qualification.get("missing") or [])[:6]:
        status = missing.get("status")
        if status == "conflict":
            reasons.append("来源冲突：" + _field_label(missing.get("field")))
        elif status == "fail":
            reasons.append("不符合：" + _field_label(missing.get("field")))
        else:
            reasons.append("缺少事实：" + _field_label(missing.get("field")))
    for missing in (record.get("channel_policy") or {}).get("missing") or []:
        name = CHANNEL_LABELS.get(str(missing), str(missing))
        text = "缺少联系方式：" + name
        if text not in reasons:
            reasons.append(text)
    if record.get("website_binding_status") == "conflicting":
        reasons.append("官网归属来源冲突")
    if record.get("website_binding_status") == "ambiguous":
        reasons.append("官网归属待确认")
    if any("stale" in (contact.get("tags") or []) for contact in record.get("contacts") or []):
        reasons.append("联系方式已过期")
    if not reasons and not deliverable:
        reasons.append("渠道政策未满足" if delivery_channels is not None else "事实或渠道待补齐")
    if not reasons:
        reasons.append("尚待资格确认")
    return list(dict.fromkeys(reasons))[:8]


def _company_item(record, *, deliverable, missing_channels=None):
    contacts = _contact_items(record)
    form = safe_url(record.get("contact_form_url"))
    channels = []
    if any(contact["kind"] == "email" for contact in contacts):
        channels.append("email")
    if any(contact["kind"] == "phone" for contact in contacts):
        channels.append("phone")
    if form:
        channels.append("contact_form")
    latest = _latest(record)
    item = {
        "company_id": _text(record.get("company_id"), 80),
        "company_name": _text(record.get("company_name"), 200) or "名称待核实",
        "country_code": _text(record.get("country_code"), 4),
        "region": _text(record.get("region"), 120),
        "city": _text(record.get("city"), 120),
        "industry_text": _text(record.get("industry_text"), 160),
        "description": _text(record.get("description"), 500),
        "products": [_text(value, 120) for value in (record.get("products") or [])[:6] if _text(value, 120)],
        "website_url": safe_url(record.get("website_url")),
        "domain": _text(record.get("domain"), 253),
        "status": "deliverable" if deliverable else "pending",
        "status_label": "已确认可交付" if deliverable else "待核实",
        "match_reasons": _match_reasons(record),
        "contacts": contacts,
        "contact_form_url": form,
        "channels": channels,
        "channel_labels": [CHANNEL_LABELS.get(name, name) for name in channels],
        "sources": _source_items(record),
        "pending_reasons": [] if deliverable else _pending_reasons(record, deliverable, missing_channels),
        "missing": [_text(value, 60) for value in (record.get("missing") or [])[:8] if _text(value, 60)],
        "confidence": record.get("confidence"),
        "lead_score": record.get("lead_score"),
        "observed_at": _iso(latest),
    }
    return item


_detail_cache: dict[str, dict] = {}
_detail_lock = threading.Lock()


def remember(run_id, signature, detail):
    """Keep the delivery selection a commit already computed; never recompute it."""
    with _detail_lock:
        _detail_cache[run_id] = {"signature": signature, "detail": detail}


def forget(run_id=None):
    with _detail_lock:
        if run_id is None:
            _detail_cache.clear()
        else:
            _detail_cache.pop(run_id, None)


def _cached(run_id, signature):
    with _detail_lock:
        value = _detail_cache.get(run_id)
    return value["detail"] if value and value.get("signature") == signature else None


def company_signature(session, run_id):
    """Cheap identity of the retained rows, used to validate any cached selection."""
    from sqlalchemy import func
    from app.persistence.models import Company
    count, latest, newest = session.execute(
        select(func.count(Company.id), func.max(Company.updated_at), func.max(Company.created_at))
        .where(Company.run_id == run_id)
    ).one()
    return "%s:%s:%s" % (count, latest.isoformat() if latest else "", newest.isoformat() if newest else "")


def _policy_inputs(run):
    from app.schemas import spec_version
    version = spec_version(run.spec_json)
    policy = ((run.spec_json.get("qualification_policy") or {}).get("contact_policy")
              if version == 2 else None)
    return version, policy, ("v2" if version == 2 else "legacy"), run.spec_json.get("freshness_days", 180)


def _select_lists(session, run, now):
    """One delivery pass plus one redacted qualified pass; never called per poll."""
    from app import runs
    from app.delivery.selection import delivery_companies, pending_companies
    rows = runs._rows(session, run)
    suppressions = runs._suppressions(session, run.spec_json)
    version, policy, identity, freshness = _policy_inputs(run)
    common = dict(suppressions=suppressions, freshness_days=freshness, now=now)
    deliverable_rows = delivery_companies(rows, policy=policy, identity_version=identity, **common)
    # 待办口径与 runs 进度投影共用同一实现（spec 2.6 §4.1C），不得各算一套。
    pending_rows = pending_companies(rows, deliverable_rows, identity_version=identity, **common)
    return deliverable_rows, pending_rows


def _scalars(session, run, now):
    """Header numbers that need no delivery recomputation."""
    from app import runs
    from app.persistence.models import Job
    states = Counter(session.scalars(select(Job.state).where(Job.run_id == run.id)))
    next_available = session.scalar(select(Job.available_at).where(Job.run_id == run.id, Job.state == "queued")
                                    .order_by(Job.available_at).limit(1))
    research = run.plan_json.get("research") or {}
    return {
        "admitted": int(research.get("admitted_count") or 0),
        "queued": states["queued"], "running": states["running"],
        "processed": states["completed"], "failed": states["failed"],
        "processing": bool(run.process_token and run.process_until and run.process_until > now),
        "input_open": bool(research.get("input_open")),
        "stop_reason": research.get("stop_reason"),
        "active_seconds": runs._elapsed(run, now),
        "contactable_count": research.get("contactable_count"),
        "discovery_seen": run.stats_json.get("discovery_seen"),
        "next_retry_at": _iso(next_available) if next_available and next_available > now else None,
    }


def _error_items(run):
    from app.capabilities import FAILURE_CATEGORIES
    items = []
    for error in (run.errors_json or [])[-3:]:
        category = error.get("category")
        items.append({"at": _text(error.get("at"), 40),
                      "code": _text(error.get("code"), 60),
                      "category": _text(category, 60),
                      "reason": FAILURE_CATEGORIES.get(category) or "原因未知",
                      "retryable": bool(error.get("retryable"))})
    return items


def build_projection(run_id, now=None, detail=None, reuse=True):
    """Pure read of committed state; never writes, fetches or mutates the batch."""
    from app.persistence.models import Run

    now = now or utcnow()
    with transaction() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise AppError("NOT_FOUND", "批次不存在", 404)
        version = _policy_inputs(run)[0]
        signature = company_signature(session, run.id)
        chosen = detail if detail is not None else _cached(run_id, signature)
        deliverable, pending = None, None
        deliverable_total = pending_total = 0
        if chosen:
            deliverable_rows, pending_rows = chosen["contactable"], chosen["pending"]
            deliverable_total, pending_total = len(deliverable_rows), len(pending_rows)
            deliverable = [_company_item(row, deliverable=True) for row in deliverable_rows[:ITEMS_PER_LIST]]
            pending = [_company_item(row, deliverable=False,
                                      missing_channels=(row.get("channel_policy") or {}).get("missing"))
                       for row in pending_rows[:ITEMS_PER_LIST]]
        else:
            # A pending marker means the last rebuild failed: never reuse a file
            # that is already known to be behind the committed state.
            existing = (read_projection(run_id)
                        if reuse and not pending_path(run_id).is_file() else None)
            if existing and existing.get("companies_signature") == signature:
                # Same retained rows: reuse the already mapped page instead of
                # running the delivery selection again for a status-only change.
                deliverable = existing.get("deliverables") or []
                pending = existing.get("pending") or []
                deliverable_total = int((existing.get("counts") or {}).get("deliverable") or len(deliverable))
                pending_total = int((existing.get("counts") or {}).get("pending") or len(pending))
            else:
                deliverable_rows, pending_rows = _select_lists(session, run, now)
                deliverable_total, pending_total = len(deliverable_rows), len(pending_rows)
                deliverable = [_company_item(row, deliverable=True) for row in deliverable_rows[:ITEMS_PER_LIST]]
                pending = [_company_item(row, deliverable=False,
                                          missing_channels=(row.get("channel_policy") or {}).get("missing"))
                           for row in pending_rows[:ITEMS_PER_LIST]]
                remember(run_id, signature, {"contactable": deliverable_rows, "pending": pending_rows})

        summary = _scalars(session, run, now)
        spec = run.spec_json
        plan = run.plan_json or {}
        research = plan.get("research") or {}
        circuit = (plan.get("execution") or {}).get("circuit") or {}
        status = run.status
        if status == "running" and circuit.get("open"):
            status = "blocked"
        target = int(spec["quantity"])
        counts = {
            "target": target,
            "admitted": summary["admitted"],
            "deliverable": deliverable_total,
            "pending": pending_total,
            "shortfall": max(0, target - deliverable_total),
            "exportable": min(deliverable_total, target),
            "discovered": None,
            "discovered_note": "暂不可得：本机核心不统计全网发现量",
            "submitted_candidates": summary.get("discovery_seen"),
        }
        core_contactable = summary.get("contactable_count")
        consistency = {"core_contactable": core_contactable,
                       "page_deliverable": deliverable_total,
                       "match": core_contactable is None or int(core_contactable) == deliverable_total}
        warnings = []
        if not consistency["match"]:
            warnings.append("页面可交付数与核心选择不一致；请刷新后重试，导出仍以核心结果为准")
        if pending_total > ITEMS_PER_LIST or deliverable_total > ITEMS_PER_LIST:
            warnings.append("结果超过单页展示上限，页面仅列出前 %d 家；导出仍按核心完整结果" % ITEMS_PER_LIST)
        if circuit.get("open"):
            warnings.append("连续失败已熔断：%s" % (circuit.get("last_code") or "原因未知"))
        projection = {
            "projection_version": PROJECTION_VERSION,
            "generated_at": _iso(now),
            "stale_after_seconds": STALE_AFTER_SECONDS,
            "companies_signature": signature,
            "run": {
                "id": run.id,
                "name": _text(spec.get("name"), 160) or run.id,
                "status": status,
                "status_label": STATUS_LABELS.get(status, "未知"),
                # Observation time is the last committed state change, not the
                # time this page happened to be rebuilt.
                "observed_at": _iso(run.updated_at),
                "version": run.version,
                "spec_version": version,
                "target": target,
                "industry": _text(spec.get("industry"), 160),
                "countries": [_text(value, 4) for value in (spec.get("countries") or [])[:20] if _text(value, 4)],
                "stop_reason": _text(research.get("stop_reason"), 60),
                "stop_reason_label": STOP_REASONS.get(research.get("stop_reason")),
                "processing": summary["processing"],
                "input_open": summary["input_open"],
                "active_seconds": round(float(summary["active_seconds"] or 0), 1),
                "started_at": _iso(run.created_at),
                "updated_at": _iso(run.updated_at),
                "finished_at": _iso(run.finished_at),
                "demo_only": bool(run.demo_only),
                "release": deepcopy(plan.get("release") or {}),
            },
            "counts": counts,
            "progress": {"queued": summary["queued"], "running": summary["running"],
                         "processed": summary["processed"], "failed": summary["failed"],
                         "candidate_limit": research.get("candidate_limit"),
                         "next_retry_at": summary["next_retry_at"]},
            "consistency": consistency,
            "warnings": warnings,
            "errors": _error_items(run),
            "filters": {"countries": sorted({item["country_code"] for item in deliverable + pending if item.get("country_code")}),
                        "channels": ["email", "phone", "contact_form"]},
            "deliverables": deliverable,
            "pending": pending,
            "notes": {
                "deliverable": "经现行交付选择、去重与抑制后，同时满足冻结客群及渠道政策的企业",
                "pending": "必要事实、主体或渠道仍待确认；不计入可交付，也不进入正式导出",
                "channel": "公开、来源可追溯不代表已实际验证可送达",
                "admitted": "核心已接收的去重前候选数；不是全网发现量",
            },
        }
    projection["revision"] = _revision(projection)
    return projection


def _revision(projection):
    payload = {key: value for key, value in projection.items() if key not in {"generated_at", "revision"}}
    # A running stopwatch is a clock, not a data change; otherwise every rebuild
    # would look different and defeat the no-change write skip and ETag.
    payload["run"] = {key: value for key, value in (payload.get("run") or {}).items()
                      if key != "active_seconds"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def write_projection(run_id, projection=None, reuse=True):
    """Atomically replace this run's projection; skip identical data rebuilds.

    A running stopwatch changes every second, so identity is the data revision,
    not the byte stream. When the revision is unchanged the file is left alone
    (no rewrite, no mtime churn); a low-frequency observation heartbeat refreshes
    only the generation time so a live status never ages into 'unknown'.
    """
    projection = projection or build_projection(run_id, reuse=reuse)
    payload = json.dumps(projection, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode()
    path = projection_path(run_id)
    heartbeat = False
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and existing.get("revision") == projection["revision"]:
            written = _parse(existing.get("generated_at"))
            age = (utcnow() - written).total_seconds() if written else None
            if age is not None and age < HEARTBEAT_SECONDS:
                _clear_pending(run_id)
                return {"changed": False, "skipped": True, "path": str(path),
                        "revision": projection["revision"], "generated_at": existing.get("generated_at")}
            heartbeat = True
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (".projection.%s.%s.tmp" % (os.getpid(), threading.get_ident()))
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _clear_pending(run_id)
    return {"changed": not heartbeat, "skipped": False, "heartbeat": heartbeat,
            "path": str(path), "revision": projection["revision"],
            "generated_at": projection["generated_at"], "bytes": len(payload)}


def _clear_pending(run_id):
    marker = pending_path(run_id)
    if marker.is_file():
        try:
            marker.unlink()
        except OSError:
            pass


def mark_pending(run_id, code, message="页面未反映最新变更"):
    """Record a failed rebuild so an old page is never presented as current."""
    try:
        path = pending_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"at": _iso(utcnow()), "code": str(code)[:80],
                              "message": message}, ensure_ascii=False).encode()
        temporary = path.parent / (".pending.%s.%s.tmp" % (os.getpid(), threading.get_ident()))
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        # The page still shows its own generation time; never abort research here.
        pass


def refresh(run_id, *, minimum_interval=0.0, force=False, reuse=True):
    """Rebuild at a safety checkpoint after the database commit, then replace."""
    now = utcnow().timestamp()
    with _write_lock:
        previous = _last_write.get(run_id)
        if not force and minimum_interval and previous and now - previous < minimum_interval:
            return {"skipped": True, "reason": "throttled", "run_id": run_id}
        try:
            result = write_projection(run_id, reuse=reuse)
            _last_write[run_id] = now
            return {**result, "skipped": False}
        except Exception as error:  # never break research for a page refresh
            mark_pending(run_id, getattr(error, "code", type(error).__name__))
            return {"error": getattr(error, "code", type(error).__name__), "run_id": run_id}


def refresh_safely(run_id, **kwargs):
    try:
        return refresh(run_id, **kwargs)
    except Exception:  # pragma: no cover - defensive boundary
        return {"error": "unavailable", "run_id": run_id}


def refresh_existing():
    """Rebuild every projection already on disk after a global delivery change."""
    root = (Path(settings.artifact_root).resolve() / "live")
    results = []
    if not root.is_dir():
        return results
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not _RUN_ID.fullmatch(entry.name) or ".." in entry.name:
            continue
        if not (entry / "projection.json").is_file():
            continue
        # Suppressions and expiries change eligibility without touching company
        # rows, so the retained file must never be treated as reusable here.
        results.append({"run_id": entry.name, **refresh(entry.name, force=True, reuse=False)})
    return results


def read_projection(run_id):
    """Return the last complete projection, or None for missing/corrupt files."""
    try:
        path = projection_path(run_id)
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AppError):
        return None
    if not isinstance(value, dict) or value.get("projection_version") != PROJECTION_VERSION:
        return None
    return value


def read_state(run_id, now=None):
    """Page-facing file metadata: existence, age and an unapplied-rebuild marker."""
    now = now or utcnow()
    projection = read_projection(run_id)
    marker = None
    try:
        path = pending_path(run_id)
        if path.is_file():
            marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        marker = {"at": None, "code": "UNREADABLE", "message": "页面可能过期"}
    generated = _parse((projection or {}).get("generated_at"))
    age = (now - generated).total_seconds() if generated else None
    return {"exists": projection is not None, "projection": projection, "pending": marker,
            "age_seconds": round(age, 1) if age is not None else None,
            "generated_at": (projection or {}).get("generated_at")}


def status_view(projection, now=None):
    """Never report a stale 'running' as current: age turns it into unknown."""
    now = now or utcnow()
    if not projection:
        return {"value": "unknown", "label": "未知", "observed_at": None,
                "stale": True, "note": "尚无已保存的页面数据"}
    run = projection.get("run") or {}
    value = run.get("status") or "unknown"
    generated = _parse(projection.get("generated_at"))
    threshold = int(projection.get("stale_after_seconds") or STALE_AFTER_SECONDS)
    age = (now - generated).total_seconds() if generated else None
    stale = bool(age is not None and age > threshold)
    if value == "running" and stale:
        return {"value": "unknown", "label": "未知", "observed_at": run.get("observed_at"),
                "stale": True, "note": "最后更新于 %s；此后没有新的已提交状态" % (projection.get("generated_at") or "未知")}
    return {"value": value, "label": STATUS_LABELS.get(value, "未知"),
            "observed_at": run.get("observed_at"), "stale": stale,
            "note": None if not stale else "数据可能滞后，最后更新于 %s" % (projection.get("generated_at") or "未知")}


def matches_filters(item, filters):
    """Mirror the core keyword/country/channel predicates for page previews.

    Export never trusts this helper: the server re-runs the core selection with
    the same filters, and the returned row count is authoritative.
    """
    filters = filters or {}
    query = _text(filters.get("q"), 200)
    if query:
        haystack = " ".join(str(item.get(field) or "") for field in
                            ("company_name", "domain", "country_code", "region", "city", "industry_text"))
        if query.casefold() not in haystack.casefold():
            return False
    countries = filters.get("countries") or filters.get("country")
    if countries:
        wanted = {str(value).strip().casefold() for value in ([countries] if isinstance(countries, str) else countries)}
        if str(item.get("country_code") or "").casefold() not in wanted:
            return False
    channels = filters.get("channels") or filters.get("channel")
    if channels:
        wanted = {str(value).strip().casefold() for value in ([channels] if isinstance(channels, str) else channels)}
        if not wanted & {str(value).casefold() for value in item.get("channels") or []}:
            return False
    return True


def filter_items(items, filters):
    return [item for item in items or [] if matches_filters(item, filters)]


def cleanup(retention_days=None):
    """Remove only known projection files of expired runs; never recurse blindly."""
    days = RETENTION_DAYS if retention_days is None else int(retention_days)
    root = (Path(settings.artifact_root).resolve() / "live")
    removed, failed = [], []
    if not root.is_dir():
        return {"removed": removed, "failed": failed}
    cutoff = utcnow().timestamp() - max(1, days) * 86400
    for entry in root.iterdir():
        if not entry.is_dir() or not _RUN_ID.fullmatch(entry.name) or ".." in entry.name:
            continue
        try:
            stamps = [path.stat().st_mtime for path in (entry / "projection.json", entry / "pending.json")
                      if path.is_file()]
            # Heartbeat writes update the file, not the directory: an active page
            # must not be expired just because its folder was created long ago.
            if not stamps or max(stamps) >= cutoff:
                continue
            for name in ("projection.json", "pending.json"):
                target = entry / name
                if target.is_file():
                    target.unlink()
            for leftover in entry.glob(".projection.*.tmp"):
                leftover.unlink(missing_ok=True)
            for leftover in entry.glob(".pending.*.tmp"):
                leftover.unlink(missing_ok=True)
            try:
                entry.rmdir()
            except OSError:
                # Unknown files stay: only our own projection files are removed.
                pass
            removed.append(entry.name)
        except OSError as error:
            failed.append({"run_id": entry.name, "error": type(error).__name__})
    return {"removed": removed, "failed": failed}
