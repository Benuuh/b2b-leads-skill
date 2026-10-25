"""Skill delivery is stricter than historical coverage and shares export truth."""
from copy import deepcopy
import csv
import io
import json
import zipfile

import pytest

from app.rules.contacts import extract_company
from app.delivery.selection import DELIVERY_POLICY_VERSION, delivery_companies
from app.delivery.exporting import build_export
from app.delivery.metrics import METRICS_VERSION, calculate_metrics
from app.delivery.selection import filter_companies

NOW = "2026-10-24T00:00:00Z"


def company(identifier="one", *, channel="email", website=None):
    website = website or f"https://{identifier}.example.org"
    contact = {"contact_id": "ct_" + identifier, "company_id": identifier, "email": None, "phone": None,
        "retrieved_at": NOW, "source_urls": [website + "/contact"], "binding_status": "company",
        "inferred": False, "tags": ["public_business"], "confidence": .9}
    if channel == "email":
        contact.update(email=f"info@{identifier}.example.org", email_status="unverified", syntax_status="valid",
                       mx_status="unknown", email_type="company_generic", is_role_account=True)
    elif channel == "phone":
        contact.update(phone="+12135550123", phone_raw="+1 213 555 0123", phone_status="valid", phone_type="company_main")
    value = contact.get(channel)
    item = {"company_id": identifier, "company_name": "Company " + identifier, "website_url": website,
        "domain": website.split("://")[1], "country_code": "US", "industry_text": "freight forwarding",
        "qualification": {"decision": "qualified", "confidence": .9}, "status": "qualified",
        "candidate_ordinal": 1, "contacts": [contact] if channel != "none" else [], "conflicts": [], "missing": [],
        "sources": [{"entity_id": "ct_" + identifier, "field": channel, "value": value,
                     "source_url": website + "/contact", "source_type": "official_site",
                     "retrieved_at": NOW, "method": "extracted", "source_excerpt": str(value), "confidence": .9}] if value else []}
    item['sources'].append({'entity_id': identifier, 'field': 'website_url', 'value': website,
        'source_url': 'https://directory.example.org/' + identifier, 'source_type': 'directory',
        'retrieved_at': NOW, 'method': 'agent_page_observation',
        'source_excerpt': item['company_name'] + ': ' + website})
    return item


def form_company(identifier="form"):
    item = company(identifier, channel="none")
    item["contact_form_url"] = item["website_url"] + "/contact"
    item["sources"].insert(0, {"entity_id": identifier, "field": "contact_form_url", "value": item["contact_form_url"],
        "source_url": item["contact_form_url"], "source_type": "official_site", "retrieved_at": NOW,
        "method": "observed_contact_form", "form_fields": ["email", "message"], "confidence": .9})
    return item


def test_unverified_public_email_and_valid_phone_are_channels_without_mailbox_claim():
    email, phone, form = company(), company("phone", channel="phone"), form_company()
    original = deepcopy([email, phone, form])
    actual_input = [email, phone, form]
    rows = delivery_companies([email, phone, form], now=NOW)
    assert {row["company_id"] for row in rows} == {"one", "phone", "form"}
    assert next(row for row in rows if row["company_id"] == "one")["contacts"][0]["email_status"] == "unverified"
    form = next(row for row in rows if row["company_id"] == "form")
    assert form["contactable"] and form["delivery_channels"] == ["contact_form"]
    assert actual_input == original
    assert all(row["delivery_policy_version"] == DELIVERY_POLICY_VERSION for row in rows)


@pytest.mark.parametrize("change", [
    {"inferred": True}, {"email_type": "inferred"}, {"binding_status": "inferred"},
    {"binding_status": "unbound"}, {"binding_status": None}, {"company_id": "wrong"},
    {"tags": ["binding_conflict"]}, {"tags": ["stale"]}, {"tags": ["expired"]},
    {"email_status": "invalid"}, {"mx_status": "invalid"}, {"syntax_status": "invalid"},
    {"mx_status": "absent"}, {"mx_status": "not_found"},
    {"email": "not an email"}, {"opt_out": True},
])
def test_unusable_or_unbound_channels_never_count(change):
    item = company()
    item["contacts"][0].update(change)
    assert delivery_companies([item], now=NOW) == []


@pytest.mark.parametrize("timestamp", [None, "", "2026-10-24", "yesterday", "2025-01-01T00:00:00Z", "2026-10-25T00:00:00Z"])
def test_unknown_stale_or_future_field_evidence_never_counts(timestamp):
    item = company()
    item["sources"][0]["retrieved_at"] = timestamp
    # Fresh contact aggregates cannot launder bad field-level evidence.
    assert delivery_companies([item], now=NOW) == []


