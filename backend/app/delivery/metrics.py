"""Versioned, network-free B2B metrics calculated at an explicit observation time.

Business coverage is a qualified enterprise cohort, never the number of export
rows.  Output ratios deliberately retain their separate, selected-row meaning.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

from ..rules.contacts import normalize_domain
from .fields import COMPANY_FIELDS, CONTACT_FIELDS
from .selection import (coalesce_companies, eligibility, filter_companies, matches,
    normalize_filters, normalize_name, observation_time, qualification_cohort, suppression_sets)

METRICS_VERSION = "b2b-metrics-1"
COVERAGE_POLICY = "b2b-business-1"
DEDUP_POLICY_VERSION = "b2b-dedup-1"
LEGACY_DEDUP_POLICY_VERSION = "b2b-dedup-legacy-1"
COMPANY_FILTERS = {"q", "country", "country_code", "countries", "industry", "industry_text", "company_type", "status"}


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def _time(value):
    try:
        return observation_time(value)
    except ValueError:
        return None


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def ratio(numerator, denominator, unknown_count=0):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None, "unknown_count": unknown_count}


def _suppressed(company, sets):
    domain = normalize_domain(company.get("domain") or company.get("website_url") or "")
    return (any(domain == value or domain.endswith("." + value) for value in sets["domain"])
            or normalize_name(company.get("company_name")) in sets["company"]
            or normalize_name(company.get("company_id")) in sets["company"])


def _property_matches(company, filters):
    """Enterprise-only predicate: no channels, freshness, scores or row removal."""
    if filters.get("q") and str(filters["q"]).casefold() not in " ".join(str(company.get(field) or "") for field in
            ("company_name", "domain", "country_code", "region", "city", "industry_text")).casefold():
        return False
    return (matches(company.get("country_code"), filters.get("countries", filters.get("country_code", filters.get("country"))))
            and matches(company.get("industry_text"), filters.get("industry", filters.get("industry_text")), substring=True)
            and matches(company.get("company_type"), filters.get("company_type"))
            and matches(company.get("status"), filters.get("status")))


def _email(contact):
    return bool(str(contact.get("email") or "").strip()) and contact.get("email_status") != "invalid"


def _phone(contact):
    return bool(str(contact.get("phone") or "").strip()) and contact.get("phone_status") == "valid"


def _inferred(contact):
    return bool(contact.get("inferred") or contact.get("email_type") == "inferred"
                or contact.get("binding_status") == "inferred" or "inferred" in (contact.get("tags") or []))


def _freshness(contact, as_of, freshness_days):
    observed = _time(contact.get("retrieved_at"))
    if set(contact.get("tags") or []) & {"stale", "expired"}:
        return "stale"
    if observed is None or observed > as_of:
        return "unknown"
    return "fresh" if as_of - observed <= timedelta(days=freshness_days) else "stale"


def _source_valid(source):
    return bool(source.get("source_url") and _time(source.get("retrieved_at")) and source.get("method"))


def _named_source(company, contact):
    if contact.get("source_urls"):
        return True
    return any(source.get("entity_id") == contact.get("contact_id") and source.get("source_url")
               and source.get("field") in {"contact_name", "title"} for source in company.get("sources") or [])


def _coverage(companies, retained, as_of, freshness_days):
    lookup = {row["company_id"]: row for row in retained}
    counts = Counter()
    unknown = Counter()
    statuses = Counter()
    channel_counts = Counter()
    for original in companies:
        company = lookup.get(original["company_id"], {})
        contacts = company.get("contacts") or []
        predicates = {
            "email": any(_email(c) for c in contacts),
            "phone": any(_phone(c) for c in contacts),
            "contact": any(_email(c) or _phone(c) for c in contacts),
            "has_name": any(c.get("contact_name") for c in contacts),
            "has_name_title_source": any(c.get("contact_name") and c.get("title") and _named_source(company, c) for c in contacts),
            "explicit_same_entity_binding": any(c.get("contact_name") and c.get("title") and _named_source(company, c)
                and c.get("binding_status") == "explicit" and not _inferred(c) for c in contacts),
            "observed_email": any(_email(c) and not _inferred(c) for c in contacts),
            "inferred_email": any(_email(c) and _inferred(c) for c in contacts),
            "risky_email": any(_email(c) and (c.get("email_status") in {"risky", "catch_all"}
                or c.get("catch_all_status") in {"risky", "catch_all"}) for c in contacts),
            "stale_channels": any((_email(c) or _phone(c)) and _freshness(c, as_of, freshness_days) == "stale" for c in contacts),
            "fresh_usable_channels": any((_email(c) or _phone(c)) and _freshness(c, as_of, freshness_days) == "fresh" for c in contacts),
            "website_or_directory": bool(company.get("website_url") or any(source.get("source_url") and source.get("source_type") in
                {"directory", "association", "registry", "maps", "map"} for source in company.get("sources") or [])),
            "industry_and_website": bool(company.get("industry_text") and company.get("website_url")),
            "country": bool(company.get("country_code")),
        }
        counts.update(key for key, value in predicates.items() if value)
        if any(c.get("email") and c.get("email_status") in {None, "", "unknown", "unverified"} for c in contacts):
            unknown["email"] += 1
        if any(c.get("phone") and c.get("phone_status") in {None, "", "unknown"} for c in contacts):
            unknown["phone"] += 1
        if any((_email(c) or _phone(c)) and _freshness(c, as_of, freshness_days) == "unknown" for c in contacts):
            unknown["fresh_usable_channels"] += 1
        if any(c.get("contact_name") and c.get("binding_status") not in {"explicit", "company", "inferred"} for c in contacts):
            unknown["explicit_same_entity_binding"] += 1
        for contact in contacts:
            channel_counts["contacts"] += 1
            if _email(contact):
                channel_counts["emails"] += 1
                channel_counts["verified_emails"] += contact.get("email_status") == "verified"
            if _phone(contact):
                channel_counts["phones"] += 1
            if contact.get("email"):
                statuses["email:" + str(contact.get("email_status") or "unknown")] += 1
            if contact.get("phone"):
                statuses["phone:" + str(contact.get("phone_status") or "unknown")] += 1
    keys = ("contact", "email", "phone", "has_name", "has_name_title_source", "explicit_same_entity_binding", "observed_email",
            "inferred_email", "risky_email", "stale_channels", "fresh_usable_channels", "website_or_directory", "industry_and_website", "country")
    return {"coverage": {key: ratio(counts[key], len(companies), unknown[key]) for key in keys},
            "counts": {key: channel_counts[key] for key in ("contacts", "emails", "phones", "verified_emails")},
            "channel_status_counts": dict(sorted(statuses.items()))}


def _present(value):
    return value is not None and value != "" and value != [] and value != {}


def _provenance(companies, sources=None, fields=None):
    sources = [source for company in companies for source in company.get("sources") or []] if sources is None else sources
    indexed = {(source.get("entity_id"), source.get("field"), stable_hash(source.get("value")))
               for source in sources if _source_valid(source)}
    total = traced = 0
    identity_fields = {"company_id", "contact_id"}
    for company in companies:
        entities = [(company, (fields or {}).get("Companies")), *[(contact, (fields or {}).get("Contacts")) for contact in company.get("contacts") or []]]
        for entity, selected_fields in entities:
            identifier = entity.get("contact_id") or entity.get("company_id")
            if selected_fields is None:
                selected_fields = CONTACT_FIELDS if entity.get("contact_id") else COMPANY_FIELDS
            for field in selected_fields:
                if field in identity_fields or not _present(entity.get(field)):
                    continue
                total += 1
                traced += (identifier, field, stable_hash(entity[field])) in indexed
    with_sources = sum(any(_source_valid(source) for source in company.get("sources") or []) for company in companies)
    return {"records_with_sources": ratio(with_sources, len(companies), len(companies) - with_sources),
            "export_fields_with_sources": ratio(traced, total, total - traced),
            "identity_fields": "generated_identifiers_not_public_claims"}


def _telemetry(context, contactable, as_of):
    summary = deepcopy(context.get("observation_summary") or context.get("request_attempts") or {})
    enabled = context.get("request_observation_enabled", summary.get("enabled",
        summary.get("protocol_version") in {"observed-1", "observed-2"} and summary.get("completeness") != "historical_missing"))
    attempts = summary.get("actual_http_attempts", summary.get("total")) if enabled and summary.get("completeness") != "historical_missing" else None
    requests = {"actual_http_attempts": attempts,
                "confirmed_http_attempts": summary.get("confirmed_http_attempts") if enabled else None,
                "dispatch_denied_attempts": summary.get("dispatch_denied_attempts") if enabled else None,
                "dispatch_unknown_attempts": summary.get("dispatch_unknown_attempts") if enabled else None,
                "recorded_attempts": summary.get("recorded_attempts") if enabled else None,
                "by_purpose": summary.get("by_purpose", summary.get("purpose_counts")) if enabled else None,
                "by_source": summary.get("source_counts") if enabled else None,
                "by_query": summary.get("query_counts") if enabled else None,
                "by_status": summary.get("status_counts") if enabled else None,
                "retry_attempts": summary.get("retry_attempts", summary.get("retry_count")) if enabled else None,
                "pending_reservations": summary.get("pending_reservations", summary.get("reserved_requests")),
                "unfinished_attempts": summary.get("unfinished_attempts", summary.get("outstanding_count")) if enabled else None,
                "outcome_unknown": summary.get("outcome_unknown", summary.get("unknown_count")) if enabled else None,
                "cache_hits": summary.get("cache_hits"), "dns_lookups": summary.get("dns_lookups", summary.get("dns_attempts")),
                "mx_lookups": summary.get("mx_lookups", summary.get("mx_attempts")), "response_bytes": summary.get("response_bytes"),
                "lookup_completeness": summary.get("dns_mx_completeness", "not_recorded"),
                "dns_recorded_attempts": summary.get("dns_recorded_attempts"),
                "mx_recorded_attempts": summary.get("mx_recorded_attempts"),
                "dns_unknown_count": summary.get("dns_unknown_count"),
                "mx_unknown_count": summary.get("mx_unknown_count"),
                "protocol_version": summary.get("protocol_version") if enabled else None,
                "http_per_qualified_contactable_company": attempts / contactable if attempts is not None and contactable else None,
                "budget_protection": "not_enabled"}
    started = _time(context.get("started_at"))
    finished = _time(context.get("finished_at"))
    end = min(finished, as_of) if finished else as_of
    wall = max(0, (end - started).total_seconds()) if started and started <= as_of else None
    paused = context.get("paused_seconds")
    paused_at = _time(context.get("paused_at"))
    if paused is not None and paused_at and context.get("batch_status") == "paused":
        paused += max(0, (end - paused_at).total_seconds())
    timing = {"wall_seconds": wall, "task_processing_seconds": context.get("task_processing_seconds"),
              "throttling_wait_seconds": context.get("throttling_wait_seconds", summary.get("throttle_wait_seconds") if enabled else None),
              "paused_seconds": paused, "stage_processing_seconds": context.get("stage_processing_seconds")}
    return requests, timing


def calculate_metrics(cohort, channels=None, context=None):
    """Return one envelope for API, export and pilot without I/O or implicit time.

    ``cohort`` contains all available processed enterprise records. ``channels``
    may supply actual output rows/projection; formal batch and view coverage are
    always calculated from the qualified cohort before channel/output filters.
    """
    context = dict(context or {})
    as_of = observation_time(context.get("as_of"))
    mode = context.get("mode", context.get("quality_mode", "max_coverage"))
    normalized_filters = normalize_filters(context.get("filters"))
    suppressions = context.get("suppressions") or []
    suppressed = suppression_sets(suppressions)
    freshness_days = int(context.get("freshness_days", 180))
    prepared_rows = context.get("prepared_rows")
    prepared_qualified = context.get("qualified_rows")
    rows = list(prepared_rows) if prepared_rows is not None else coalesce_companies(list(cohort or []))
    original = list(prepared_qualified) if prepared_qualified is not None else [
        row for row in rows if eligibility(row)[0] == "qualified"]
    qualified = [row for row in original if not _suppressed(row, suppressed)]
    batch_channels = context.get("batch_channels")
    if batch_channels is None:
        batch_channels = filter_companies(qualified, mode="max_coverage", suppressions=suppressions,
                                          freshness_days=freshness_days, now=as_of)
    company_filters = {key: value for key, value in normalized_filters.items() if key in COMPANY_FILTERS}
    # Property filters may define a sub-cohort; channel and score filters never do.
    view_cohort = [row for row in qualified if _property_matches(row, company_filters)]
    view_channels = context.get("view_channels")
    if view_channels is None:
        view_channels = filter_companies(view_cohort, mode=mode, filters=normalized_filters,
                                         suppressions=suppressions, freshness_days=freshness_days, now=as_of)
    supplied = channels or {}
    output = supplied.get("output") if isinstance(supplied, dict) else supplied
    if output is None:
        output = filter_companies(rows, mode=mode, filters=normalized_filters,
                                  suppressions=suppressions, freshness_days=freshness_days, now=as_of)
    output_sources = supplied.get("output_sources") if isinstance(supplied, dict) else None
    output_sources = [source for row in output for source in row.get("sources") or []] if output_sources is None else output_sources
    export_fields = supplied.get("export_fields") if isinstance(supplied, dict) else None
    batch = _coverage(qualified, batch_channels, as_of, freshness_days)
    batch.update(cohort_count_original=len(original), cohort_count=len(qualified),
                 suppressed_qualified_companies=len(original) - len(qualified), eligibility_basis=dict(Counter(eligibility(row)[1] for row in original)))
    view = _coverage(view_cohort, view_channels, as_of, freshness_days)
    cohort_hash = stable_hash([row["company_id"] for row in original])
    view.update(cohort_count=len(view_cohort), scope="enterprise_subcohort" if company_filters else "batch_cohort",
                cohort_hash=stable_hash([row["company_id"] for row in view_cohort]))
    output_contacts = [contact for row in output for contact in row.get("contacts") or []]
    output_metrics = {"scope": supplied.get("output_scope", context.get("output_scope", "processed_enterprises")) if isinstance(supplied, dict) else context.get("output_scope", "processed_enterprises"),
                      "row_counts": {"companies": len(output), "contacts": len(output_contacts), "sources": len(output_sources)},
                      "nonempty_ratios": {key: ratio(sum(any(contact.get(field) for contact in row.get("contacts") or []) for row in output), len(output))
                          for key, field in (("email", "email"), ("phone", "phone"), ("has_name", "contact_name"))},
                      "provenance": _provenance(output, output_sources, export_fields)}
    output_metrics["nonempty_ratios"]["contact"] = ratio(sum(any(c.get("email") or c.get("phone") for c in row.get("contacts") or []) for row in output), len(output))
    decisions = Counter(eligibility(row)[0] for row in rows)
    funnel = {"raw_candidates": context.get("raw_candidate_count"), "deduplicated_candidates": context.get("discovered"),
              "processed": len(rows), "qualified": decisions["qualified"], "excluded": decisions["excluded"],
              "uncertain": decisions["uncertain"], "unevaluated": decisions["unevaluated"], "failed": context.get("failed"),
              "suppressed": sum(_suppressed(row, suppressed) for row in rows),
              "pipeline_stop_reason_counts": deepcopy(context.get("pipeline_stop_reason_counts")),
              "terminal_partition": ["qualified", "excluded", "uncertain", "unevaluated"],
              "cross_attributes": ["failed", "suppressed"]}
    requests, timing = _telemetry(context, batch["coverage"]["contact"]["numerator"], as_of)
    request_summary = context.get("observation_summary") or context.get("request_attempts") or {}
    request_completeness = request_summary.get("completeness")
    reasons = []
    if requests['lookup_completeness'] not in {'complete','observed','in_progress'}:
        reasons.append('lookup_details_missing')
    if requests.get('dns_unknown_count') or requests.get('mx_unknown_count'):
        reasons.append('lookup_outcome_incomplete')
    # Retention can prune observed-1 rows while preserving the protocol marker.
    # A known protocol is not proof that its full request history still exists.
    if request_completeness == "historical_missing" or (requests["actual_http_attempts"] is None
            and request_completeness not in {"incomplete", "in_progress"}
            and not (requests["unfinished_attempts"] or requests["outcome_unknown"])):
        reasons.append("historical_request_details_missing")
    if requests["unfinished_attempts"] or requests["outcome_unknown"] or request_completeness == "incomplete":
        reasons.append("request_outcome_incomplete")
    if decisions["unevaluated"] or any(eligibility(row)[1] == "legacy" for row in rows):
        reasons.append("qualification_history_incomplete")
    if any(timing[key] is None for key in ("wall_seconds", "task_processing_seconds", "throttling_wait_seconds", "paused_seconds")):
        reasons.append("timing_details_missing")
    state = "in_progress" if context.get("batch_status") in {"draft", "running", "paused"} or request_completeness == "in_progress" else "incomplete" if any(reason in reasons for reason in ('request_outcome_incomplete','lookup_outcome_incomplete')) else "historical_details_missing" if reasons else "complete"
    order_basis = "candidate_ordinal" if original and all(isinstance(row.get("candidate_ordinal"), int)
        and not isinstance(row.get("candidate_ordinal"), bool) for row in original) else "created_at"
    policies = {"metrics_version": METRICS_VERSION, "coverage_policy": COVERAGE_POLICY, "dedup_policy_version": DEDUP_POLICY_VERSION,
                "ownership_order": "candidate_ordinal/company_id; legacy cohort created_at/company_id", "freshness_days": freshness_days,
                "ownership_basis": order_basis, "frozen_result_policy": context.get("frozen_policy_hash")}
    suppression_snapshot = {key: sorted(value) for key, value in suppressed.items()}
    return {"metrics_version": METRICS_VERSION, "coverage_policy": COVERAGE_POLICY, "dedup_policy_version": DEDUP_POLICY_VERSION,
            "as_of": _stamp(as_of), "scope": context.get("scope", "batch"), "cohort_hash": cohort_hash, "dedup_order_basis": order_basis,
            "demo_only": bool(context.get("demo_only") or any(row.get("demo_only") for row in rows)),
            "policy_hash": stable_hash(policies), "suppression_version": context.get("suppression_version") or stable_hash(suppression_snapshot),
            "data_version": context.get("data_version") or stable_hash(rows), "quality_mode": mode,
            "normalized_filters_hash": stable_hash(normalized_filters), "batch_metrics": batch, "view_metrics": view,
            "output_metrics": output_metrics, "funnel": funnel, "requests": requests, "timing": timing,
            "completeness": {"state": state, "reasons": reasons, "requests": "incomplete" if "request_outcome_incomplete" in reasons else
                "unrecorded" if requests["actual_http_attempts"] is None else "observed",
                "qualification": "unrecorded_or_legacy" if "qualification_history_incomplete" in reasons else "evaluated",
                "sources": "no_sample" if not output else "complete" if output_metrics["provenance"]["export_fields_with_sources"]["value"] == 1 else "incomplete"}}


def legacy_stats(envelope, prior_stats=None):
    """Keep the numeric compatibility fields; new consumers use the envelope."""
    result = deepcopy(prior_stats or {})
    batch = envelope["batch_metrics"]
    coverage = batch["coverage"]
    counts = batch["counts"]
    result.update(qualified=batch["cohort_count"], processed=envelope["funnel"]["processed"], contacts=counts["contacts"],
                  email_count=counts["emails"], phone_count=counts["phones"], verified_email_count=counts["verified_emails"],
                  with_email=coverage["email"]["numerator"], with_phone=coverage["phone"]["numerator"],
                  with_named_contact=coverage["has_name"]["numerator"],
                  provenance_complete=envelope["output_metrics"]["provenance"]["records_with_sources"]["value"] == 1,
                  legacy_semantics="compatibility_numbers_empty_denominator_zero; authoritative_contract=metrics", metrics=envelope)
    if envelope["funnel"].get("failed") is not None:
        result["failed"] = envelope["funnel"]["failed"]
    for old, new in (("email_coverage", "email"), ("phone_coverage", "phone"), ("contact_coverage", "contact"), ("named_contact_coverage", "has_name")):
        result[old] = coverage[new]["value"] if coverage[new]["value"] is not None else 0
    return result
