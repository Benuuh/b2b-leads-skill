"""Encode the common selected enterprise projection as Excel or CSV/manifest.

The public build_export adapter retains the CLI contract. Selection and all
business predicates live in selection.py, while metrics.py owns calculations.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import csv
import io
import json
import re
import zipfile

from ..rules.ranking import SCORING_RULE_VERSION
from .fields import COMPANY_FIELDS, CONTACT_FIELDS, SOURCE_FIELDS, EMAIL_CHANNEL_FIELDS, HEADERS_ZH
from .metrics import calculate_metrics
from .selection import DELIVERY_POLICY_VERSION, observation_time, select_export_companies


def safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        value = json.dumps(list(value) if isinstance(value, set) else value, ensure_ascii=False, sort_keys=True, default=str)
    if isinstance(value, str):
        # Excel ignores leading whitespace/control bytes before a formula sigil.
        if value.lstrip(" \t\r\n\v\f").startswith(("=", "+", "-", "@")):
            return "'" + value
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return value


def _field_list(name, defaults, fields):
    required = {"Companies": ["company_id"], "Contacts": ["contact_id", "company_id"],
                "Sources": ["entity_id", "field", "value", "source_type", "source_url", "retrieved_at", "method"]}[name]
    def companions(selected):
        if name == "Contacts":
            if "email" in selected:
                selected += [field for field in EMAIL_CHANNEL_FIELDS if field not in selected]
                selected += [field for field in ("inferred", "tags", "confidence", "contact_priority", "priority_score", "source_urls", "retrieved_at") if field not in selected]
            if "phone" in selected or "phone_raw" in selected:
                selected += [field for field in ("phone_status", "phone_type", "line_type", "tags", "confidence", "contact_priority", "priority_score", "source_urls", "retrieved_at") if field not in selected]
            if "contact_name" in selected:
                selected += [field for field in ("tags", "confidence", "source_urls", "retrieved_at") if field not in selected]
        return selected
    if not fields:
        return list(defaults)
    if isinstance(fields, list):
        chosen = [field for field in fields if field in defaults]
        # A flat company field selection should not erase every other sheet.
        return companions(list(dict.fromkeys(required + chosen))) if chosen else list(defaults)
    selected = fields.get(name, fields.get(name.lower(), None))
    if selected is None:
        return list(defaults)
    if not isinstance(selected, list) or not selected or any(field not in defaults for field in selected):
        raise ValueError("Invalid export field selection: " + name)
    return companions(list(dict.fromkeys(required + selected)))


def build_export(companies: list, manifest: dict, *, format="xlsx", mode="balanced", filters=None,
                 fields=None, language="en", suppressions=None, metrics_context=None, now=None,
                 delivery_only=False, delivery_limit=None, policy=None,
                 identity_version="legacy", channels=None, selected=None,
                 eligible_delivery=None) -> tuple[bytes, str, str, dict]:
    if manifest.get("demo_only") or any(company.get("demo_only") for company in companies):
        raise ValueError("演示批次不能作为正式获客名单导出")
    current = now or (metrics_context or {}).get("as_of") or manifest.get("generated_at") or datetime.now(timezone.utc)
    if selected is None:
        selected, eligible_delivery = select_export_companies(companies, mode=mode, filters=filters,
            suppressions=suppressions, freshness_days=manifest.get("freshness_days", 180), now=current,
            delivery_only=delivery_only, delivery_limit=delivery_limit, policy=policy,
            identity_version=identity_version, channels=channels)
    if format not in {"xlsx", "csv", "zip"}:
        raise ValueError("Export format must be xlsx or csv")
    headers = HEADERS_ZH if language.lower().startswith("zh") else {}
    company_fields = _field_list("Companies", COMPANY_FIELDS, fields)
    contact_fields = _field_list("Contacts", CONTACT_FIELDS, fields)
    source_fields = _field_list("Sources", SOURCE_FIELDS + (["form_fields"] if delivery_only else []), fields)
    if delivery_only and "form_fields" not in source_fields:
        source_fields.append("form_fields")
    contacts = [c for company in selected for c in company.get("contacts", [])]
    if len(selected) + len(contacts) > 10000:
        raise ValueError("单次导出企业和联系人合计不能超过10000行，请缩小筛选范围")
    company_ids = {co["company_id"] for co in selected}
    sources = [s for company in selected for s in company.get("sources", [])
               if s.get("field") in (company_fields if s.get("entity_id") in company_ids else contact_fields)]
    source_stats = Counter(s.get("source_type", "unknown") for s in sources)
    denominator = len(selected)
    def coverage(predicate):
        return round(sum(predicate(company) for company in selected) / denominator, 4) if denominator else 0
    metrics = calculate_metrics(companies, channels={"output": selected, "output_sources": sources, "output_scope": "export_nonexcluded_enterprises",
        "export_fields": {"Companies": company_fields, "Contacts": contact_fields}}, context={**(metrics_context or {}),
        "as_of": current, "mode": mode, "filters": filters or {}, "suppressions": suppressions or [],
        "freshness_days": manifest.get("freshness_days", 180)})
    completed = {**deepcopy(manifest), "generated_at": observation_time(current).isoformat().replace("+00:00", "Z"),
                 "tool_version": manifest.get("tool_version", "b2b-lead-gen/1.0"), "mode": mode, "format": "xlsx" if format == "xlsx" else "csv",
                 "scoring_rule_version": SCORING_RULE_VERSION,
                 "filters": deepcopy(filters or {}), "fields": {"Companies": company_fields, "Contacts": contact_fields, "Sources": source_fields},
                 "language": language, "row_counts": {"companies": len(selected), "contacts": len(contacts), "sources": len(sources)},
                 "coverage": {"contact": coverage(lambda co: any(c.get("email") or c.get("phone") for c in co["contacts"])),
                              "email": coverage(lambda co: any(c.get("email") for c in co["contacts"])),
                              "phone": coverage(lambda co: any(c.get("phone") for c in co["contacts"])),
                              "named_contact": coverage(lambda co: any(c.get("contact_name") for c in co["contacts"]))},
                 "source_statistics": dict(source_stats),
                 "coverage_semantics": "legacy_output_nonempty_ratio; use metrics.batch_metrics.coverage for business acceptance",
                 "metrics": metrics,
                 "warnings": list(manifest.get("warnings", [])) + (["个人商务数据仅用于合法 B2B 联系；推测和未验证邮箱不代表可达；过期数据已标记 stale。"] if any(c.get("inferred") or c.get("email_status") in {"unverified", "unknown", "risky"} or set(c.get("tags", [])) & {"personal_business", "stale"} for c in contacts) else [])}
    # Common aliases keep consumers simple while row_counts remains authoritative.
    if delivery_only:
        completed["delivery"] = {"policy_version": DELIVERY_POLICY_VERSION,
            "eligible_count": eligible_delivery, "exported_count": len(selected), "limit": delivery_limit,
            "channel_filter": list(channels) if channels else None,
            "channels": {channel: sum(channel in company.get("delivery_channels", []) for company in selected)
                         for channel in ("email", "phone", "contact_form")},
            "semantics": "qualified_unique_enterprises_with_fresh_observed_source_backed_public_channels; not_delivery_guaranteed"}
    completed["counts"] = completed["row_counts"]
    sheets = [("Companies", selected, company_fields), ("Contacts", contacts, contact_fields), ("Sources", sources, source_fields)]
    batch_name = re.sub(r"[^a-zA-Z0-9_-]", "_", str(manifest.get("batch_id") or "leads"))[:80]
    if format == "xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        workbook = Workbook()
        workbook.remove(workbook.active)
        for name, rows, columns in sheets:
            sheet = workbook.create_sheet(name)
            sheet.append([headers.get(column, column) for column in columns])
            for row in rows:
                sheet.append([safe_cell(row.get(column)) for column in columns])
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="183E54")
            for index, column in enumerate(columns, 1):
                sheet.column_dimensions[get_column_letter(index)].width = min(55, max(18, len(headers.get(column, column)) + 4))
        sheet = workbook.create_sheet("Manifest")
        sheet.append(["项目" if headers else "key", "内容" if headers else "value"])
        for key, value in completed.items():
            sheet.append([safe_cell(key), safe_cell(value)])
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 28
        sheet.column_dimensions["B"].width = 100
        stream = io.BytesIO()
        workbook.save(stream)
        return stream.getvalue(), f"b2b_{batch_name}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", completed
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rows, columns in sheets:
            text = io.StringIO(newline="")
            writer = csv.writer(text, lineterminator="\r\n")
            writer.writerow([headers.get(column, column) for column in columns])
            writer.writerows([safe_cell(row.get(column)) for column in columns] for row in rows)
            archive.writestr(name.lower() + ".csv", text.getvalue().encode("utf-8-sig"))
        archive.writestr("manifest.json", json.dumps(completed, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
    return stream.getvalue(), f"b2b_{batch_name}.zip", "application/zip", completed
