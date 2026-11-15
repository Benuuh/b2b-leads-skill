"""Offline readable deliverable built from an existing 39-column export.

The readable workbook is derived from the export alone: no database, HTTP or
DNS.  The original export stays untouched as the evidence-facing file, and
every rating here is a deterministic function of the cells already exported.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

from app.errors import AppError
from app.rules.contacts import PERSONAL_DOMAINS, normalize_domain
from app.rules.customer_type import classify_customer_type

from .exporting import safe_cell
from .fields import HEADERS_ZH

READABLE_VERSION = 'readable-export-1'
HEADERS = ["序号", "评级", "客户性质", "企业名称", "品类", "州", "城市", "地址", "官网",
           "邮箱", "电话", "渠道", "线索分", "置信度", "备注"]
SOURCE_HEADERS = ["URL", "企业数", "来源条数", "采集时间"]
NOTE_HEADERS = ["项目", "内容"]
SHEETS = ("名单", "来源", "说明")
OFFICIAL_TYPES = {"official_site", "company_website"}
STATUS_LABELS = {"needs_review": "待核", "insufficient": "缺联系渠道", "excluded": "已排除"}
# Consumer/ISP mailboxes are personal channels, not a company-domain conflict.
PUBLIC_MAILBOX = set(PERSONAL_DOMAINS) | {
    "sbcglobal.net", "bellsouth.net", "att.net", "verizon.net", "verizon.com",
    "comcast.net", "cox.net", "charter.net", "roadrunner.com", "earthlink.net",
    "dslextreme.com", "dslextreme.net", "msn.com", "me.com", "mac.com", "ymail.com",
    "rocketmail.com", "googlemail.com", "aim.com", "juno.com", "gmx.com", "gmx.net",
    "protonmail.com", "mail.com", "yandex.com", "qq.com", "163.com", "126.com",
    "sina.com", "foxmail.com",
}
_ZH = {value: key for key, value in HEADERS_ZH.items()}
_FIELD = re.compile(r"[a-z][a-z0-9_]*")
_SUFFIX = None


def _text(value):
    if value is None:
        return ""
    text = str(value).strip()
    # Excel formula protection adds one apostrophe; restore the literal value.
    if len(text) >= 2 and text[0] == "'" and text[1] in "=+-@":
        return text[1:]
    return text


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _list(value):
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = _text(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return [text]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [text]


def _field(header):
    text = _text(header)
    if not text:
        return None
    if text in _ZH:
        return _ZH[text]
    return text if _FIELD.fullmatch(text) else None


def _rows(sheet):
    iterator = sheet.iter_rows(values_only=True)
    header = next(iterator, None)
    if header is None:
        return []
    fields = [_field(item) for item in header]
    records = []
    for values in iterator:
        if not values:
            continue
        record, filled = {}, False
        for field, value in zip(fields, values):
            if not field:
                continue
            record[field] = value
            if value not in (None, ""):
                filled = True
        if filled:
            records.append(record)
    return records


def _manifest(sheet):
    data = {}
    for key, value in sheet.iter_rows(min_row=2, values_only=True):
        if key in (None, ""):
            continue
        text = _text(value)
        if text[:1] in ("{", "["):
            try:
                text = json.loads(text)
            except ValueError:
                pass
        data[str(key)] = text
    return data


def _domain(value):
    text = _text(value)
    if not text:
        return ""
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    return normalize_domain(text)


def _suffix(host):
    global _SUFFIX
    if _SUFFIX is None:
        from tldextract import TLDExtract
        _SUFFIX = TLDExtract(suffix_list_urls=())
    return _SUFFIX(host)


def _registrable(host):
    record = _suffix(host)
    return ".".join(part for part in (record.domain, record.suffix) if part) or host


def _label(host):
    record = _suffix(host)
    return re.sub(r"[^a-z0-9]", "", (record.domain or "").casefold())


def _is_public(host):
    return bool(host) and _registrable(host) in PUBLIC_MAILBOX


def _conflicts(email, website):
    """True when the mailbox domain is demonstrably another company's domain.

    A different TLD or a brand prefix (``bluewaveplastics.net`` against
    ``bluewaveplastics.com``) is not treated as a conflict; ISP and consumer
    mailboxes are labelled as public instead.
    """
    mailbox, site = _domain(email), _domain(website)
    if not mailbox or not site or _is_public(mailbox):
        return False
    if _registrable(mailbox) == _registrable(site):
        return False
    left, right = _label(mailbox), _label(site)
    if not left or not right:
        return True
    if left == right:
        return False
    short, longer = sorted((left, right), key=len)
    return not (len(short) >= 6 and short in longer)


def _usable_emails(company, contacts):
    values = []
    main = _text(company.get("main_email"))
    if main:
        values.append(main)
    ranked = sorted(contacts, key=lambda item: (_number(item.get("priority_score")) or 0,
                                               _number(item.get("confidence")) or 0),
                    reverse=True)
    for contact in ranked:
        value = _text(contact.get("email"))
        if value and _text(contact.get("email_status")).casefold() != "invalid" \
                and not contact.get("opt_out") and value not in values:
            values.append(value)
    return values


def _usable_phones(company, contacts):
    values = []
    main = _text(company.get("main_phone"))
    if main:
        values.append(main)
    for contact in contacts:
        value = _text(contact.get("phone") or contact.get("phone_raw"))
        if value and _text(contact.get("phone_status")).casefold() != "invalid" and value not in values:
            values.append(value)
    return values


def _locality(company):
    region, city = _text(company.get("region")), _text(company.get("city"))
    address = _text(company.get("address"))
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if parts and re.fullmatch(r"[A-Z]{2}", parts[-1]):
        region = region or parts[-1]
        if not city and len(parts) >= 2:
            candidate = re.sub(r"^\d{3,6}(?:-\d{4})?\s+", "", parts[-2])
            candidate = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", candidate)
            if candidate and not candidate.isdigit():
                city = candidate
    return region, city


def _relevance(company):
    """High/medium/low from the exported qualification, never re-fetched."""
    score = _number(company.get("qualification_score"))
    if score is None:
        score = _number(company.get("confidence"))
    status = _text(company.get("status")).casefold()
    if status == "qualified" and score is not None and score >= 0.7:
        return "high"
    if status == "qualified" or (score is not None and score >= 0.5):
        return "medium"
    return "low"


def rating(relevance, email, phone, form, conflict):
    """A/B/C exactly as the readable contract states it."""
    channels = bool(email or phone or form)
    if conflict or relevance == "low" or not channels:
        return "C"
    if relevance == "high" and email:
        return "A"
    if relevance == "high" or email:
        return "B"
    return "C"


def _email_flags(email, contacts):
    """Verification and binding flags already present in the export."""
    for contact in contacts:
        if _text(contact.get("email")) == email:
            return {"status": _text(contact.get("email_status")).casefold(),
                    "binding": _text(contact.get("binding_status")).casefold(),
                    "role": bool(contact.get("is_role_account"))}
    return {}


def _notes(company, email, conflict, channels, contacts):
    notes = []
    if conflict:
        notes.append("邮箱域名与官网不一致")
    elif email and _is_public(_domain(email)):
        notes.append("公共邮箱")
    if email:
        flags = _email_flags(email, contacts)
        if flags.get("role"):
            notes.append("角色邮箱")
        if flags.get("binding") == "inferred":
            notes.append("邮箱绑定为推测")
        if flags.get("status") in {"", "unverified", "unknown", "not_checked"}:
            notes.append("邮箱未验证")
    if not email:
        notes.append("无邮箱")
    if not channels:
        notes.append("无联系渠道")
    binding = _text(company.get("website_binding_status")).casefold()
    if binding and binding != "confirmed":
        notes.append("官网归属未确认")
    status = _text(company.get("status")).casefold()
    if status and status != "qualified":
        notes.append("批次状态：" + STATUS_LABELS.get(status, status))
    return "；".join(notes)


def _entry(index, company, contacts, sources):
    website = _text(company.get("website_url")) or (
        "https://" + _text(company.get("domain")) if _text(company.get("domain")) else "")
    emails = _usable_emails(company, contacts)
    email = emails[0] if emails else ""
    conflict = any(_conflicts(item, website) for item in emails)
    phones = _usable_phones(company, contacts)
    form = _text(company.get("contact_form_url"))
    channels = [name for name, present in (("邮箱", bool(emails)), ("电话", bool(phones)),
                                           ("表单", bool(form))) if present]
    relevance = _relevance(company)
    models = _list(company.get("business_models"))
    classification = classify_customer_type(
        name=_text(company.get("company_name")), business_models=models,
        text=" ".join([_text(company.get("company_type")), _text(company.get("products")),
                       _text(company.get("description")), _text(company.get("industry_text")),
                       " ".join(_text(item.get("source_excerpt")) for item in sources
                                if _text(item.get("source_type")).casefold() in OFFICIAL_TYPES)]))
    category = _text(company.get("industry_text")) or "; ".join(_list(company.get("products")))
    region, city = _locality(company)
    return {
        "序号": index,
        "评级": rating(relevance, email, phones, form, conflict),
        "相关度": relevance,  # 内部计数用，不写入 15 列
        "客户性质": classification["type"],
        "企业名称": _text(company.get("company_name")),
        "品类": category,
        "州": region,
        "城市": city,
        "地址": _text(company.get("address")),
        "官网": website,
        "邮箱": email,
        "电话": phones[0] if phones else "",
        "渠道": "、".join(channels) if channels else "无",
        "线索分": round(_number(company.get("lead_score")) or 0.0, 3),
        "置信度": round(_number(company.get("qualification_score"))
                        if _number(company.get("qualification_score")) is not None
                        else (_number(company.get("confidence")) or 0.0), 3),
        "备注": _notes(company, email, conflict, channels, contacts),
    }


# The exporter attaches framework rows (status, scores, counters) to the
# company's primary page.  They are not evidence that the page produced a fact,
# so the source ledger counts only fact rows.
FRAMEWORK_FIELDS = {
    "status", "lead_score", "priority_score", "confidence", "confidence_components",
    "source_count", "email_count", "phone_count", "named_contact_count", "contactable",
    "qualification_score", "missing", "conflicts", "last_verified_at", "score_breakdown",
    "tags", "inferred", "opt_out", "binding_status", "contact_priority", "retrieved_at",
    "contact_id", "company_id", "form_fields", "email_status", "mx_status", "phone_status",
    "syntax_status", "catch_all_status", "smtp_status", "disposable_status", "is_role_account",
    "line_type", "email_type", "phone_type", "source_id", "main_email", "main_phone",
}


def _source_rows(sources, owners=None):
    owners = owners or {}
    groups = {}
    for row in sources:
        url = _text(row.get("source_url"))
        if not url:
            continue
        item = groups.setdefault(url, {"companies": set(), "rows": 0, "retrieved_at": ""})
        entity = _text(row.get("entity_id"))
        company = owners.get(entity, entity)
        if company:
            item["companies"].add(company)
        if _text(row.get("field")) not in FRAMEWORK_FIELDS:
            item["rows"] += 1
        when = _text(row.get("retrieved_at"))
        if when > item["retrieved_at"]:
            item["retrieved_at"] = when
    rows = [{"URL": url, "企业数": len(item["companies"]), "来源条数": item["rows"],
             "采集时间": item["retrieved_at"]}
            for url, item in groups.items()]
    return sorted(rows, key=lambda row: (-row["企业数"], -row["来源条数"], row["URL"]))


_PHONE = re.compile(r"\+?[\d\s().-]{6,}")


def _cell_for(header, value):
    """Write readable cells; phone numbers stay plain text instead of ``'+...``."""
    if header == "电话":
        text = _text(value)
        return text if _PHONE.fullmatch(text) else safe_cell(text)
    return safe_cell(value)


