"""Offline fixtures verify factual bindings, validation limits and export boundaries."""
from copy import deepcopy
import csv
import io
import json
import zipfile

import pytest

from app.rules.contacts import extract_company, validate_email, validate_phone
from app.delivery.exporting import build_export
from app.delivery.selection import filter_companies

NOW = "2026-10-20T01:00:00Z"
CANDIDATE = {"company_name": "Fixture Forwarding", "website_url": "https://fixture-freight.test",
             "country_code": "US", "source_url": "https://directory.test/member/fixture",
             "source_type": "directory", "retrieved_at": NOW, "industry_text": "freight forwarding"}
SEGMENT = {"countries": ["US"], "contact_freshness_days": 180}
HTML = """<html><body>
<script type="application/ld+json">{"@type":"Organization","name":"Fixture Forwarding",
"address":{"streetAddress":"1 Business Way","addressLocality":"Los Angeles","addressRegion":"CA","addressCountry":"US"},
"telephone":"+1 213 555 0123","email":"info@fixture-freight.test"}</script>
<div class="team-member"><h3>Anna Smith</h3><div class="position">Sales Director</div>
<a href="mailto:anna.smith@fixture-freight.test">Email Anna</a><a href="tel:+12135550124;ext=42">Direct</a></div>
<div class="team-member"><h3>Michael Chen</h3><div class="position">Operations Manager</div></div>
<p>Shared desk: sales [at] fixture-freight [dot] test</p>
<p>Jane Doe, Purchasing Manager</p><p>Unbound mailbox: purchasing@fixture-freight.test</p>
</body></html>"""


def company(html=HTML, candidate=None):
    return extract_company(candidate or CANDIDATE, [{"final_url": "https://fixture-freight.test/team", "html": html,
                           "fetched_at": NOW, "fetch_status": "ok", "source_type": "official_site"}],
                           SEGMENT, mx_lookup=lambda _: "valid", now=NOW)


def test_public_extraction_provenance_never_infers_email_or_page_wide_binding():
    record = company()
    contacts = {c["email"]: c for c in record["contacts"] if c["email"]}
    assert contacts["info@fixture-freight.test"]["email_type"] == "company_generic"
    assert contacts["sales@fixture-freight.test"]["email_type"] == "role_based"
    assert contacts["anna.smith@fixture-freight.test"]["contact_name"] == "Anna Smith"
    assert contacts["anna.smith@fixture-freight.test"]["phone"] == "+12135550124"
    assert contacts["anna.smith@fixture-freight.test"]["phone_extension"] == "42"
    assert 'michael.chen@fixture-freight.test' not in contacts
    assert not any(contact.get('inferred') for contact in record['contacts'])
    assert contacts["purchasing@fixture-freight.test"]["contact_name"] is None
    assert all(c["email_status"] != "verified" for c in record["contacts"])
    assert record["address"] == "1 Business Way, Los Angeles, CA"
    assert all(s["source_url"] and s["retrieved_at"] and s["method"] for s in record["sources"])
    for contact in record["contacts"]:
        for field in ("email", "phone", "contact_name", "title"):
            if contact.get(field):
                assert any(s["entity_id"] == contact["contact_id"] and s["field"] == field and s["value"] == contact[field] for s in record["sources"])


def test_no_inference_without_observed_named_pattern_and_no_fabricated_country():
    record = company('<div class="team-member"><h3>Anna Smith</h3><p class="position">Manager</p></div>',
                     {"company_name": "Missing Facts", "website_url": "https://missing.test", "source_url": "https://missing.test"})
    assert record["country_code"] is None
    assert record["industry_text"] is None
    assert not any(c["email"] for c in record["contacts"])
    assert "no_email" in record["missing"] and "no_country" in record["missing"]
    assert record["status"] == "insufficient"


def test_validation_does_not_turn_mx_into_mailbox_verification():
    assert validate_email("person@example.test", mx_lookup=lambda _: True) == {
        "email": "person@example.test", "syntax_status": "valid", "mx_status": "valid", "email_status": "unverified",
        "catch_all_status": "not_checked", "smtp_status": "not_performed", "disposable_status": "unknown", "is_role_account": False}
    assert validate_email("bad..local@example.test", mx_lookup=lambda _: "valid")["syntax_status"] == "invalid"
    assert validate_email("person@example.test", mx_lookup=lambda _: "invalid")["email_status"] == "invalid"
    assert validate_email("person@example.test", mx_lookup=lambda _: (_ for _ in ()).throw(TimeoutError()))["mx_status"] == "unknown"
    phone = validate_phone("(213) 555-0123 x 99", "US")
    assert phone["phone"] == "+12135550123" and phone["phone_extension"] == "99"
    assert phone["phone_status"] == "valid" and phone["line_type"] != "unknown"


