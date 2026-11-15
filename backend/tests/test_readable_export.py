"""Offline readable export: fixed workbook fixture, ratings and no I/O.

The fixture is produced by the real 39-column exporter, so the reader is tested
against the same Chinese headers, sheet names and formula protection a run
writes.
"""
import argparse
import io
import time

import pytest
from openpyxl import Workbook, load_workbook

from app import cli
from app.delivery.exporting import build_export
from app.delivery.readable import HEADERS, SOURCE_HEADERS, export_readable
from app.errors import AppError

NOW = "2026-11-14T00:00:00Z"
MANIFEST = {
    "batch_id": "fixture-batch",
    "tool_version": "skill-v2.8.0",
    "runtime_release": {"core_version": "2.8.0", "skill_version": "2.8.0"},
    "research_summary": {"active_wall_seconds": 271.5},
    "policy": {"rule_version": "b2b-policy-3"},
    "freshness_days": 180,
}


def company(key, name, **overrides):
    record = {
        "company_id": "co_" + key, "company_name": name, "domain": key + ".com",
        "website_url": "https://" + key + ".com", "country_code": "US",
        "industry_text": "outdoor furniture", "status": "qualified",
        "qualification_score": 0.77, "confidence": 0.7, "lead_score": 60.0,
        "website_binding_status": "confirmed", "missing": [], "conflicts": [],
        "qualification": {"decision": "qualified", "confidence": 0.77, "independent_sources": 2},
        "contacts": [],
        "sources": [{"entity_id": "co_" + key, "field": "company_name", "value": name,
                     "source_url": "https://directory.example.com/outdoor/" + key,
                     "source_type": "directory", "retrieved_at": NOW, "method": "discovered"}],
    }
    record.update(overrides)
    return record


def contact(key, **overrides):
    return {"contact_id": "ct_" + key, "company_id": "co_" + key, "contact_name": None,
            "email": "", "email_status": "unverified", "mx_status": "unknown",
            "phone": "", "phone_status": None, "priority_score": 40, "confidence": 0.6,
            "inferred": False, "opt_out": False, "retrieved_at": NOW,
            "source_urls": ["https://site.example.com/" + key + "/contact"],
            "tags": ["public_business"], **overrides}


def records():
    return [
        company("alpinepatiofurniture", "Alpine Patio Furniture",
                region="TX", city="Austin", address="1 Congress Ave, Austin, TX 78701",
                lead_score=82.5,
                contacts=[contact("alpinepatiofurniture", email="sales@alpinepatiofurniture.com",
                                  phone="+15125550111")],
                sources=[
                    {"entity_id": "co_alpinepatiofurniture", "field": "company_name",
                     "value": "Alpine Patio Furniture",
                     "source_url": "https://directory.example.com/outdoor",
                     "source_type": "directory", "retrieved_at": NOW, "method": "discovered"},
                    {"entity_id": "co_alpinepatiofurniture", "field": "website_url",
                     "value": "https://alpinepatiofurniture.com",
                     "source_url": "https://directory.example.com/outdoor",
                     "source_type": "directory", "retrieved_at": NOW, "method": "discovered"}]),
        company("gardenthumbgc", "Green Thumb Garden Center",
                address="2 Nursery Lane, 78704 Austin, TX", lead_score=80.0,
                sources=[{"entity_id": "co_gardenthumbgc", "field": "company_name",
                          "value": "Green Thumb Garden Center",
                          "source_url": "https://directory.example.com/outdoor",
                          "source_type": "directory", "retrieved_at": NOW, "method": "discovered"}],
                contacts=[contact("gardenthumbgc", email="hello@gardenthumbgc.com")]),
        company("comfypatio", "Comfy Patio", domain="comfypatio.com",
                website_url="https://comfypatio.com", lead_score=70.0,
                contacts=[contact("comfypatio", email="anna@comfypatio.com", priority_score=70)]),
        company("jollypolly", "Jolly Polly", lead_score=65.0,
                contacts=[contact("jollypolly", email="jollypolly@gmail.com")]),
        company("sunbeltpartyrental", "Sunbelt Party Rental", lead_score=60.0,
                contacts=[contact("sunbeltpartyrental", phone="+15125550222")]),
        company("acmeoutdoor", "Acme Outdoor", website_url="https://acmeoutdoor.com",
                domain="acmeoutdoor.com", lead_score=55.0,
                contacts=[contact("acmeoutdoor", email="bob@other-company.com")],
                sources=[{"entity_id": "co_acmeoutdoor", "field": "company_name",
                          "value": "Acme Outdoor",
                          "source_url": "https://other-directory.example.com/list",
                          "source_type": "directory", "retrieved_at": NOW, "method": "discovered"}]),
        company("silentpatio", "Silent Patio", lead_score=50.0),
        company("patioplace", "Patio Place", status="needs_review", qualification_score=0.35,
                qualification={"decision": "uncertain", "confidence": 0.35, "independent_sources": 0},
                lead_score=40.0,
                contacts=[contact("patioplace", email="sales@patioplace.com")]),
    ]