def _duration(seconds):
    if seconds is None:
        return ""
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    return f"{int(seconds // 60)} 分 {seconds % 60:.1f} 秒（{seconds:.1f} 秒）"


def _report_value(value):
    return '未取到' if value is None else str(value)


def _cost_rows(manifest, summary):
    """Prefer the P1-5 run report; fall back to the manifest's wall clock."""
    report = manifest.get('run_report')
    if isinstance(report, dict):
        estimate = report.get('estimate') or {}
        parts = [report.get('summary_text') or '']
        if report.get('active_seconds') is not None:
            parts.append(f"活动 {report['active_seconds']} 秒、"
                         f"暂停 {_report_value(report.get('paused_seconds'))} 秒")
        parts.append(
            f"页 {_report_value(report.get('pages'))}、候选 {_report_value(report.get('candidates'))}、"
            f"准入 {_report_value(report.get('admitted'))}、合格 {_report_value(report.get('qualified'))}、"
            f"可交付 {_report_value(report.get('contactable'))}、导出 {_report_value(report.get('exported'))}")
        if estimate.get('method'):
            parts.append(f"估算方法 {estimate['method']}（粗估，未校准）")
        if report.get('path'):
            parts.append('报告：' + str(report['path']))
        return [("执行花费", '；'.join(part for part in parts if part))]
    return [("实际用时", ("批次活动墙钟：" + _duration(summary.get("active_wall_seconds")))
             if summary.get("active_wall_seconds") is not None
             else "未取到（导出清单没有活动墙钟字段）"),
            ("token", "未取到（运行环境未提供；未用估算冒充实测）")]