def test_contact_freshness_and_json_ld_person_binding():
    record = extract_company(CANDIDATE, [{"final_url": "https://fixture-freight.test/people", "fetched_at": "2025-01-01T00:00:00Z",
         "html": '<script type="application/ld+json">{"@type":"Person","name":"Sarah Lee","jobTitle":"CEO","email":"sarah.lee@fixture-freight.test"}</script>'}],
         SEGMENT, mx_lookup=lambda _: "valid", now=NOW)
    contact = record["contacts"][0]
    assert contact["contact_name"] == "Sarah Lee" and "stale" in contact["tags"]
    assert filter_companies([record], mode="verified_only") == []


def test_suppression_and_verified_mode_do_not_leak_aggregate_fields_or_sources():
    record = company()
    filtered = filter_companies([record], suppressions=[{"kind": "email", "value": "info@fixture-freight.test"}])
    assert filtered[0]["main_email"] != "info@fixture-freight.test"
    assert "info@fixture-freight.test" not in json.dumps(filtered)
    assert filter_companies([record], suppressions={"domains": ["fixture-freight.test"]}) == []
    assert filter_companies([record], suppressions={"companies": ["Fixture Forwarding"]}) == []
    verified = filter_companies([record], mode="verified_only")
    assert verified and all(not c.get("email") for c in verified[0]["contacts"])
    assert verified[0]["main_email"] is None
    assert "@fixture-freight.test" not in json.dumps(verified)
    assert all(c.get("phone_status") == "valid" for c in verified[0]["contacts"])
    # Input remains available for a later balanced export.
    assert record["main_email"] == "info@fixture-freight.test"


def test_csv_manifest_counts_bom_formula_protection_and_demo_boundary():
    record = company()
    record["company_name"] = "  =HYPERLINK(\"https://invalid.test\")"
    data, filename, media_type, manifest = build_export([record], {"batch_id": "batch_fixture", "segment_version": 2}, format="csv")
    assert filename.endswith(".zip") and media_type == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == {"companies.csv", "contacts.csv", "sources.csv", "manifest.json"}
        for name in ("companies", "contacts", "sources"):
            content = archive.read(name + ".csv")
            assert content.startswith(b"\xef\xbb\xbf")
            rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
            assert len(rows) == manifest["row_counts"][name]
            if name == "companies":
                assert rows[0]["company_name"].startswith("'  =")
        assert json.loads(archive.read("manifest.json"))["row_counts"] == manifest["row_counts"]
    demo = deepcopy(record)
    demo["demo_only"] = True
    with pytest.raises(ValueError, match="演示"):
        build_export([demo], {}, format="csv")
    with pytest.raises(ValueError, match="演示"):
        build_export([], {"demo_only": True}, format="csv")


def test_xlsx_four_sheets_field_selection_chinese_headers_and_no_formulas():
    from openpyxl import load_workbook
    record = company()
    record["company_name"] = "=1+1"
    data, filename, media_type, manifest = build_export([record], {"batch_id": "fixture"},
        fields={"Companies": ["company_id", "company_name"]}, language="zh")
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.sheetnames == ["Companies", "Contacts", "Sources", "Manifest"]
    assert workbook["Companies"]["B1"].value == "企业名称"
    assert workbook["Companies"]["B2"].value == "'=1+1"
    assert workbook["Companies"]["B2"].data_type == "s"
    assert workbook["Companies"].max_column == 2
    assert workbook["Contacts"].max_row - 1 == manifest["row_counts"]["contacts"]
    assert filename.endswith(".xlsx") and "spreadsheetml" in media_type


def test_filters_preserve_contact_semantics_and_phone_suppression():
    record = company()
    assert filter_companies([record], filters={"q": "fixture", "country": "US", "industry": "freight", "has_email": True})
    assert filter_companies([record], filters={"country": "DE"}) == []
    named = filter_companies([record], filters={"named_only": True})[0]
    assert named["main_email"] == "anna.smith@fixture-freight.test"
    assert all(contact.get("contact_name") for contact in named["contacts"])
    filtered = filter_companies([record], suppressions=[{"kind": "phone", "value": "+1 (213) 555-0124"}])
    assert not any(c.get("contact_name") == "Anna Smith" for c in filtered[0]["contacts"])
    assert "+12135550124" not in json.dumps(filtered)