def test_contact_source_url_fallback_never_substitutes_field_observations():
    item = company()
    item["sources"] = []
    assert delivery_companies([item], now=NOW) == []
    item["contacts"][0]["retrieved_at"] = None
    assert delivery_companies([item], now=NOW) == []
    item["contacts"][0]["retrieved_at"] = NOW
    item["contacts"][0]["source_urls"] = ["https://unrelated.net/team"]
    assert delivery_companies([item], now=NOW) == []


def test_source_binding_blocks_wrong_official_site_and_accepts_identified_directory():
    item = company()
    source = item["sources"][0]
    source["source_url"] = "https://unrelated.net/member/one"
    assert delivery_companies([item], now=NOW) == []
    source["source_type"] = "directory"
    assert delivery_companies([item], now=NOW) == []
    item["sources"].append({**source, "entity_id": item["company_id"], "field": "company_name", "value": item["company_name"], "source_excerpt": item['company_name']})
    assert len(delivery_companies([item], now=NOW)) == 1
    item["sources"][-1]["value"] = "A different subject"
    assert delivery_companies([item], now=NOW) == []
    source.update(source_url="https://sibling.example.org/contact", source_type="official_site")
    assert delivery_companies([item], now=NOW) == []


def test_qualification_suppression_and_ambiguous_website_are_conservative():
    item = company()
    assert delivery_companies([item], suppressions={"email": [item["contacts"][0]["email"]]}, now=NOW) == []
    assert delivery_companies([item], suppressions={"domain": ["one.example.org"]}, now=NOW) == []
    item["qualification"]["decision"] = "uncertain"
    assert delivery_companies([item], now=NOW) == []
    item["qualification"]["decision"] = "qualified"
    item["conflicts"] = [{"field": "website_url", "values": [item["website_url"], "https://other.org"]}]
    assert delivery_companies([item], now=NOW) == []


def test_existing_shared_channel_ownership_does_not_transfer_to_another_subject():
    first, second = company("first"), company("second")
    first["candidate_ordinal"], second["candidate_ordinal"] = 1, 2
    second["contacts"][0]["email"] = first["contacts"][0]["email"]
    second["sources"][0]["value"] = second["contacts"][0]["email"]
    second["sources"][0]["source_excerpt"] = second["contacts"][0]["email"]
    first["qualification"]["decision"] = "uncertain"
    assert delivery_companies([second, first], now=NOW) == []
    first["qualification"]["decision"] = "qualified"
    assert [row["company_id"] for row in delivery_companies([second, first], now=NOW)] == ["first"]


def test_shared_form_ownership_is_frozen_before_qualification():
    first, second = form_company("first"), form_company("second")
    second["website_url"], second["domain"] = first["website_url"], first["domain"]
    second["contact_form_url"] = first["contact_form_url"]
    second["sources"][0].update(value=first["contact_form_url"], source_url=first["contact_form_url"])
    first["candidate_ordinal"], second["candidate_ordinal"] = 1, 2
    first["qualification"]["decision"] = "uncertain"
    assert delivery_companies([second, first], now=NOW) == []


def test_same_name_different_country_and_company_site_remain_separate():
    first, second = company("first"), company("second")
    first["company_name"] = second["company_name"] = "Same Name Logistics"
    for item in (first, second):
        item['sources'][-1]['source_excerpt'] = item['company_name'] + ': ' + item['website_url']
    second["country_code"] = "CA"
    assert len(delivery_companies([first, second], now=NOW)) == 2


def test_delivery_strips_bad_channel_when_independent_public_phone_qualifies():
    item = company("phone", channel="phone")
    invalid = company("phone")["contacts"][0]
    invalid["contact_id"] = "inferred"
    invalid.update(inferred=True, email_type="inferred")
    item["contacts"].append(invalid)
    row = delivery_companies([item], now=NOW)[0]
    assert row["email_count"] == 0 and row["main_email"] is None and row["phone_count"] == 1
    assert len(row["contacts"]) == 1


@pytest.mark.parametrize("change", [
    {"method": "discovered"}, {"method": "official_public_link"}, {"retrieved_at": None},
    {"form_fields": None}, {"form_fields": ["email", "newsletter"]},
    {"source_url": "https://form.example.org/other"}, {"value": "https://form.example.org/other"},
])
def test_bare_contact_page_and_unproven_form_never_count(change):
    item = form_company()
    item["sources"][0].update(change)
    assert delivery_companies([item], now=NOW) == []


