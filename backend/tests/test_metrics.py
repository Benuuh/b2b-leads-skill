"""Metrics contracts use explicit clocks and synthetic fixtures, never pilot labels."""
from copy import deepcopy
import json

import pytest

from app.delivery.exporting import build_export
from app.delivery.selection import filter_companies
from app.delivery.metrics import calculate_metrics, eligibility, legacy_stats, qualification_cohort

AS_OF = "2026-10-23T00:00:00Z"


def contact(identifier, **changes):
    return {"contact_id": identifier, "company_id": "unused", "email": identifier + "@example.org", "email_status": "unverified",
            "retrieved_at": AS_OF, "source_urls": ["https://example.org/contact"], "confidence": .8, "tags": [], **changes}


def company(identifier, contacts=None, **changes):
    items = contacts or []
    return {"company_id": identifier, "company_name": "Company " + identifier, "country_code": "US",
            "website_url": "https://" + identifier + ".example.org/", "industry_text": "freight forwarding",
            "created_at": "2026-10-22T00:00:00Z", "qualification": {"decision": "qualified", "confidence": .9},
            "status": "qualified" if items else "insufficient", "contacts": [{**item, "company_id": identifier} for item in items],
            "sources": [], "conflicts": [], "missing": [], **changes}


def metrics(rows, **context):
    return calculate_metrics(rows, context={"as_of": AS_OF, **context})


def test_formal_denominator_survives_no_channels_contact_and_score_filters():
    rows = [company(str(index), [contact("ct" + str(index))] if index < 6 else []) for index in range(10)]
    filtered = metrics(rows, filters={"has_email": True})
    assert filtered["batch_metrics"]["coverage"]["email"] == {"numerator": 6, "denominator": 10, "value": .6, "unknown_count": 6}
    assert filtered["batch_metrics"]["cohort_count"] == 10
    assert filtered["view_metrics"]["coverage"]["email"]["denominator"] == 10
    assert filtered["output_metrics"]["row_counts"]["companies"] == 6
    assert filtered["output_metrics"]["nonempty_ratios"]["email"]["value"] == 1
    strict = metrics(rows, mode="verified_only", filters={"named_only": True, "min_lead_score": 100})
    assert strict["batch_metrics"] == filtered["batch_metrics"]
    assert strict["view_metrics"]["coverage"]["email"]["value"] == 0
    assert strict["cohort_hash"] == filtered["cohort_hash"]


def test_empty_ratios_and_historical_details_are_unknown():
    result = metrics([])
    assert result["batch_metrics"]["coverage"]["email"]["value"] is None
    assert result["output_metrics"]["provenance"]["records_with_sources"]["value"] is None
    assert result["output_metrics"]["provenance"]["export_fields_with_sources"]["value"] is None
    assert result["requests"]["actual_http_attempts"] is None
    assert result["requests"]["http_per_qualified_contactable_company"] is None
    assert result["completeness"]["sources"] == "no_sample"
    assert result["completeness"]["state"] == "historical_details_missing"
    old = legacy_stats(result)
    assert old["email_coverage"] == 0 and old["provenance_complete"] is False


@pytest.mark.parametrize("value", [None, "2026-10-23", "not-a-time"])
def test_explicit_timezone_clock_is_required(value):
    with pytest.raises(ValueError):
        calculate_metrics([], context={"as_of": value})


def test_modern_decision_wins_and_legacy_is_labeled_without_fabrication():
    modern = company("modern", status="needs_review")
    old = company("legacy", qualification=None, match_reasons=["公开内容命中:freight"])
    unknown = company("unknown", qualification=None)
    excluded = company("excluded", qualification={"decision": "excluded"}, status="qualified")
    assert eligibility(modern) == ("qualified", "qualification")
    result = metrics([modern, old, unknown, excluded])
    assert result["batch_metrics"]["cohort_count"] == 2
    assert result["batch_metrics"]["eligibility_basis"] == {"qualification": 1, "legacy": 1}
    assert result["funnel"]["unevaluated"] == 1 and result["funnel"]["excluded"] == 1
    assert old["qualification"] is None
    assert len(qualification_cohort([modern, old, unknown, excluded])) == 2