def test_discovery_sources_exact_duplicates_and_shared_domain_entities():
    candidate = deepcopy(CANDIDATE)
    candidate["sources"] = [{"source_url": "https://association.test/fixture", "source_type": "association", "retrieved_at": NOW,
        'company_name': CANDIDATE['company_name'], 'source_excerpt': CANDIDATE['company_name']}]
    record = company(candidate=candidate)
    assert any(s["source_url"] == "https://association.test/fixture" for s in record["sources"])
    duplicate = deepcopy(record)
    duplicate["company_id"] = "co_duplicate"
    duplicate["sources"].append({"entity_id": "co_duplicate", "field": "company_name", "value": record["company_name"],
        "source_type": "maps", "source_url": "https://maps.test/fixture", "retrieved_at": NOW, "method": "discovered", "confidence": .8})
    different_entity = deepcopy(record)
    different_entity["company_id"] = "co_other"
    different_entity["company_name"] = "Fixture Subsidiary LLC"
    different_entity["contacts"] = []
    branch = deepcopy(record)
    branch["company_id"] = "co_branch"
    branch["branch_id"] = "east-coast"
    branch["contacts"] = []
    filtered = filter_companies([record, duplicate, different_entity, branch])
    assert len(filtered) == 3
    assert any(s["source_url"] == "https://maps.test/fixture" for s in filtered[0]["sources"])
    demo = deepcopy(record)
    demo["demo_only"] = True
    assert filter_companies([demo])[0]["demo_only"]


def test_contactpoint_export_freshness_and_post_filter_aggregate_provenance():
    record = company('<script type="application/ld+json">{"@type":"ContactPoint","contactType":"sales",'
                     '"email":"sales@fixture-freight.test","telephone":"+1 213 555 0123"}</script>')
    assert any(c["email"] == "sales@fixture-freight.test" for c in record["contacts"])
    later = filter_companies([record], freshness_days=30, now="2027-01-01T00:00:00Z")
    assert all("stale" in c["tags"] for c in later[0]["contacts"])
    assert filter_companies([record], mode="verified_only", freshness_days=30, now="2027-01-01T00:00:00Z") == []
    filtered = filter_companies([company()], suppressions={"email": ["info@fixture-freight.test"]})[0]
    assert filtered["main_email"] == "anna.smith@fixture-freight.test"
    assert any(s["entity_id"] == filtered["company_id"] and s["field"] == "main_email"
               and s["value"] == filtered["main_email"] for s in filtered["sources"])
    assert filter_companies([record], filters={"named_only": True}) == []
    excluded = deepcopy(record)
    excluded["status"] = "excluded"
    _, _, _, manifest = build_export([excluded], {}, format="csv")
    assert manifest["row_counts"]["companies"] == 0


def test_no_domain_offices_do_not_merge_and_identity_fields_cannot_be_removed():
    first = company(candidate={**CANDIDATE, "website_url": "", "address": "1 West Road"})
    second = company(candidate={**CANDIDATE, "website_url": "", "address": "2 East Road"})
    assert len(filter_companies([first, second])) == 2
    data, _, _, manifest = build_export([first], {}, format="csv", fields={"Companies": ["company_name"],
        "Contacts": ["email"], "Sources": ["confidence"]})
    assert manifest["fields"]["Companies"] == ["company_id", "company_name"]
    assert manifest["fields"]["Contacts"][:2] == ["contact_id", "company_id"]
    assert all(field in manifest["fields"]["Contacts"] for field in ("email_status", "inferred", "tags", "syntax_status", "mx_status"))
    assert all(field in manifest["fields"]["Sources"] for field in ("entity_id", "field", "value", "source_type", "source_url", "retrieved_at", "method"))