def fixture(path, manifest=None):
    data, _, _, _ = build_export(records(), dict(manifest or MANIFEST), format="xlsx", language="zh")
    path.write_bytes(data)
    return path


def table(output):
    rows = list(load_workbook(output)["名单"].iter_rows(values_only=True))
    assert list(rows[0]) == HEADERS
    assert all(len(row) == len(HEADERS) for row in rows)
    return [dict(zip(HEADERS, row)) for row in rows[1:]]


def test_readable_columns_ratings_customer_type_and_notes(tmp_path):
    source, output = fixture(tmp_path / "export.xlsx"), tmp_path / "readable.xlsx"
    result = export_readable(source, output)
    assert result["ratings"] == {"A": 4, "B": 1, "C": 3}
    assert result["companies"] == 8 and result["network_requests"] == 0
    assert result["database_writes"] == 0
    rows = table(output)

    assert [row["序号"] for row in rows] == list(range(1, 9))
    grades = [row["评级"] for row in rows]
    assert grades == sorted(grades, key="ABC".index)
    assert grades[:4] == ["A"] * 4

    by_name = {row["企业名称"]: row for row in rows}
    alpine = by_name["Alpine Patio Furniture"]
    assert alpine["评级"] == "A" and alpine["客户性质"] == "零售/门店"
    assert alpine["州"] == "TX" and alpine["城市"] == "Austin"
    assert alpine["电话"] == "+15125550111"  # formula protection is removed, not shown
    assert alpine["渠道"] == "邮箱、电话"
    assert alpine["官网"] == "https://alpinepatiofurniture.com"

    garden = by_name["Green Thumb Garden Center"]
    assert garden["评级"] == "A" and garden["客户性质"] == "园艺中心"
    # Region and city are inferred from the exported US address, which stays intact.
    assert garden["州"] == "TX" and garden["城市"] == "Austin"
    assert garden["地址"] == "2 Nursery Lane, 78704 Austin, TX"

    assert by_name["Comfy Patio"]["评级"] == "A"  # contact mailbox is a usable email
    assert by_name["Comfy Patio"]["邮箱"] == "anna@comfypatio.com"
    assert by_name["Jolly Polly"]["评级"] == "A"
    assert by_name["Jolly Polly"]["备注"] == "公共邮箱；邮箱未验证"
    assert by_name["Sunbelt Party Rental"]["评级"] == "B"
    assert by_name["Sunbelt Party Rental"]["客户性质"] == "租赁/活动"
    assert by_name["Sunbelt Party Rental"]["渠道"] == "电话"
    assert by_name["Sunbelt Party Rental"]["备注"] == "无邮箱"

    acme = by_name["Acme Outdoor"]
    assert acme["评级"] == "C" and "邮箱域名与官网不一致" in acme["备注"]
    assert by_name["Patio Place"]["评级"] == "C"
    silent = by_name["Silent Patio"]
    assert silent["评级"] == "C" and silent["渠道"] == "无"
    assert silent["备注"] == "无邮箱；无联系渠道"

    sources = list(load_workbook(output)["来源"].iter_rows(values_only=True))
    assert list(sources[0]) == SOURCE_HEADERS
    first = dict(zip(SOURCE_HEADERS, sources[1]))
    assert first["URL"] == "https://directory.example.com/outdoor"
    assert first["企业数"] == 2 and first["来源条数"] == 3
    assert first["采集时间"] == NOW

    notes = {row[0]: row[1] for row in load_workbook(output)["说明"].iter_rows(min_row=2, values_only=True)}
    assert "未做可达性验证" in notes["联系方式未验证"]
    assert notes["token"].startswith("未取到")
    assert "4 分 31.5 秒" in notes["实际用时"]
    assert "b2b-policy-3" in notes["版本"] and "readable-export-1" in notes["版本"]
    assert "fixture-batch" in notes["数据来源"] and "export.xlsx" in notes["数据来源"]
    assert "A 4、B 1、C 3" in notes["本次交付"]
    assert "高相关 7 家（88%）" in notes["本次交付"]
    assert "渠道覆盖 88%" in notes["本次交付"]


