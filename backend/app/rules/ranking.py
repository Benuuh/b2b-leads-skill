"""Deterministic evidence quality and lead ordering, never a delivery guarantee.

Contact priority orders observed business channels. It does not validate a
mailbox or upgrade email_status/phone_status. Company confidence measures the
quality of the retained evidence; lead_score additionally weights qualification
and useful contact coverage. All functions return detached copies.
"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import math
from urllib.parse import urlsplit

import tldextract

SCORING_RULE_VERSION = "b2b-pipeline-1"
_suffix = tldextract.TLDExtract(suffix_list_urls=())
_SOURCE_WEIGHTS = {"official_site": .9, "company_website": .9, "registry": .85,
                   "maps": .8, "directory": .7, "association": .7,
                   "exhibition": .7, "news": .65, "public_social": .6,
                   "search": .5, "inferred": .3}
_PRIORITY_BASE = {"direct_business": 95, "company_channel": 72,
                  "unbound": 48, "inferred": 24, "unusable": 0}


def _number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _unit(value, default=0):
    return max(0, min(1, _number(value, default)))


def _source_domain(url):
    host = (urlsplit(str(url or "")).hostname or "").casefold().removeprefix("www.")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    parsed = _suffix(host)
    # Reserved fixture/private suffixes also share their parent origin; no
    # network lookup is needed to compare subdomains of fixture.test.
    return parsed.top_domain_under_public_suffix or ".".join(host.split(".")[-2:])


def _email_usable(contact):
    return bool(contact.get("email")) and not any(
        str(contact.get(field) or "").lower() == "invalid"
        for field in ("email_status", "syntax_status", "mx_status"))


def _phone_usable(contact):
    return bool(contact.get("phone")) and contact.get("phone_status") != "invalid"


def _stale(contact):
    return bool(set(contact.get("tags") or []) & {"stale", "expired"}) or any(
        contact.get(field) in {"stale", "expired"} for field in ("email_status", "phone_status"))


def _contact_priority(contact):
    email, phone = _email_usable(contact), _phone_usable(contact)
    if contact.get("opt_out") or not (email or phone):
        return "unusable", 0
    tags = set(contact.get("tags") or [])
    inferred = contact.get("inferred") or contact.get("binding_status") == "inferred" or contact.get("email_type") == "inferred" or "inferred" in tags
    explicit = contact.get("binding_status") == "explicit" and bool(contact.get("contact_name"))
    direct_email = email and contact.get("email_type") == "named_business" and not contact.get("is_role_account")
    direct_phone = phone and contact.get("phone_type") in {"mobile_business", "direct_business"}
    if inferred:
        priority = "inferred"
    elif explicit and (direct_email or direct_phone):
        priority = "direct_business"
    elif contact.get("binding_status") == "company" or (email and (contact.get("is_role_account") or contact.get("email_type") in {"role_based", "company_generic"})) or (phone and contact.get("phone_type") == "company_main"):
        priority = "company_channel"
    else:
        priority = "unbound"
    quality = .65 + .35 * _unit(contact.get("confidence"), .5)
    freshness = .55 if _stale(contact) else 1
    conflict = .65 if tags & {"conflict", "conflicting", "binding_conflict"} or contact.get("conflicts") else 1
    validation = 1
    if email and not phone and (contact.get("email_status") == "risky" or contact.get("catch_all_status") in {"catch_all", "risky"} or contact.get("disposable_status") in {"disposable", "yes", "true"}):
        validation = .7
    return priority, round(_PRIORITY_BASE[priority] * quality * freshness * conflict * validation, 2)


def _rank_inplace(contacts) -> list:
    """Annotate and sort contacts that the caller already owns."""
    ranked = list(contacts or [])
    for contact in ranked:
        contact["contact_priority"], contact["priority_score"] = _contact_priority(contact)
    return sorted(ranked, key=lambda contact: (
        -contact["priority_score"], str(contact.get("contact_id") or ""),
        str(contact.get("contact_name") or "").casefold(),
        str(contact.get("email") or "").casefold(), str(contact.get("phone") or "")))


def rank_contacts(contacts) -> list:
    """Copy contacts, annotate priority, and order best available channels first."""
    return _rank_inplace(deepcopy(list(contacts or [])))


def _primary(contacts, field):
    # A named direct telephone should not promote an accompanying role mailbox
    # above an independently observed named business email (or vice versa).
    candidates = deepcopy(contacts)
    for contact in candidates:
        if field == "email":
            contact["phone"] = None
        else:
            contact["email"] = None
            contact["email_type"] = None
    return next((contact[field] for contact in _rank_inplace(candidates)
                 if contact["priority_score"] > 0 and contact.get(field)), None)


def score_company(record, *, copy=True) -> dict:
    """Recompute scores from retained facts, including records predating scoring.

    Lead points: qualification 35, best contact 45, email/phone coverage 8,
    independent evidence 7, factual completeness 5, minus conflict penalties.
    Uncertain leads are capped at 59 and excluded leads at 20. Evidence
    confidence uses source authority and observed contact confidence separately.

    Internal selection passes already own a detached row and may pass
    ``copy=False``; the public default keeps returning a detached record.
    """
    company = deepcopy(record) if copy else record
    contacts = _rank_inplace(company.get("contacts"))
    company["contacts"] = contacts
    usable = [contact for contact in contacts if contact["priority_score"] > 0]
    observed = [contact for contact in usable if contact["contact_priority"] != "inferred"]
    company["main_email"] = _primary(usable, "email")
    company["main_phone"] = _primary(usable, "phone")
    qualification = company.get("qualification") or {}
    decision = "excluded" if company.get("status") == "excluded" else qualification.get("decision")
    assessed = qualification.get("decision") in {"qualified", "excluded", "uncertain"}
    qualification_value = qualification.get("confidence", company.get("qualification_score")) if assessed else None
    # Accept percentage scores from older consumers while keeping the new
    # qualification contract normalized to 0..1.
    qualification_value = _number(qualification_value, .7 if company.get("status") == "qualified" else .4)
    if qualification_value > 1:
        qualification_value /= 100
    qualification_value = _unit(qualification_value)
    if assessed:
        company["qualification_score"] = round(qualification_value, 3)
    else:
        # Legacy extraction status is not an assessment of segment eligibility.
        # Keep its conservative sorting fallback internal to the point formula.
        company.pop("qualification_score", None)
    qualification_points = min(.45, qualification_value) if decision == "uncertain" else 0 if decision == "excluded" else qualification_value

    authorities, origins = [], set()
    for source in company.get("sources") or []:
        url = source.get("source_url")
        if not url:
            continue
        origin = _source_domain(url)
        if origin:
            origins.add(origin)
        # Each row is a field observation, not an independent source.
        authority = _SOURCE_WEIGHTS.get(source.get("source_type"), .55)
        authorities.append(min(authority, _unit(source.get("confidence"), authority)))
    source_quality = max(authorities, default=.4)
    if "qualification" in company:
        independent_count = max(0, int(_number(qualification.get("independent_sources"), 0)))
        if "evidence" in qualification:
            matched_origins = {_source_domain(evidence.get("source_url"))
                               for evidence in qualification.get("evidence") or []
                               if isinstance(evidence, dict) and evidence.get("matched_terms")}
            independent_count = min(independent_count, len(matched_origins - {""}))
    else:
        independent_count = len(origins)
    corroboration = min(1, independent_count / 3)
    contact_confidences = []
    for contact in observed:
        quality = _unit(contact.get("confidence"), .5)
        if _stale(contact):
            quality *= .55
        if contact.get("conflicts") or set(contact.get("tags") or []) & {"conflict", "conflicting", "binding_conflict"}:
            quality *= .65
        if _email_usable(contact) and not _phone_usable(contact) and (contact.get("email_status") == "risky" or contact.get("disposable_status") in {"disposable", "yes", "true"}):
            quality *= .7
        contact_confidences.append(quality)
    contact_quality = max(contact_confidences, default=0)
    completeness = sum(bool(company.get(field)) for field in (
        "company_name", "country_code", "website_url", "industry_text", "address")) / 5
    conflicts = len(company.get("conflicts") or [])
    evidence_penalty = min(.2, conflicts * .05)
    confidence = .55 * contact_quality + .3 * source_quality + .15 * completeness - evidence_penalty
    if not observed:
        confidence = min(.45, confidence)
    if decision == "uncertain":
        confidence *= .8
    elif decision == "excluded":
        confidence *= .3
    company["confidence"] = round(_unit(confidence), 3)
    channels = int(any(_email_usable(contact) for contact in usable)) + int(any(_phone_usable(contact) for contact in usable))
    breakdown = {"version": SCORING_RULE_VERSION,
                 "qualification": round(35 * qualification_points, 2),
                 "best_contact": round(.45 * max((contact["priority_score"] for contact in usable), default=0), 2),
                 "channel_coverage": channels * 4,
                 "source_corroboration": round(7 * corroboration, 2),
                 "completeness": round(5 * completeness, 2),
                 "conflict_penalty": -min(15, conflicts * 5)}
    raw_score = sum(value for value in breakdown.values() if isinstance(value, (int, float)))
    cap = 20 if decision == "excluded" else 59 if decision == "uncertain" else 100
    if not usable:
        cap = min(cap, 40)
    breakdown["score_cap"] = cap
    company["lead_score"] = round(max(0, min(cap, raw_score)), 2)
    company["score_breakdown"] = breakdown
    return company