def test_public_forms_social_links_and_unperformed_email_checks_are_traceable():
    html = '''<form action="/submit" method="post"><input type="email" name="email"><textarea name="message"></textarea></form>
    <a href="https://linkedin.com/company/fixture-forwarding">LinkedIn</a>
    <a href="javascript:alert(1)">bad</a><a href="http://localhost/secret">bad</a>
    <a href="https://linkedin.com.attacker.com/company/fixture">bad</a>
    <a href="https://twitter.com/intent/tweet?text=test">share</a>
    <script type="application/ld+json">{"@type":"Organization","sameAs":["https://facebook.com/fixtureforwarding"],"email":"sales@fixture-freight.test"}</script>'''
    record = company(html, candidate={**CANDIDATE, "demo_only": True})
    assert record["contact_form_url"] == "https://fixture-freight.test/team"
    assert set(record["social_urls"]) == {"https://linkedin.com/company/fixture-forwarding", "https://facebook.com/fixtureforwarding"}
    contact = next(c for c in record["contacts"] if c["email"])
    assert contact["catch_all_status"] == "not_checked" and contact["smtp_status"] == "not_performed"
    assert contact["disposable_status"] == "unknown" and contact["is_role_account"]
    for field in ("contact_form_url", "social_urls"):
        assert any(s["field"] == field and s["source_url"] for s in record["sources"])


def test_merged_candidate_contacts_keep_only_actual_observation_sources():
    first_url = "https://search.example.com/results"
    email_url = "https://directory.example.com/fixture"
    second_email_url = "https://association.example.com/fixture"
    phone_url = "https://registry.example.com/fixture"
    direct = {**CANDIDATE, "source_url": first_url, "email": "sales@fixture-freight.test", "phone": "+1 213 555 0123",
        "sources": [{"source_url": first_url, "source_type": "search", "company_name": CANDIDATE["company_name"]},
            {"source_url": email_url, "source_type": "directory", "email": "sales@fixture-freight.test", "retrieved_at": NOW,
             'source_excerpt': 'Fixture Forwarding sales@fixture-freight.test'},
            {"source_url": second_email_url, "source_type": "association", "email": ["sales@fixture-freight.test"], "retrieved_at": NOW,
             'source_excerpt': 'Fixture Forwarding sales@fixture-freight.test'},
            {"source_url": phone_url, "source_type": "registry", "phone": "+12135550123", "retrieved_at": NOW,
             'source_excerpt': 'Fixture Forwarding +1 213 555 0123'}]}
    result = extract_company(direct, [], SEGMENT, mx_lookup=lambda _: "valid", now=NOW)
    email_contact = next(c for c in result["contacts"] if c["email"])
    assert set(email_contact["source_urls"]) == {email_url, second_email_url}
    email_sources = [s for s in result["sources"] if s["field"] == "email"]
    assert {s["source_url"] for s in email_sources} == {email_url, second_email_url}
    assert {s["source_url"] for s in result["sources"] if s["field"] == "phone"} == {phone_url}


@pytest.mark.parametrize('field,value', [('email', 'invented@example.org'), ('phone', '+12135550123')])
def test_imported_channel_assertions_need_their_own_literal_observation(field, value):
    candidate = {**CANDIDATE, 'website_url': '', field: value,
        'source_url': 'https://directory.example.org/company', 'source_excerpt': 'Fixture Forwarding is a freight forwarder.',
        'sources': [{'field': field, 'value': value, 'source_url': 'https://directory.example.org/company',
            'source_type': 'directory', 'retrieved_at': NOW, 'source_excerpt': 'Fixture Forwarding company profile.'}]}
    def extract():
        return extract_company(candidate, [], SEGMENT, mx_lookup=lambda _: 'valid', now=NOW)
    assert extract()['contacts'] == []
    candidate['sources'][0]['source_excerpt'] = 'Fixture Forwarding business contact ' + value
    retained = extract()
    assert len(retained['contacts']) == 1 and retained['contacts'][0][field] == value
    candidate['sources'][0]['source_type'] = 'search_snippet'
    assert extract()['contacts'] == []


def test_bare_discovery_reference_never_claims_that_the_page_identifies_company():
    candidate = {**CANDIDATE, 'sources': [{'source_url': 'https://unrelated.example.org/profile'}]}
    result = extract_company(candidate, [], SEGMENT, now=NOW)
    assert not any(source['source_url'] == 'https://unrelated.example.org/profile' for source in result['sources'])