def test_notes_sheet_prefers_the_run_report(tmp_path):
    report = {
        "report_version": "run-report-1",
        "summary_text": "总用时 4 分 0.0 秒；总 token 未取到（运行环境未提供；未用估算冒充实测）",
        "active_seconds": 240.0, "paused_seconds": 0.0, "pages": 12, "candidates": 8,
        "admitted": 8, "qualified": 7, "contactable": 6, "exported": 6,
        "estimate": {"method": "per_company", "label": "逐家官网取证"},
        "path": "D:/data/skill-v2/reports/run-fixture-batch.json",
    }
    source = fixture(tmp_path / "export.xlsx", {**MANIFEST, "run_report": report})
    output = tmp_path / "readable.xlsx"
    export_readable(source, output)
    notes = {row[0]: row[1] for row in load_workbook(output)["说明"].iter_rows(min_row=2, values_only=True)}
    assert notes["执行花费"].startswith("总用时 4 分")
    assert "per_company" in notes["执行花费"]
    assert "run-fixture-batch.json" in notes["执行花费"]
    assert "实际用时" not in notes and "token" not in notes


def test_contact_quality_flags_are_marked(tmp_path):
    record = company("quality", "Quality Patio", contacts=[
        contact("quality", email="info@qualitypatio.com", is_role_account=True,
                binding_status="inferred")])
    data, _, _, _ = build_export([record], dict(MANIFEST), format="xlsx", language="zh")
    source = tmp_path / "export.xlsx"
    source.write_bytes(data)
    output = tmp_path / "readable.xlsx"
    export_readable(source, output)
    row = table(output)[0]
    assert row["评级"] == "A"
    assert row["备注"] == "角色邮箱；邮箱绑定为推测；邮箱未验证"


def test_cli_readable_export_never_prepares_the_database(tmp_path, monkeypatch):
    source, output = fixture(tmp_path / "export.xlsx"), tmp_path / "readable.xlsx"
    import app.persistence as persistence

    def explode(*_args, **_kwargs):
        raise AssertionError("readable-export must not touch PostgreSQL")

    monkeypatch.setattr(persistence, "engine", explode)
    monkeypatch.setattr("app.runtime.prepare", explode)
    result = cli.dispatch(argparse.Namespace(command="readable-export", input=source, output=output))
    assert result["companies"] == 8 and result["database_writes"] == 0
    assert output.is_file()


def test_english_headers_are_read_too(tmp_path):
    data, _, _, _ = build_export(records(), dict(MANIFEST), format="xlsx", language="en")
    source = tmp_path / "export-en.xlsx"
    source.write_bytes(data)
    output = tmp_path / "readable.xlsx"
    export_readable(source, output)
    rows = table(output)
    assert {row["企业名称"] for row in rows} == {record["company_name"] for record in records()}
    assert rows[0]["评级"] == "A"


def test_missing_or_unusable_input_is_rejected(tmp_path):
    with pytest.raises(AppError) as exc:
        export_readable(tmp_path / "missing.xlsx", tmp_path / "out.xlsx")
    assert exc.value.code == "INPUT_MISSING"

    text = tmp_path / "notes.txt"
    text.write_text("not a workbook", encoding="utf-8")
    with pytest.raises(AppError) as exc:
        export_readable(text, tmp_path / "out.xlsx")
    assert exc.value.code == "INVALID_INPUT"

    source = fixture(tmp_path / "export.xlsx")
    with pytest.raises(AppError) as exc:
        export_readable(source, source)
    assert exc.value.code == "INVALID_INPUT"

    incomplete = Workbook()
    incomplete.active.title = "名单"
    incomplete.save(tmp_path / "incomplete.xlsx")
    with pytest.raises(AppError) as exc:
        export_readable(tmp_path / "incomplete.xlsx", tmp_path / "out.xlsx")
    assert exc.value.code == "INVALID_INPUT"


def test_two_hundred_rows_are_exported_under_five_seconds(tmp_path):
    rows = [company(f"c{index}", f"Patio Company {index}", domain=f"company{index}.com",
                    website_url=f"https://company{index}.com", lead_score=50 + index % 20,
                    contacts=[contact(f"c{index}", email=f"sales@company{index}.com")])
            for index in range(200)]
    data, _, _, _ = build_export(rows, dict(MANIFEST), format="xlsx", language="zh")
    source = tmp_path / "export-200.xlsx"
    source.write_bytes(data)
    began = time.perf_counter()
    result = export_readable(source, tmp_path / "readable-200.xlsx")
    assert time.perf_counter() - began < 5
    assert result["companies"] == 200 and result["ratings"] == {"A": 200, "B": 0, "C": 0}