def _share(count, total):
    return f"{count / total * 100:.0f}%" if total else "0%"


def _notes_sheet(manifest, entries, source_rows, elapsed):
    summary = manifest.get("research_summary") if isinstance(manifest.get("research_summary"), dict) else {}
    runtime = manifest.get("runtime_release") if isinstance(manifest.get("runtime_release"), dict) else {}
    policy = manifest.get("policy") if isinstance(manifest.get("policy"), dict) else {}
    counts = manifest.get("row_counts") if isinstance(manifest.get("row_counts"), dict) else {}
    ratings = {grade: sum(entry["评级"] == grade for entry in entries) for grade in "ABC"}
    high_relevance = sum(entry.get("相关度") == "high" for entry in entries)
    channel_covered = sum(entry.get("渠道") not in (None, "", "无") for entry in entries)
    return [
        ("口径", "阅读版由已有 39 列导出离线生成，不重新取证、不连数据库、不发请求；"
                 "原始导出保留为证据核对文件。列：序号、评级、客户性质、企业名称、品类、州、城市、"
                 "地址、官网、邮箱、电话、渠道、线索分、置信度、备注。行按评级 A→B→C、同级按线索分降序。"),
        ("评级口径", "A＝高相关且有邮箱且邮箱域名与官网一致；B＝高相关但仅有电话/表单，"
                     "或中等相关且有邮箱；C＝低相关、无任何渠道、任一邮箱域名与官网不一致，"
                     "或中等相关且无邮箱。"),
        ("相关度口径", "高＝批次状态 qualified 且资格证据置信度≥0.70；中＝qualified 但分数更低或缺失，"
                       "或置信度≥0.50；低＝其余（待核、排除或无分数）。"),
        ("客户性质口径", "优先 business.models 证据；本工作簿无该字段时按企业名称与官网摘录关键词归类。"
                         "仅作阅读辅助，不参与资格、评分或计数。"),
        ("渠道口径", "邮箱＝企业邮箱或联系人邮箱；电话＝企业电话或联系人电话；表单＝公开联系表单。"
                     "任一存在即计入渠道，未验证可达。"),
        ("联系方式未验证", "邮箱、电话均未做可达性验证；公共邮箱只标注，不按域名冲突降级。"
                           "仅用于合法 B2B 联系，投放前请人工确认。"),
        ("州/城市推断", "原表缺少州/城市时，仅按美国地址的两位州码与门牌后缀推断；原文保留在地址列。"),
        ("数据来源", f"输入工作簿：{manifest.get('_input_name') or '未记录'}；批次：{manifest.get('batch_id') or '未记录'}；"
                     f"生成时间：{manifest.get('generated_at') or '未记录'}；"
                     f"原始行数：企业 {counts.get('companies', len(entries))}、"
                     f"联系人 {counts.get('contacts', '未记录')}、来源 {counts.get('sources', len(source_rows))}。"),
        ("本次交付", f"企业 {len(entries)} 家；评级 A {ratings['A']}、B {ratings['B']}、C {ratings['C']}；"
                     f"高相关 {high_relevance} 家（{_share(high_relevance, len(entries))}）；"
                     f"渠道覆盖 {_share(channel_covered, len(entries))}；"
                     f"来源网址 {len(source_rows)} 条。"),
        *_cost_rows(manifest, summary),
        ("限制", "目录与搜索摘要只作发现线索；官网归属、邮箱与电话未验证；公共邮箱、缺州/城市、"
                 "缺失官网的企业只能标注不能修复。来源页的「来源条数」只计事实型条目，"
                 "不含状态、评分、计数等框架行。"),
        ("版本", f"工具 {manifest.get('tool_version') or '未记录'}；核心 {runtime.get('core_version') or '未记录'}；"
                 f"Skill {runtime.get('skill_version') or '未记录'}；规则 {policy.get('rule_version') or '未记录'}；"
                 f"评分规则 {manifest.get('scoring_rule_version') or '未记录'}；阅读版 {READABLE_VERSION}；"
                 f"本地生成耗时 {elapsed:.3f} 秒。"),
    ]