def test_email_only_later_page_does_not_attest_to_inherited_phone():
    pages = [{"final_url": "https://fixture-freight.test/team", "source_type": "official_site", "fetched_at": NOW,
        "html": '<div class="team-member"><h3>Anna Smith</h3><span class="position">Sales</span>'
                '<a href="mailto:anna.smith@fixture-freight.test">Email</a><a href="tel:+12135550124">Phone</a></div>'},
        {"final_url": "https://fixture-freight.test/press", "source_type": "official_site", "fetched_at": NOW,
         "html": '<a href="mailto:anna.smith@fixture-freight.test">Business email</a>'}]
    result = extract_company(CANDIDATE, pages, SEGMENT, mx_lookup=lambda _: "valid", now=NOW)
    assert all(source["source_url"].endswith("/team") for source in result["sources"] if source["field"] in {"phone", "contact_name", "title"})


def test_seed_domain_hint_is_replaced_by_published_company_data():
    candidate = {**CANDIDATE, "company_name": "fixture-freight.test", "company_name_is_domain_hint": True}
    html = '''<script type="application/ld+json">{"@type":"Organization","url":"https://fixture-freight.test",
    "name":"Fixture Forwarding LLC","description":"Public international transport company",
    "industry":"freight forwarding","naics":"488510","isicV4":"5229",
    "annualRevenue":{"value":2500000,"currency":"USD"},"brand":{"name":"Fixture Ocean"},
    "hasCertification":{"name":"ISO 9001"},"products":[{"name":"Consolidation services"}],
    "knowsAbout":["Customs brokerage"]}</script>'''
    result = extract_company(candidate, [{"html": html, "final_url": "https://fixture-freight.test/", "fetched_at": NOW}],
        SEGMENT, mx_lookup=lambda _: "valid", now=NOW)
    assert result["company_name"] == "Fixture Forwarding LLC"
    assert result["industry_text"] == "freight forwarding"
    assert result["industry_codes"] == ["NAICS:488510", "ISIC:5229"]
    assert result["revenue"] == "2500000 USD" and result["brands"] == ["Fixture Ocean"]
    assert result["certifications"] == ["ISO 9001"] and result["products"] == ["Consolidation services"]
    assert result["knows_about"] == ["Customs brokerage"]
    assert all(source["value"] != "fixture-freight.test" for source in result["sources"] if source["field"] == "company_name")
    for field in ("description", "industry_codes", "revenue", "brands", "certifications", "products", "knows_about"):
        assert any(source["field"] == field and source["value"] == result[field] and source["source_url"] for source in result["sources"])


def test_meta_name_and_description_do_not_invent_industry():
    candidate = {**CANDIDATE, "company_name": "fixture-freight.test", "company_name_is_domain_hint": True, "industry_text": None}
    result = extract_company(candidate, [{"html": '<meta property="og:site_name" content="Fixture Public Name">'
        '<meta name="description" content="Public site description">', "final_url": "https://fixture-freight.test/", "fetched_at": NOW}],
        SEGMENT, mx_lookup=lambda _: "valid", now=NOW)
    assert result["company_name"] == "Fixture Public Name" and result["description"] == "Public site description"
    assert result["industry_text"] is None


def test_default_contact_validation_does_not_resolve_dns(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("rule made a DNS request"))
    assert validate_email("person@example.org")["mx_status"] == "unknown"


"""Offline pipeline ranking and export checks using retained, traceable facts."""
from copy import deepcopy
import csv
import io
import json
import zipfile

from app.delivery.exporting import build_export
from app.delivery.selection import filter_companies
from app.rules.ranking import SCORING_RULE_VERSION, rank_contacts, score_company

RANK_NOW = "2026-10-23T00:00:00Z"


def rank_contact_fixture(key="direct", **changes):
    return {"contact_id": "ct_" + key, "company_id": "co_fixture",
            "contact_name": "Anna Smith", "email": key + "@fixture.test",
            "email_type": "named_business", "email_status": "unverified",
            "syntax_status": "valid", "mx_status": "valid", "phone": None,
            "phone_status": None, "binding_status": "explicit", "inferred": False,
            "confidence": .9, "source_urls": ["https://fixture.test/team"],
            "retrieved_at": RANK_NOW, "tags": ["public_business"], **changes}