def test_parsed_forms_require_reply_and_message_fields_and_imports_need_observed_fields():
    candidate = {"company_name": "Public Freight", "website_url": "https://freight.org", "country_code": "US",
                 "source_url": "https://freight.org/contact", "source_type": "official_site", "retrieved_at": NOW}
    def extract(html):
        return extract_company(candidate, [{"final_url": candidate["source_url"], "html": html, "fetched_at": NOW}], {}, now=NOW)
    assert extract('<form><input type="email" name="newsletter"></form>')["contact_form_url"] is None
    assert extract('<a href="/contact">Contact us</a>')["contact_form_url"] is None
    assert extract('<form><input type="email"><textarea name="message"></textarea></form>')["contact_form_url"] == candidate["source_url"]
    candidate["contact_form_url"] = candidate["source_url"]
    source = {"field": "contact_form_url", "value": candidate["source_url"], "source_url": candidate["source_url"],
              "source_type": "official_site", "retrieved_at": NOW, "method": "observed_contact_form"}
    candidate["sources"] = [source]
    assert extract_company(candidate, [], {}, now=NOW)["contact_form_url"] is None
    source["form_fields"] = ["email", "message"]
    assert extract_company(candidate, [], {}, now=NOW)["contact_form_url"] == candidate["source_url"]


def test_delivery_export_uses_identical_predicate_caps_target_and_preserves_old_metrics():
    rows = [company("email"), company("phone", channel="phone"), form_company(), company("uncertain")]
    rows[-1]["qualification"]["decision"] = "uncertain"
    original = calculate_metrics(rows, context={"as_of": NOW})
    data, _, _, manifest = build_export(rows, {"freshness_days": 180}, format="csv", now=NOW,
                                       delivery_only=True, delivery_limit=2)
    expected = delivery_companies(rows, now=NOW)[:2]
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        exported = list(csv.DictReader(io.StringIO(package.read("companies.csv").decode("utf-8-sig"))))
    assert [row["company_id"] for row in exported] == [row["company_id"] for row in expected]
    assert manifest["delivery"]["eligible_count"] == 3
    assert manifest["delivery"]["exported_count"] == manifest["row_counts"]["companies"] == 2
    assert manifest["metrics"]["metrics_version"] == METRICS_VERSION
    assert manifest["metrics"]["batch_metrics"] == original["batch_metrics"]
    _, _, _, legacy = build_export(rows, {}, format="csv", now=NOW)
    assert legacy["row_counts"]["companies"] == 4 and "delivery" not in legacy