def test_enterprise_suppression_changes_current_n_channel_suppression_does_not():
    rows = [company("first", [contact("first")]), company("second", [contact("second")])]
    result = metrics(rows, suppressions=[{"kind": "email", "value": "first@example.org"}])
    assert result["batch_metrics"]["cohort_count_original"] == result["batch_metrics"]["cohort_count"] == 2
    assert result["batch_metrics"]["coverage"]["email"]["numerator"] == 1
    removed = metrics(rows, suppressions=[{"kind": "company_id", "value": "first"}])
    assert removed["batch_metrics"]["cohort_count_original"] == 2
    assert removed["batch_metrics"]["cohort_count"] == 1
    assert removed["batch_metrics"]["suppressed_qualified_companies"] == 1
    assert removed["cohort_hash"] == result["cohort_hash"]
    assert removed["suppression_version"] != result["suppression_version"]


def test_business_channel_predicates_keep_unknown_inferred_and_risk_separate():
    rows = [company("one", [contact("inferred", inferred=True, email_type="inferred")]),
            company("two", [contact("risk", email_status="risky")]),
            company("three", [contact("invalid", email_status="invalid", phone="+12135550123", phone_status="unknown")]),
            company("four", [contact("phone", email=None, phone="+12135550124", phone_status="valid")])]
    result = metrics(rows)["batch_metrics"]["coverage"]
    assert result["email"]["numerator"] == 2 and result["phone"]["numerator"] == 1
    assert result["contact"]["numerator"] == 3
    assert result["inferred_email"]["numerator"] == 1
    assert result["observed_email"]["numerator"] == 1
    assert result["risky_email"]["numerator"] == 1
    assert result["phone"]["unknown_count"] == 1


def test_freshness_uses_observation_time_and_retention_does_not_renew():
    rows = [company("stale", [contact("stale", retrieved_at="2026-01-01T00:00:00Z")]),
            company("fresh", [contact("fresh")]),
            company("unknown", [contact("unknown", retrieved_at=None)]),
            company("expired", [contact("expired")], expires_at=AS_OF)]
    result = metrics(rows)["batch_metrics"]["coverage"]
    assert result["email"]["numerator"] == 3 and result["email"]["denominator"] == 4
    assert result["fresh_usable_channels"] == {"numerator": 1, "denominator": 4, "value": .25, "unknown_count": 1}
    assert result["stale_channels"]["numerator"] == 1
    strict = metrics(rows, mode="verified_only")
    assert strict["batch_metrics"]["coverage"] == result
    assert strict["output_metrics"]["row_counts"]["companies"] == 0


def test_frozen_channel_ownership_is_not_query_score_completion_or_filter_order():
    first = company("z_first", [contact("z_owner", email="shared@example.org", email_status="unknown")], candidate_ordinal=1)
    second = company("a_second", [contact("a_later", email="shared@example.org", email_status="verified")], candidate_ordinal=2)
    for rows in ([second, first], [first, second]):
        view = filter_companies(rows, mode="max_coverage", now=AS_OF)
        assert next(row for row in view if row["company_id"] == "z_first")["contacts"][0]["email"] == "shared@example.org"
        assert next(row for row in view if row["company_id"] == "a_second")["contacts"] == []
        filtered = filter_companies(rows, mode="verified_only", now=AS_OF)
        assert filtered == []
        assert filter_companies(rows, filters={"email_status": "verified"}, now=AS_OF) == []
    assert metrics([first, second]) == metrics([second, first])


def test_legacy_cohort_uses_created_time_and_one_fallback_policy():
    later = company("a", [contact("a", email="shared@example.org")], created_at="2026-10-22T00:00:00Z", candidate_ordinal=0)
    earlier = company("z", [contact("z", email="shared@example.org")], created_at="2026-10-21T00:00:00Z")
    result = filter_companies([later, earlier], now=AS_OF)
    assert next(row for row in result if row["company_id"] == "z")["contacts"][0]["email"] == "shared@example.org"
    assert next(row for row in result if row["company_id"] == "a")["contacts"] == []