def rank_company_fixture(key="fixture", contacts=None, **changes):
    contacts = contacts if contacts is not None else [rank_contact_fixture()]
    record = {"company_id": "co_" + key, "company_name": key.title(),
              "domain": key + ".test", "website_url": "https://" + key + ".test",
              "country_code": "US", "industry_text": "freight forwarding",
              "status": "qualified", "qualification_score": .9,
              "qualification": {"decision": "qualified", "confidence": .9, "independent_sources": 2},
              "contacts": contacts, "sources": [], "missing": [], "conflicts": [], **changes}
    for item in contacts:
        item["company_id"] = record["company_id"]
        for field in ("email", "phone", "contact_name"):
            if item.get(field):
                record["sources"].append({"entity_id": item["contact_id"], "field": field,
                    "value": item[field], "source_url": item["source_urls"][0],
                    "source_type": "official_site", "retrieved_at": RANK_NOW,
                    "method": "observed", "confidence": .9})
    return record


def test_explicit_named_contact_outranks_inferred_without_changing_validation():
    direct = rank_contact_fixture()
    inferred = rank_contact_fixture("inferred", binding_status="inferred", inferred=True, email_type="inferred", confidence=1)
    role = rank_contact_fixture("sales", contact_name=None, email_type="role_based", binding_status="rank_company_fixture", is_role_account=True)
    unbound = rank_contact_fixture("unbound", binding_status="unbound")
    originals = deepcopy([inferred, unbound, role, direct])
    ranked = rank_contacts(originals)
    assert [item["contact_priority"] for item in ranked] == ["direct_business", "company_channel", "unbound", "inferred"]
    assert [item["contact_id"] for item in ranked] == ["ct_direct", "ct_sales", "ct_unbound", "ct_inferred"]
    assert all(item["email_status"] == "unverified" for item in ranked)
    assert all("priority_score" not in item for item in originals)


def test_invalid_stale_conflicted_channels_cannot_receive_high_priority():
    fresh = rank_contact_fixture()
    invalid = rank_contact_fixture("invalid", email_status="invalid", confidence=1)
    stale = rank_contact_fixture("stale", tags=["stale"], confidence=1)
    conflict = rank_contact_fixture("conflict", tags=["binding_conflict"], confidence=1)
    ranked = {item["contact_id"]: item for item in rank_contacts([fresh, invalid, stale, conflict])}
    assert ranked["ct_invalid"]["contact_priority"] == "unusable"
    assert ranked["ct_invalid"]["priority_score"] == 0
    assert ranked["ct_stale"]["priority_score"] < ranked["ct_direct"]["priority_score"]
    assert ranked["ct_conflict"]["priority_score"] < ranked["ct_direct"]["priority_score"]
    record = score_company(rank_company_fixture(contacts=[invalid]))
    assert record["lead_score"] <= 40 and record["confidence"] < .5
    assert record["main_email"] is None


def test_primary_email_and_phone_each_follow_their_own_channel_priority():
    telephone = rank_contact_fixture("desk", email="sales@fixture.test", email_type="role_based", is_role_account=True,
                        phone="+12135550123", phone_type="direct_business", phone_status="valid")
    email = rank_contact_fixture("named", phone="+12135550124", phone_type="company_main", phone_status="valid")
    result = score_company(rank_company_fixture(contacts=[telephone, email]))
    assert result["main_email"] == email["email"]
    assert result["main_phone"] == telephone["phone"]


def test_post_filter_score_and_primary_channel_use_only_permitted_facts():
    direct = rank_contact_fixture()
    role = rank_contact_fixture("sales", contact_name=None, email_type="role_based", binding_status="rank_company_fixture", is_role_account=True, confidence=.7)
    original = rank_company_fixture(contacts=[role, direct])
    before = filter_companies([original], now=RANK_NOW)[0]
    after = filter_companies([original], suppressions={"email": [direct["email"]]}, now=RANK_NOW)[0]
    assert before["main_email"] == direct["email"]
    assert after["main_email"] == role["email"]
    assert after["lead_score"] < before["lead_score"]
    assert after["confidence"] < before["confidence"]
    assert direct["email"] not in json.dumps(after)
    assert any(source["entity_id"] == after["company_id"] and source["field"] == "main_email"
               and source["value"] == role["email"] and source["source_url"] == role["source_urls"][0]
               for source in after["sources"])
    assert "lead_score" not in original