def test_form_only_export_reports_contactable_and_source_truth():
    data, _, _, manifest = build_export([form_company()], {}, format="csv", now=NOW, delivery_only=True)
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        rows = list(csv.DictReader(io.StringIO(package.read("companies.csv").decode("utf-8-sig"))))
        sources = list(csv.DictReader(io.StringIO(package.read("sources.csv").decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["contactable"] == "True"
    assert manifest["delivery"]["channels"] == {"email": 0, "phone": 0, "contact_form": 1}
    assert any(source["field"] == "contactable" and source["value"] == "True" for source in sources)
    assert any(source["field"] == "contact_form_url" and "message" in source["form_fields"] for source in sources)
    # Historical R0 coverage intentionally does not gain contact-form numerator.
    assert manifest["metrics"]["batch_metrics"]["coverage"]["contact"]["numerator"] == 0


def test_suppressed_contact_cannot_escape_through_nested_evidence_or_csv_sheets():
    item = company()
    forbidden = item["contacts"][0]["email"]
    item["qualification"]["evidence"] = [
        {"source_url": item["website_url"] + "/team?email=info%40one.example.org", "matched_terms": ["freight"]},
        {"source_url": item["website_url"] + "/about", "contact": {"email": forbidden}},
    ]
    phone = company(channel="phone")["contacts"][0]
    phone["contact_id"] = "ct_phone"
    item["contacts"].append(phone)
    item["sources"].append({**company(channel="phone")["sources"][0], "entity_id": "ct_phone"})
    original = deepcopy(item)
    selected = filter_companies([item], suppressions={"email": [forbidden]}, now=NOW)[0]
    assert selected["main_email"] is None and selected["main_phone"] == phone["phone"]
    assert forbidden not in json.dumps(selected)
    assert "info%40one.example.org" not in json.dumps(selected)
    data, _, _, manifest = build_export([item], {}, format="csv", now=NOW,
        suppressions={"email": [forbidden]}, delivery_only=True)
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        assert all(forbidden not in package.read(name).decode("utf-8-sig") for name in package.namelist())
    assert manifest["delivery"]["channels"] == {"email": 0, "phone": 1, "contact_form": 0}
    assert item == original


def test_chinese_excel_has_four_tables_and_safe_text_cells():
    from openpyxl import load_workbook
    item = company()
    item["company_name"] = "=HYPERLINK(\"https://example.org\")"
    item['sources'][-1]['source_excerpt'] = item['company_name'] + ': ' + item['website_url']
    data, filename, media_type, manifest = build_export([item], {}, now=NOW,
        language="zh-CN", delivery_only=True)
    workbook = load_workbook(io.BytesIO(data))
    assert filename.endswith(".xlsx") and "spreadsheetml" in media_type
    assert workbook.sheetnames == ["Companies", "Contacts", "Sources", "Manifest"]
    sheet = workbook["Companies"]
    assert sheet["B1"].value == "企业名称"
    assert sheet["B2"].value == "'" + item["company_name"] and sheet["B2"].data_type == "s"
    assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref
    assert workbook["Manifest"]["A1"].value == "项目"
    assert manifest["row_counts"]["companies"] == 1


def test_delivery_selection_performs_no_dns_or_mx_lookups(monkeypatch):
    import socket
    import dns.resolver
    def unexpected(*args, **kwargs):
        raise AssertionError("selection must never perform network I/O")
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(dns.resolver, "resolve", unexpected)
    assert len(delivery_companies([company()], now=NOW)) == 1


def test_self_declared_website_confirmation_and_unrelated_channel_excerpt_never_deliver():
    item = company()
    item['website_binding_status'] = 'confirmed'
    item['sources'] = item['sources'][:1]
    assert delivery_companies([item], now=NOW) == []
    item = company()
    item['sources'][0]['source_excerpt'] = 'Contact our business team for information.'
    assert delivery_companies([item], now=NOW) == []


def test_sourced_directory_channel_remains_deliverable_without_a_website():
    from app.pipeline import qualify
    candidate = {'company_name': 'Harbor Freight LLC', 'website_url': '', 'country_code': 'US',
        'industry_text': 'freight forwarding', 'source_url': 'https://directory.example.org/harbor',
        'source_type': 'directory', 'retrieved_at': NOW, 'source_excerpt': 'Harbor Freight LLC provides freight forwarding in the US.',
        'email': 'sales@harbor.example.org', 'sources': [{'field': 'email', 'value': 'sales@harbor.example.org',
            'source_url': 'https://directory.example.org/harbor', 'source_type': 'directory', 'retrieved_at': NOW,
            'source_excerpt': 'Harbor Freight LLC business contact sales@harbor.example.org'}]}
    spec = {'industry': 'freight forwarding', 'countries': ['US']}
    def process():
        row = extract_company(candidate, [], spec, now=NOW, mx_lookup=lambda _: 'unknown')
        return qualify(row, candidate, [], spec)
    row = delivery_companies([process()], now=NOW)[0]
    assert row['delivery_channels'] == ['email'] and not row['website_url']
    candidate['sources'][0]['source_excerpt'] = 'Harbor Freight LLC business profile.'
    assert delivery_companies([process()], now=NOW) == []


def test_another_directory_subjects_phone_cannot_bind_through_a_customer_mention():
    from app.pipeline import qualify
    candidate = {'company_name': 'Harbor Company', 'website_url': '', 'country_code': 'US',
        'industry_text': 'freight forwarding', 'source_url': 'https://directory.example.org/harbor',
        'source_type': 'directory', 'retrieved_at': NOW,
        'source_excerpt': 'Harbor Company provides freight forwarding services.', 'phone': '+12135550123',
        'sources': [{'field': 'phone', 'value': '+12135550123',
            'source_url': 'https://directory.example.org/other', 'source_type': 'directory', 'retrieved_at': NOW,
            'source_excerpt': 'Other Company contact +1 213 555 0123. Our customer is Harbor Company.'}]}
    spec = {'industry': 'freight forwarding', 'countries': ['US']}
    row = extract_company(candidate, [], spec, now=NOW)
    assert row['contacts'] == []
    row = qualify(row, candidate, [], spec)
    assert delivery_companies([row], now=NOW) == []
    assert not any(source.get('field') == 'company_name' and source.get('source_url').endswith('/other') for source in row['sources'])


def test_observed_distinct_entities_sharing_one_website_keep_separate_delivery_rows():
    first = company('first', website='https://group.example.org')
    second = company('second', website='https://group.example.org')
    assert {row['company_id'] for row in delivery_companies([first, second], now=NOW)} == {'first', 'second'}


def test_conflicting_website_subject_never_delivers_even_with_retained_channel_evidence():
    item = company()
    item['website_binding_status'] = 'conflicting'
    assert delivery_companies([item], now=NOW) == []


@pytest.mark.parametrize("limit", [0, -1, True])
def test_delivery_export_rejects_nonpositive_or_boolean_limit(limit):
    with pytest.raises(ValueError, match="delivery_limit"):
        build_export([company()], {}, now=NOW, delivery_only=True, delivery_limit=limit)