def test_legacy_ownership_compares_created_instants_across_timezones():
    earlier = company("z", [contact("z", email="shared@example.org")], created_at="2026-10-22T00:00:00+08:00")
    later = company("a", [contact("a", email="shared@example.org")], created_at="2026-10-21T20:00:00Z")
    result = filter_companies([later, earlier], now=AS_OF)
    assert next(row for row in result if row["company_id"] == "z")["contacts"][0]["email"] == "shared@example.org"


def test_intra_enterprise_channel_owner_is_contact_id_before_priority_score():
    row = company("one", [contact("z", email="same@example.org", binding_status="explicit", contact_name="Jane", title="Director", email_type="named_business"),
                          contact("a", email="same@example.org", email_type="inferred", inferred=True)])
    result = filter_companies([row], now=AS_OF)[0]
    assert next(c for c in result["contacts"] if c.get("email"))["contact_id"] == "a"


def test_three_named_contracts_are_distinct_and_inference_is_not_explicit():
    rows = [company("one", [contact("name", contact_name="Person")]),
            company("two", [contact("title", contact_name="Person", title="Sales", binding_status="inferred", inferred=True)]),
            company("three", [contact("explicit", contact_name="Person", title="Sales", binding_status="explicit")])]
    result = metrics(rows)["batch_metrics"]["coverage"]
    assert result["has_name"]["numerator"] == 3
    assert result["has_name_title_source"]["numerator"] == 2
    assert result["explicit_same_entity_binding"]["numerator"] == 1


def test_record_sources_do_not_imply_every_exported_field_is_traceable():
    row = company("one", [contact("email")])
    row["sources"] = [{"entity_id": "one", "field": "company_name", "value": row["company_name"],
        "source_url": "https://example.org/", "retrieved_at": AS_OF, "source_type": "directory", "method": "observed"}]
    result = metrics([row])["output_metrics"]["provenance"]
    assert result["records_with_sources"]["value"] == 1
    assert result["export_fields_with_sources"]["value"] < 1
    assert result["export_fields_with_sources"]["unknown_count"] > 0


def test_observed_requests_cost_and_wall_time_are_not_task_time():
    rows = [company("one", [contact("one")])]
    summary = {"protocol_version": "observed-1", "completeness": "complete", "actual_http_attempts": 12,
               "dns_attempts": 12, "mx_attempts": 1, "dns_mx_completeness": "observed",
               "purpose_counts": {"robots": 2, "discovery": 4, "contacts": 6}, "retry_count": 1,
               "outstanding_count": 0, "unknown_count": 0, "cache_hits": 0, "throttle_wait_seconds": 2}
    result = metrics(rows, request_attempts=summary, started_at="2026-10-22T23:59:00Z", task_processing_seconds=100, paused_seconds=10)
    assert result["requests"]["actual_http_attempts"] == 12
    assert result["requests"]["http_per_qualified_contactable_company"] == 12
    assert result["requests"]["pending_reservations"] is None
    assert result["requests"]["budget_protection"] == "not_enabled"
    assert result["timing"]["wall_seconds"] == 60 and result["timing"]["task_processing_seconds"] == 100
    assert result["completeness"]["state"] == "complete"
    summary.update(actual_http_attempts=None, outstanding_count=1, unknown_count=1, completeness="incomplete")
    result = metrics(rows, request_attempts=summary)
    assert result["requests"]["actual_http_attempts"] is None
    assert result["requests"]["http_per_qualified_contactable_company"] is None
    assert "request_outcome_incomplete" in result["completeness"]["reasons"]


def test_lookup_outcome_is_independent_of_http_completeness():
    result=metrics([],request_attempts={'protocol_version':'observed-1','completeness':'complete',
        'actual_http_attempts':3,'dns_mx_completeness':'incomplete','dns_unknown_count':1})
    assert result['requests']['actual_http_attempts']==3
    assert result['requests']['dns_lookups'] is None
    assert 'lookup_outcome_incomplete' in result['completeness']['reasons']
    assert result['completeness']['state']=='incomplete'