def test_mode_filter_recomputes_primary_phone_and_score_after_removing_mail():
    direct = rank_contact_fixture()
    phone = rank_contact_fixture("phone", contact_name=None, email=None, email_type=None, binding_status="rank_company_fixture",
                    phone="+12135550123", phone_type="company_main", phone_status="valid")
    original = rank_company_fixture(contacts=[phone, direct])
    balanced = filter_companies([original], now=RANK_NOW)[0]
    verified = filter_companies([original], mode="verified_only", now=RANK_NOW)[0]
    assert balanced["main_email"] == direct["email"]
    assert verified["main_email"] is None and verified["main_phone"] == phone["phone"]
    assert direct["email"] not in json.dumps(verified)
    assert verified["lead_score"] < balanced["lead_score"]


def test_stable_company_and_contact_order_regardless_of_database_order():
    a = rank_company_fixture("a", contacts=[rank_contact_fixture("z", email="z@a.test"), rank_contact_fixture("a", email="a@a.test")])
    b = rank_company_fixture("b", contacts=[rank_contact_fixture("b", email="b@b.test")])
    low = rank_company_fixture("low", contacts=[rank_contact_fixture("low", email="low@low.test", inferred=True, email_type="inferred", binding_status="inferred")])
    expected = filter_companies([b, low, a], now=RANK_NOW)
    actual = filter_companies([a, low, b], now=RANK_NOW)
    assert [item["company_id"] for item in expected] == ["co_a", "co_b", "co_low"]
    assert [item["company_id"] for item in expected] == [item["company_id"] for item in actual]
    assert [item["contact_id"] for item in actual[0]["contacts"]] == ["ct_a", "ct_z"]
    assert score_company(score_company(a)) == score_company(a)


def test_duplicate_company_keeps_strongest_public_contact_binding_and_sources():
    inferred = rank_contact_fixture("same", inferred=True, email_type="inferred", binding_status="inferred", confidence=.4)
    first = rank_company_fixture(contacts=[inferred])
    second = rank_company_fixture(contacts=[rank_contact_fixture("observed", email=inferred["email"])])
    second["company_id"] = "co_later_duplicate"
    for source in second["sources"]:
        source["source_url"] = "https://association.test/member"
    result = filter_companies([second, first], now=RANK_NOW)
    assert len(result) == 1
    chosen = result[0]["contacts"][0]
    assert chosen["contact_priority"] == "direct_business" and not chosen["inferred"]
    assert chosen["binding_status"] == "explicit"
    assert {source["source_url"] for source in result[0]["sources"] if source["field"] == "email"} == {
        "https://fixture.test/team", "https://association.test/member"}


def test_duplicate_company_cannot_restore_an_opted_out_contact():
    first = rank_company_fixture(contacts=[rank_contact_fixture("same", opt_out=True)])
    second = rank_company_fixture(contacts=[rank_contact_fixture("same")])
    second["company_id"] = "co_later_duplicate"
    result = filter_companies([second, first], now=RANK_NOW)[0]
    assert result["contacts"] == [] and result["main_email"] is None
    assert "same@fixture.test" not in json.dumps(result)


def test_old_records_get_evidence_confidence_and_threshold_is_not_lead_score():
    old = rank_company_fixture()
    for field in ("qualification", "qualification_score", "confidence"):
        old.pop(field, None)
    result = filter_companies([old], filters={"min_confidence": .6}, now=RANK_NOW)
    assert len(result) == 1 and .6 <= result[0]["confidence"] <= 1
    assert result[0]["lead_score"] > 1
    assert "qualification_score" not in result[0] and "qualification" not in result[0]
    assert filter_companies([old], filters={"min_confidence": .99}, now=RANK_NOW) == []
    assert filter_companies([old], filters={"min_lead_score": 100}, now=RANK_NOW) == []


def test_legacy_qualification_fallback_stays_internal_and_remains_unassessed():
    old = rank_company_fixture(qualification_score=.7)
    old.pop("qualification")
    result = score_company(old)
    assert "qualification_score" not in result and "qualification" not in result
    assert result["confidence"] > .6 and result["lead_score"] > 0