def _write(output, entries, source_rows, notes):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "名单"
    sheet.append(HEADERS)
    for entry in entries:
        sheet.append([_cell_for(header, entry[header]) for header in HEADERS])
    widths = [6, 6, 12, 34, 20, 8, 14, 34, 34, 30, 18, 12, 10, 10, 44]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width
    for sheet_name, headers, rows in (("来源", SOURCE_HEADERS, source_rows),
                                      ("说明", NOTE_HEADERS, notes)):
        current = workbook.create_sheet(sheet_name)
        current.append(headers)
        for row in rows:
            current.append([safe_cell(row[header]) for header in headers] if isinstance(row, dict)
                           else [safe_cell(item) for item in row])
        current.column_dimensions["A"].width = 34 if sheet_name == "来源" else 18
        current.column_dimensions["B"].width = 14
        if sheet_name == "来源":
            current.column_dimensions["C"].width = 12
            current.column_dimensions["D"].width = 26
        else:
            current.column_dimensions["B"].width = 110
    for current in workbook.worksheets:
        for cell in current[1]:
            cell.font = Font(bold=True)
        current.freeze_panes = "A2"
    for row in workbook["说明"].iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)


def export_readable(input_path, output_path):
    """Build the readable workbook; returns a small offline summary."""
    began = time.perf_counter()
    source_path, target_path = Path(input_path), Path(output_path)
    if not source_path.is_file():
        raise AppError("INPUT_MISSING", f"找不到输入工作簿：{source_path}")
    if source_path.resolve() == target_path.resolve():
        raise AppError("INVALID_INPUT", "输出文件不能覆盖输入工作簿")
    if source_path.suffix.casefold() != ".xlsx":
        raise AppError("INVALID_INPUT", "readable-export 只接受 xlsx 导出")
    workbook = load_workbook(source_path, read_only=True, data_only=True)
    try:
        if "Companies" not in workbook.sheetnames:
            raise AppError("INVALID_INPUT", "输入工作簿缺少 Companies 表，无法生成阅读版")
        companies = _rows(workbook["Companies"])
        contacts = _rows(workbook["Contacts"]) if "Contacts" in workbook.sheetnames else []
        sources = _rows(workbook["Sources"]) if "Sources" in workbook.sheetnames else []
        manifest = _manifest(workbook["Manifest"]) if "Manifest" in workbook.sheetnames else {}
    finally:
        workbook.close()
    if companies and not any(_text(item.get("company_name")) for item in companies):
        raise AppError("INVALID_INPUT", "输入工作簿 Companies 表没有可识别的企业名称列")
    by_company, by_entity = {}, {}
    for contact in contacts:
        by_company.setdefault(_text(contact.get("company_id")), []).append(contact)
    owners = {_text(contact.get("contact_id")): _text(contact.get("company_id"))
              for contact in contacts if _text(contact.get("contact_id"))}
    for source in sources:
        by_entity.setdefault(_text(source.get("entity_id")), []).append(source)
    entries = []
    for company in companies:
        key = _text(company.get("company_id"))
        entries.append(_entry(0, company, by_company.get(key, []), by_entity.get(key, [])))
    entries.sort(key=lambda entry: ({"A": 0, "B": 1, "C": 2}[entry["评级"]],
                                    -float(entry["线索分"] or 0)))
    for index, entry in enumerate(entries, 1):
        entry["序号"] = index
    source_rows = _source_rows(sources, owners)
    manifest["_input_name"] = source_path.name
    elapsed = time.perf_counter() - began
    _write(target_path, entries, source_rows,
           _notes_sheet(manifest, entries, source_rows, elapsed))
    return {"input": str(source_path), "output": str(target_path),
            "companies": len(entries),
            "ratings": {grade: sum(entry["评级"] == grade for entry in entries) for grade in "ABC"},
            "sources": len(source_rows), "sheets": list(SHEETS),
            "elapsed_seconds": round(elapsed, 3),
            "network_requests": 0, "database_writes": 0}