def test_pruned_observed_history_cannot_be_complete_or_zero_cost_with_known_timings():
    rows = [company("one", [contact("one")])]
    result = metrics(rows, request_observation_enabled=True, batch_status="completed",
        request_attempts={"protocol_version": "observed-1", "actual_http_attempts": None, "recorded_attempts": 0,
                          "completeness": "historical_missing", "outstanding_count": 0, "unknown_count": 0,
                          "throttle_wait_seconds": 0},
        started_at="2026-10-22T23:59:00Z", finished_at=AS_OF, task_processing_seconds=60, paused_seconds=0)
    assert result["requests"]["protocol_version"] == "observed-1"
    assert result["requests"]["actual_http_attempts"] is None
    assert result["requests"]["http_per_qualified_contactable_company"] is None
    assert result["completeness"]["state"] == "historical_details_missing"
    assert result["completeness"]["requests"] == "unrecorded"
    assert "historical_request_details_missing" in result["completeness"]["reasons"]


def test_terminal_missing_total_without_outcome_evidence_is_not_complete():
    result = metrics([], request_observation_enabled=True, batch_status="completed",
        request_attempts={"protocol_version": "observed-1", "actual_http_attempts": None,
                          "completeness": "complete", "throttle_wait_seconds": 0},
        started_at="2026-10-22T23:59:00Z", finished_at=AS_OF, task_processing_seconds=60, paused_seconds=0)
    assert result["completeness"]["state"] == "historical_details_missing"
    assert "historical_request_details_missing" in result["completeness"]["reasons"]


def test_enterprise_attributes_define_labeled_subcohort_only():
    rows = [company("us", [contact("us")]), company("ca", [], country_code="CA")]
    result = metrics(rows, filters={"country": "CA", "has_email": True})
    assert result["batch_metrics"]["cohort_count"] == 2
    assert result["view_metrics"]["cohort_count"] == 1
    assert result["view_metrics"]["scope"] == "enterprise_subcohort"
    assert result["view_metrics"]["coverage"]["email"]["value"] == 0


def test_semantically_identical_filters_share_normalized_hash():
    rows = [company("one", [contact("one")])]
    first = metrics(rows, filters={"country_code": "US", "min_confidence": 0, "q": " COMPANY "})
    second = metrics(rows, filters={"countries": ["us"], "q": "company"})
    assert first["normalized_filters_hash"] == second["normalized_filters_hash"]
    assert first["batch_metrics"] == second["batch_metrics"]
    assert first["view_metrics"] == second["view_metrics"]


def test_metrics_observation_does_not_mutate_qualification_or_raw_channels():
    rows = [company("one", [contact("one")]), company("nochannel")]
    frozen = deepcopy(rows)
    metrics(rows, filters={"has_email": True})
    assert rows == frozen


def test_export_and_direct_calculator_share_batch_view_contract_independent_of_projection():
    rows = [company("one", [contact("one")]), company("nochannel")]
    context = {"as_of": AS_OF, "mode": "balanced", "filters": {"has_email": True}, "data_version": "frozen-result"}
    expected = calculate_metrics(rows, context=context)
    _, _, _, manifest = build_export(rows, {"freshness_days": 180}, format="csv", mode="balanced", filters={"has_email": True},
        metrics_context=context, now=AS_OF, fields={"Companies": ["company_name"], "Contacts": ["email"]})
    actual = manifest["metrics"]
    for field in ("cohort_hash", "policy_hash", "suppression_version", "data_version", "as_of", "batch_metrics", "view_metrics"):
        assert actual[field] == expected[field]
    assert actual["output_metrics"]["row_counts"] == manifest["row_counts"]
    assert manifest["coverage"]["email"] == 1
    assert actual["batch_metrics"]["coverage"]["email"]["value"] == .5
    assert "legacy_output_nonempty_ratio" in manifest["coverage_semantics"]
    assert json.dumps(actual)