def test_source_corroboration_groups_subdomains_and_uses_matched_qualification_sources():
    legacy = rank_company_fixture()
    legacy.pop("qualification")
    legacy["sources"] = [{"source_url": "https://" + host + "/member", "source_type": "directory"}
                         for host in ("directory.example.co.uk", "mirror.example.co.uk", "www.example.co.uk")]
    result = score_company(legacy)
    assert result["score_breakdown"]["source_corroboration"] == 2.33
    legacy["sources"] = [{"source_url": "https://" + host + "/member", "source_type": "directory"}
                         for host in ("directory.fixture.test", "mirror.fixture.test")]
    assert score_company(legacy)["score_breakdown"]["source_corroboration"] == 2.33
    qualified = rank_company_fixture(qualification={"decision": "qualified", "confidence": .9, "independent_sources": 1,
        "evidence": [{"source_url": "https://directory.example.co.uk/member", "matched_terms": ["freight"]}]})
    qualified["sources"] += [{"source_url": "https://" + host + "/member", "source_type": "directory"}
                             for host in ("other.com", "unrelated.org", "also.net")]
    assert score_company(qualified)["score_breakdown"]["source_corroboration"] == 2.33
    qualified["qualification"]["independent_sources"] = 3
    qualified["qualification"]["evidence"].append({"source_url": "https://mirror.example.co.uk/member", "matched_terms": ["freight"]})
    assert score_company(qualified)["score_breakdown"]["source_corroboration"] == 2.33


def test_nested_qualification_evidence_cannot_restore_filtered_or_suppressed_channels():
    direct = rank_contact_fixture(phone="+12135550124", phone_type="direct_business", phone_status="valid")
    role = rank_contact_fixture("sales", contact_name=None, email_type="role_based", binding_status="rank_company_fixture")
    original = rank_company_fixture(contacts=[direct, role])
    original["qualification"]["evidence"] = [
        {"source_url": "https://fixture.test/team?email=direct%40fixture.test", "matched_terms": ["freight forwarding"]},
        {"source_url": "https://association.test/member", "matched_terms": ["freight forwarding"],
         "rank_contact_fixture": {"email": direct["email"], "phone": "+1 (213) 555-0124"}}]
    original["qualification"]["reasons"] = ["freight forwarding", "Contact direct@fixture.test"]
    filtered = filter_companies([original], suppressions={"email": [direct["email"]]}, now=RANK_NOW)[0]
    assert direct["email"] not in json.dumps(filtered) and direct["phone"] not in json.dumps(filtered)
    assert "direct%40fixture.test" not in json.dumps(filtered)
    assert filtered["qualification"]["evidence"] == [
        {"source_url": "https://association.test/member", "matched_terms": ["freight forwarding"], "rank_contact_fixture": {}}]
    assert filtered["qualification"]["reasons"] == ["freight forwarding"]
    assert filtered["score_breakdown"]["source_corroboration"] == 2.33
    named_out = filter_companies([original], filters={"named_only": False}, now=RANK_NOW)[0]
    assert direct["email"] not in json.dumps(named_out) and direct["phone"] not in json.dumps(named_out)


def test_qualification_decision_penalties_do_not_rewrite_qualification_confidence():
    qualified = score_company(rank_company_fixture())
    uncertain = score_company(rank_company_fixture(qualification={"decision": "uncertain", "confidence": .9, "independent_sources": 2}))
    excluded = score_company(rank_company_fixture(qualification={"decision": "excluded", "confidence": .9, "independent_sources": 2}))
    assert uncertain["lead_score"] <= 59 < qualified["lead_score"]
    assert excluded["lead_score"] <= 20
    assert excluded["confidence"] < uncertain["confidence"] < qualified["confidence"]
    assert uncertain["qualification_score"] == .9


def test_export_schema_manifest_and_source_trace_do_not_leak_suppressed_values():
    original = rank_company_fixture(contacts=[rank_contact_fixture(), rank_contact_fixture("sales", contact_name=None, email_type="role_based", binding_status="rank_company_fixture")])
    forbidden = original["contacts"][0]["email"]
    data, _, _, manifest = build_export([original], {}, format="csv", suppressions={"email": [forbidden]})
    assert manifest["scoring_rule_version"] == SCORING_RULE_VERSION
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        companies = list(csv.DictReader(io.StringIO(archive.read("companies.csv").decode("utf-8-sig"))))
        contacts = list(csv.DictReader(io.StringIO(archive.read("contacts.csv").decode("utf-8-sig"))))
        sources = list(csv.DictReader(io.StringIO(archive.read("sources.csv").decode("utf-8-sig"))))
        assert {"lead_score", "score_breakdown", "qualification_score"} <= set(companies[0])
        assert {"contact_priority", "priority_score"} <= set(contacts[0])
        assert any(source["field"] == "lead_score" for source in sources)
        assert any(source["field"] == "priority_score" for source in sources)
        assert all(forbidden not in archive.read(name).decode("utf-8-sig") for name in archive.namelist())
