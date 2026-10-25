"""Network-free enterprise selection, channel ownership and delivery policy.

This is the common truth used by progress, metrics and export. It consumes
records and returns detached projections; it never stores data or fetches URLs.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import unquote

from ..rules.contacts import is_contact_form_fields, normalize_domain, validate_email, validate_phone
from ..rules.ranking import rank_contacts, score_company
from ..rules.evidence import literal_value, website_identity, subject_excerpt
from .fields import EMAIL_CHANNEL_FIELDS


def normalize_filters(filters):
    """Canonicalize equivalent supported filters for cross-entry-point hashes."""
    result = {}
    supplied = dict(filters or {})
    aliases = {"country_code": "country", "industry_text": "industry", "email_type": "contact_type", "is_named": "named_only", "confidence": "min_confidence"}
    # Use the same precedence as the view predicate when both names occur.
    for canonical, alias in (("country_code", "country"), ("industry", "industry_text"), ("contact_type", "email_type"),
                             ("named_only", "is_named"), ("min_confidence", "confidence")):
        if canonical in supplied and supplied[canonical] is not None and supplied[canonical] != "":
            supplied.pop(alias, None)
    for key, value in supplied.items():
        if value is None or value == "" or value == []:
            continue
        key = aliases.get(key, key)
        if key in {"min_confidence", "min_contact_confidence", "min_lead_score"} and value == 0:
            continue
        if key in {"has_email", "has_phone", "named_only", "has_named_contact"} and isinstance(value, str) and value.casefold() in {"true", "false"}:
            value = value.casefold() == "true"
        if isinstance(value, str):
            value = value.strip().casefold()
        if isinstance(value, (list, tuple, set)):
            value = sorted(set(str(item).strip().casefold() for item in value))
        result[key] = value
    country = result.pop("country", None)
    if "countries" not in result and country:
        result["countries"] = [country]
    return result



def observation_time(value):
    if value is None:
        raise ValueError("metrics as_of is required")
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("metrics as_of must be an ISO datetime") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("metrics as_of must include a timezone")
    return result.astimezone(timezone.utc)



def eligibility(company):
    """Read modern decisions first; never fabricate a historical assessment."""
    assessment = company.get("qualification") or {}
    decision = assessment.get("decision")
    if decision in {"qualified", "excluded", "uncertain"}:
        return decision, "qualification"
    if company.get("status") in {"qualified", "insufficient"} and any(
            str(reason).startswith("公开内容命中:") for reason in company.get("match_reasons") or []):
        return "qualified", "legacy"
    if company.get("status") == "excluded":
        return "excluded", "legacy"
    return "unevaluated", "unevaluated"


def qualification_cohort(rows):
    return [row for row in coalesce_companies(rows) if eligibility(row)[0] == "qualified"]



def normalize_name(value):
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def normalize_phone(value):
    return re.sub(r"\D", "", str(value or "").split("ext", 1)[0])


def suppression_sets(suppressions):
    result = {"domain": set(), "company": set(), "email": set(), "phone": set()}
    if isinstance(suppressions, dict):
        expanded = []
        for kind, values in suppressions.items():
            kind = kind.rstrip("s")
            kind = {"companie": "company", "company_name": "company"}.get(kind, kind)
            for value in values if isinstance(values, (list, tuple, set)) else [values]:
                expanded.append({"kind": kind, "value": value})
        suppressions = expanded
    for suppression in suppressions or []:
        if isinstance(suppression, str):
            kind = "email" if "@" in suppression else "domain"
            value = suppression
        else:
            kind = suppression.get("kind") or suppression.get("type") or suppression.get("match_type")
            value = suppression.get("value") or suppression.get("normalized_value")
        kind = {"company_name": "company", "company_id": "company"}.get(kind, kind)
        if kind not in result or value is None:
            continue
        normalized = normalize_domain(value) if kind == "domain" else normalize_name(value) if kind == "company" else normalize_phone(value) if kind == "phone" else str(value).strip().casefold()
        if normalized:
            result[kind].add(normalized)
    return result


def matches(value, allowed, *, substring=False):
    if allowed is None or allowed == [] or allowed == "":
        return True
    choices = allowed if isinstance(allowed, (list, tuple, set)) else [allowed]
    value = str(value or "").casefold()
    return any(str(choice).casefold() in value if substring else value == str(choice).casefold() for choice in choices)


def _safe_number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _identity(company):
    """Sharing a domain does not establish a shared legal entity or branch."""
    domain = normalize_domain(company.get("domain") or company.get("website_url") or "")
    return (domain,
            normalize_name(company.get("company_name")), str(company.get("country_code") or "").upper(),
            str(company.get("branch_id") or company.get("branch_name") or company.get("location_id") or (normalize_name(company.get("address")) if not domain else "")))


def ownership_order(companies):
    """A cohort uses one frozen policy, including when historical ordinals lack."""
    values = list(companies)
    use_ordinal = bool(values) and all(isinstance(company.get("candidate_ordinal"), int)
        and not isinstance(company.get("candidate_ordinal"), bool) for company in values)
    def order(company):
        if use_ordinal:
            position = company["candidate_ordinal"]
        else:
            try:
                created = datetime.fromisoformat(str(company.get("created_at") or "").replace("Z", "+00:00"))
                position = created.replace(tzinfo=created.tzinfo or timezone.utc).astimezone(timezone.utc)
            except (TypeError, ValueError):
                position = datetime.min.replace(tzinfo=timezone.utc)
        return position, str(company.get("company_id") or ""), normalize_name(company.get("company_name")), _identity(company)
    return sorted(values, key=order)


def coalesce_companies(companies):
    merged = {}
    # Deterministic ownership of duplicated channels and merged entity IDs.
    for original in ownership_order(companies):
        item = deepcopy(original)
        item["contacts"] = rank_contacts(item.get("contacts"))
        identity = _identity(item)
        if identity not in merged:
            merged[identity] = item
            continue
        target = merged[identity]
        aliases = {item.get("company_id"): target.get("company_id")}
        for contact in item.get("contacts", []):
            existing = next((other for other in target.get("contacts", [])
                if (contact.get("email") and str(contact["email"]).lower() == str(other.get("email") or "").lower())
                or (not contact.get("email") and contact.get("phone") and contact["phone"] == other.get("phone"))
                or (not contact.get("email") and not contact.get("phone") and contact.get("contact_name")
                    and contact.get("contact_name") == other.get("contact_name") and contact.get("title") == other.get("title"))), None)
            contact["company_id"] = target.get("company_id")
            if existing:
                aliases[contact.get("contact_id")] = existing.get("contact_id")
                urls = list(dict.fromkeys(existing.get("source_urls", []) + contact.get("source_urls", [])))
                opted_out = bool(existing.get("opt_out") or contact.get("opt_out"))
                # Prefer the strongest observation of the same channel; an
                # inferred first occurrence must not hide a later public binding.
                if contact["priority_score"] > rank_contacts([existing])[0]["priority_score"]:
                    identifier = existing.get("contact_id")
                    combined = {**existing, **{key: value for key, value in contact.items() if value is not None}}
                    combined["contact_id"] = identifier
                    existing.update(combined)
                existing["source_urls"] = urls
                existing["opt_out"] = opted_out
            else:
                target.setdefault("contacts", []).append(contact)
        for source in item.get("sources", []):
            source["entity_id"] = aliases.get(source.get("entity_id"), source.get("entity_id"))
            if source not in target.setdefault("sources", []):
                target["sources"].append(source)
        target["demo_only"] = bool(target.get("demo_only") or item.get("demo_only"))
    return list(merged.values())


def redact_qualification(company, original, permitted_emails, permitted_phones, suppressed):
    """Nested qualification evidence obeys the same channel export boundary."""
    prior_contacts = original.get("contacts") or []
    prior_emails = {str(contact.get("email") or "").strip().casefold() for contact in prior_contacts}
    prior_emails.add(str(original.get("main_email") or "").strip().casefold())
    prior_phones = {normalize_phone(contact.get("phone") or contact.get("phone_raw")) for contact in prior_contacts}
    prior_phones.add(normalize_phone(original.get("main_phone")))
    forbidden_emails = (prior_emails - {str(email).casefold() for email in permitted_emails}) | suppressed["email"]
    forbidden_phones = (prior_phones - {normalize_phone(phone) for phone in permitted_phones}) | suppressed["phone"]
    forbidden_emails.discard("")
    forbidden_phones.discard("")
    removed = object()

    def clean(value):
        if isinstance(value, str):
            decoded = unquote(value).casefold()
            digits = normalize_phone(decoded)
            if any(email in decoded for email in forbidden_emails) or any(phone in digits for phone in forbidden_phones):
                return removed
            return value
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                cleaned = clean(item)
                if cleaned is not removed:
                    result[key] = cleaned
            # An evidence row without its actual source URL is not traceable.
            if "source_url" in value and "source_url" not in result:
                return removed
            return result
        if isinstance(value, list):
            result = []
            for item in value:
                cleaned = clean(item)
                if cleaned is not removed:
                    result.append(cleaned)
            return result
        return value

    for field in ('qualification', 'sources', 'website_binding_evidence'):
        if field in company:
            cleaned = clean(company[field])
            company[field] = ({} if field == 'qualification' else []) if cleaned is removed else cleaned


def filter_companies(companies, *, mode="balanced", filters=None, suppressions=None, freshness_days=180, now=None):
    """Return a detached, deduplicated export view, including only permitted facts.

    Domain/company suppressions remove a whole company; email/phone suppressions
    remove matching contact records. Main channels and source rows are rebuilt
    afterwards, so removed data cannot escape through the Companies/Sources sheet.
    """
    if mode not in {"verified_only", "balanced", "max_coverage"}:
        raise ValueError("Unknown export mode")
    filters = normalize_filters(filters)
    current = now if isinstance(now, datetime) else datetime.fromisoformat(str(now).replace("Z", "+00:00")) if now else datetime.now(timezone.utc)
    current = current.replace(tzinfo=current.tzinfo or timezone.utc)
    suppressed = suppression_sets(suppressions)
    seen_companies, seen_emails, seen_phones = set(), set(), set()
    result = []
    coalesced = coalesce_companies(companies)
    # Freeze channel ownership before quality, contact, score and presence
    # filters. An output predicate must never transfer a channel to a different
    # enterprise. Enterprise attributes may deliberately define a sub-cohort.
    eligible = []
    for company in coalesced:
        domain = normalize_domain(company.get("domain") or company.get("website_url") or "")
        if (any(domain == d or domain.endswith("." + d) for d in suppressed["domain"])
                or normalize_name(company.get("company_name")) in suppressed["company"]
                or normalize_name(company.get("company_id")) in suppressed["company"]):
            continue
        if company.get("expires_at"):
            try:
                expiry = datetime.fromisoformat(str(company["expires_at"]).replace("Z", "+00:00"))
                if expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) <= current:
                    continue
            except (TypeError, ValueError):
                pass
        if filters.get("q") and str(filters["q"]).casefold() not in " ".join(str(company.get(field) or "") for field in ("company_name", "domain", "country_code", "region", "city", "industry_text")).casefold():
            continue
        if not matches(company.get("country_code"), filters.get("countries", filters.get("country_code", filters.get("country")))):
            continue
        if not matches(company.get("industry_text"), filters.get("industry", filters.get("industry_text")), substring=True):
            continue
        if not matches(company.get("company_type"), filters.get("company_type")) or not matches(company.get("status"), filters.get("status")):
            continue
        eligible.append(company)
    owners = {"email": {}, "phone": {}}
    for company in eligible:
        for contact in sorted(company.get("contacts") or [], key=lambda value: (str(value.get("contact_id") or ""), str(value.get("email") or "").casefold(), normalize_phone(value.get("phone") or value.get("phone_raw")))):
            if contact.get("opt_out"):
                continue
            email = str(contact.get("email") or "").strip().casefold()
            phone = normalize_phone(contact.get("phone") or contact.get("phone_raw"))
            if email in suppressed["email"] or phone in suppressed["phone"]:
                continue
            marker = (company.get("company_id"), contact.get("contact_id"))
            if email:
                owners["email"].setdefault(email, marker)
            if phone:
                owners["phone"].setdefault(phone, marker)
    for original in eligible:
        company = deepcopy(original)
        domain = normalize_domain(company.get("domain") or company.get("website_url") or "")
        if (any(domain == d or domain.endswith("." + d) for d in suppressed["domain"])
                or normalize_name(company.get("company_name")) in suppressed["company"]
                or normalize_name(company.get("company_id")) in suppressed["company"]):
            continue
        if filters.get("q") and str(filters["q"]).casefold() not in " ".join(str(company.get(field) or "") for field in ("company_name", "domain", "country_code", "region", "city", "industry_text")).casefold():
            continue
        if not matches(company.get("country_code"), filters.get("countries", filters.get("country_code", filters.get("country")))):
            continue
        if not matches(company.get("industry_text"), filters.get("industry", filters.get("industry_text")), substring=True):
            continue
        if not matches(company.get("company_type"), filters.get("company_type")):
            continue
        if not matches(company.get("status"), filters.get("status")):
            continue
        identity = _identity(company)
        if identity in seen_companies:
            continue
        contacts = []
        pending_emails, pending_phones = set(), set()
        for contact in sorted(company.get("contacts", []), key=lambda value: (str(value.get("contact_id") or ""), str(value.get("email") or "").casefold(), normalize_phone(value.get("phone") or value.get("phone_raw")))):
            if contact.get("opt_out"):
                continue
            try:
                retrieved = datetime.fromisoformat(str(contact.get("retrieved_at") or "").replace("Z", "+00:00"))
                retrieved = retrieved.replace(tzinfo=retrieved.tzinfo or timezone.utc)
                if (current - retrieved).days > int(freshness_days):
                    contact["tags"] = list(dict.fromkeys(contact.get("tags", []) + ["stale"]))
            except (ValueError, TypeError):
                pass
            email = str(contact.get("email") or "").strip().lower()
            phone = normalize_phone(contact.get("phone") or contact.get("phone_raw"))
            if (email and email in suppressed["email"]) or (phone and phone in suppressed["phone"]):
                continue
            wanted_type = filters.get("contact_type", filters.get("email_type"))
            if not (matches(contact.get("email_type"), wanted_type) or matches(contact.get("phone_type"), wanted_type)):
                continue
            if not matches(contact.get("email_status"), filters.get("email_status")):
                continue
            if not matches(contact.get("phone_status"), filters.get("phone_status")):
                continue
            if _safe_number(contact.get("confidence")) < _safe_number(filters.get("min_contact_confidence", 0)):
                continue
            named = filters.get("named_only", filters.get("is_named"))
            if named is not None and bool(contact.get("contact_name")) != bool(named):
                continue
            if mode == "verified_only":
                stale = "stale" in contact.get("tags", [])
                if contact.get("email_status") != "verified" or contact.get("inferred") or contact.get("email_type") == "inferred" or stale:
                    for field in EMAIL_CHANNEL_FIELDS:
                        contact[field] = None
                if contact.get("phone_status") != "valid" or stale:
                    for field in ("phone", "phone_raw", "phone_extension", "phone_type", "phone_status", "line_type"):
                        contact[field] = None
                if not contact.get("email") and not contact.get("phone"):
                    continue
            elif mode == "balanced":
                if contact.get("email_status") == "invalid":
                    for field in EMAIL_CHANNEL_FIELDS:
                        contact[field] = None
                if contact.get("phone_status") == "invalid":
                    for field in ("phone", "phone_raw", "phone_extension", "phone_type", "phone_status", "line_type"):
                        contact[field] = None
                if not contact.get("email") and not contact.get("phone") and not contact.get("contact_name"):
                    continue
            if contact.get("email"):
                if owners["email"].get(email) != (company.get("company_id"), contact.get("contact_id")) or email in seen_emails or email in pending_emails:
                    # Preserve an independently supplied phone instead of duplicating an email.
                    for field in EMAIL_CHANNEL_FIELDS:
                        contact[field] = None
                else:
                    pending_emails.add(email)
            if contact.get("phone"):
                if owners["phone"].get(phone) != (company.get("company_id"), contact.get("contact_id")) or phone in seen_phones or phone in pending_phones:
                    for field in ("phone", "phone_raw", "phone_extension", "phone_type", "phone_status", "line_type"):
                        contact[field] = None
                else:
                    pending_phones.add(phone)
            if not contact.get("email") and not contact.get("phone") and (mode == "verified_only" or not contact.get("contact_name")):
                continue
            contacts.append(contact)
        contact_filter = any(filters.get(field) is not None and filters.get(field) != "" for field in
                             ("contact_type", "email_type", "email_status", "phone_status", "named_only", "is_named", "min_contact_confidence"))
        if contact_filter and not contacts:
            continue
        company["contacts"] = contacts
        # Conflict annotations are data too: redact contact values removed by a
        # suppression or verification filter instead of leaving a side channel.
        permitted_emails = {c["email"] for c in contacts if c.get("email")}
        permitted_phones = {c["phone"] for c in contacts if c.get("phone")}
        clean_conflicts = []
        for conflict in company.get("conflicts", []):
            if not isinstance(conflict, dict):
                conflict = {"field": "identity", "reason": str(conflict)}
            field = str(conflict.get("field") or "")
            if "email" in field or "phone" in field:
                permitted = permitted_emails if "email" in field else permitted_phones
                conflict["values"] = [value for value in conflict.get("values", []) if value in permitted]
                if len(conflict["values"]) < 2:
                    continue
            clean_conflicts.append(conflict)
        company["conflicts"] = clean_conflicts
        redact_qualification(company, original, permitted_emails, permitted_phones, suppressed)
        # Select primary channels from the retained contacts by business
        # priority, before rebuilding their field-level provenance.
        company = score_company(company)
        contacts = company["contacts"]
        company["email_count"] = sum(bool(c.get("email")) for c in contacts)
        company["phone_count"] = sum(bool(c.get("phone")) for c in contacts)
        company["named_contact_count"] = sum(bool(c.get("contact_name")) for c in contacts)
        company["contactable"] = bool(company["email_count"] or company["phone_count"])
        company["missing"] = [tag for tag in company.get("missing", []) if tag not in {"no_email", "no_phone", "no_named_contact"}]
        for key, count in (("no_email", company["email_count"]), ("no_phone", company["phone_count"]), ("no_named_contact", company["named_contact_count"])):
            if not count:
                company["missing"].append(key)
        if filters.get("has_email") is not None and bool(company["email_count"]) != bool(filters["has_email"]):
            continue
        if filters.get("has_phone") is not None and bool(company["phone_count"]) != bool(filters["has_phone"]):
            continue
        if filters.get("has_named_contact") is not None and any(c.get("contact_name") for c in contacts) != bool(filters["has_named_contact"]):
            continue
        if mode == "verified_only" and not contacts:
            continue
        entity_fields = {c["contact_id"]: c for c in contacts}
        entity_fields[company["company_id"]] = company
        source_rows, seen_sources = [], set()
        for source in company.get("sources", []):
            entity = entity_fields.get(source.get("entity_id"))
            if entity is None:
                continue
            field, value = source.get("field"), source.get("value")
            # Drop provenance for a removed or replaced contact field, including
            # pre-filter company aggregates which may contain a suppressed value.
            subset_evidence = field in {"social_urls", "source_urls"} and isinstance(value, list) and all(item in entity.get(field, []) for item in value)
            conflicting_evidence = field not in set(EMAIL_CHANNEL_FIELDS) | {"main_email", "main_phone", "phone", "phone_raw", "phone_extension", "phone_status", "phone_type", "line_type", "status"} and any(
                isinstance(conflict, dict) and conflict.get("field") == field and value in conflict.get("values", []) for conflict in company.get("conflicts", []))
            equivalent_name = field == "company_name" and normalize_name(entity.get(field)) == normalize_name(value)
            if field not in entity or (entity.get(field) != value and not subset_evidence and not conflicting_evidence and not equivalent_name):
                continue
            if field in {"contacts", "sources", "associated_people", "conflicts"}:
                continue
            if field in {"main_email", "email"} and (not entity.get(field) or str(value).lower() in suppressed["email"]):
                continue
            if field in {"main_phone", "phone", "phone_raw"} and (not entity.get(field) or normalize_phone(value) in suppressed["phone"]):
                continue
            if mode == "verified_only" and (("email" in str(field) or field in {"syntax_status", "mx_status"}) and not entity.get("email", entity.get("main_email"))):
                continue
            if mode == "verified_only" and (("phone" in str(field) or field == "line_type") and not entity.get("phone", entity.get("main_phone"))):
                continue
            marker = json.dumps(source, sort_keys=True, ensure_ascii=False, default=str)
            if marker not in seen_sources:
                seen_sources.add(marker)
                source_rows.append(source)
        company["sources"] = source_rows
        company["source_count"] = len({s.get("source_url") for s in source_rows if s.get("source_url")})
        company = score_company(company)
        contacts = company["contacts"]
        source_rows = company["sources"]
        if company["confidence"] < _safe_number(filters.get("min_confidence", filters.get("confidence", 0))):
            continue
        if company["lead_score"] < _safe_number(filters.get("min_lead_score", 0)):
            continue
        for contact in contacts:
            evidence = next((source for source in source_rows if source.get("entity_id") == contact["contact_id"]), None)
            if evidence:
                for field in ("tags", "confidence", "confidence_components", "contact_priority", "priority_score"):
                    if field in contact and not any(source.get("entity_id") == contact["contact_id"] and source.get("field") == field and source.get("value") == contact[field] for source in source_rows):
                        source_rows.append({**evidence, "field": field, "value": contact[field], "method": "derived_export_freshness"})
        # Aggregates are rebuilt after filtering. Preserve their traceability to
        # the retained facts, rather than reusing pre-filter aggregate evidence.
        context = next((source for source in source_rows if source.get("source_url")), None)
        for field in ("main_email", "main_phone", "source_count", "email_count", "phone_count", "named_contact_count", "contactable", "qualification_score", "confidence", "lead_score", "score_breakdown", "missing", "conflicts"):
            value = company.get(field)
            if value is None or value == [] or not context:
                continue
            evidence = context
            if field in {"main_email", "main_phone"}:
                contact_field = "email" if field == "main_email" else "phone"
                evidence = next((source for source in source_rows if source.get("field") == contact_field and source.get("value") == value), context)
            if not any(source.get("entity_id") == company["company_id"] and source.get("field") == field and source.get("value") == value for source in source_rows):
                source_rows.append({**evidence, "entity_id": company["company_id"], "field": field, "value": value,
                                    "method": "derived_export_selection"})
        seen_companies.add(identity)
        seen_emails.update(pending_emails)
        seen_phones.update(pending_phones)
        result.append(company)
    return sorted(result, key=lambda company: (-company["lead_score"], str(company.get("company_id") or ""), normalize_name(company.get("company_name"))))



DELIVERY_POLICY_VERSION = "b2b-delivery-2"
PHONE_FIELDS = ("phone", "phone_raw", "phone_extension", "phone_type", "phone_status", "line_type")
BAD_TAGS = {"inferred", "stale", "expired", "binding_conflict", "conflicting", "conflict"}
INFERRED_METHODS = {"inferred", "pattern_inferred", "known_pattern_evidence"}


def _public_url(value, company):
    from ..web.security import normalize_url
    try:
        return normalize_url(value, fixture=bool(company.get("demo_only")))
    except Exception:
        return None


def _fresh(value, current, days):
    try:
        when = observation_time(value)
    except ValueError:
        return False
    return timedelta(0) <= current - when <= timedelta(days=days)


def _observed(source):
    method = str(source.get("method") or "").lower()
    return bool(method) and source.get("source_type") != "inferred" and not any(
        marker in method for marker in INFERRED_METHODS)


def _website_ambiguous(company):
    if company.get('website_binding_status') == 'conflicting' or website_identity(company)['status'] == 'conflicting':
        return True
    domains = {normalize_domain(company.get("domain") or company.get("website_url") or "")}
    for conflict in company.get("conflicts") or []:
        if isinstance(conflict, dict) and conflict.get("field") in {"domain", "website_url", "website"}:
            domains.update(normalize_domain(value) for value in conflict.get("values") or [])
    return len(domains - {""}) > 1


def _bound_source(company, url, source=None):
    """An official-site claim cannot bind a different site's subject.

    A directory can supply a public company channel when the same page also
    identifies the enterprise. Merely discovering an unrelated URL is not a
    subject binding, even if a contact record carries this company's ID.
    """
    if not _public_url(url, company):
        return False
    own_domain = normalize_domain(company.get("website_url") or company.get("domain") or "")
    source_domain = normalize_domain(url)
    same_site = bool(own_domain and (source_domain == own_domain or source_domain.endswith("." + own_domain)))
    kind = (source or {}).get("source_type")
    if kind in {"official_site", "company_website"}:
        return same_site and website_identity(company)['status'] == 'confirmed'
    if same_site:
        return website_identity(company)['status'] == 'confirmed'
    return any(row.get("entity_id") == company.get("company_id")
        and row.get("field") == "company_name" and normalize_name(row.get("value")) == normalize_name(company.get("company_name"))
        and subject_excerpt(company.get('company_name'), row.get('source_excerpt'))
        and row.get("source_url") == url and _observed(row)
        for row in company.get("sources") or [])


def _channel_evidence(company, contact, field, value, current, days):
    if contact.get("company_id") not in {None, company.get("company_id")}:
        return False
    if contact.get("binding_status") not in {"company", "explicit"}:
        return False
    if (contact.get("inferred") or contact.get("email_type") == "inferred"
            or set(contact.get("tags") or []) & BAD_TAGS or contact.get("conflicts")):
        return False
    matching = [row for row in company.get("sources") or []
        if row.get("entity_id") == contact.get("contact_id") and row.get("field") == field
        and str(row.get("value") or "").casefold() == str(value).casefold()]
    if matching:
        return any(_observed(row) and _fresh(row.get("retrieved_at"), current, days)
            and literal_value(field, value, row.get('source_excerpt'), company.get('country_code'))
            and _bound_source(company, row.get("source_url"), row) for row in matching)
    return False


def _form_evidence(company, current, days):
    url = company.get("contact_form_url")
    if not url or not _public_url(url, company):
        return False
    return any(row.get("entity_id") == company.get("company_id")
        and row.get("field") == "contact_form_url" and row.get("value") == url
        and row.get("source_url") == url and row.get("method") == "observed_contact_form"
        and is_contact_form_fields(row.get("form_fields"))
        and _observed(row) and _fresh(row.get("retrieved_at"), current, days)
        and _bound_source(company, url, row) for row in company.get("sources") or [])


def delivery_companies(companies, *, suppressions=None, freshness_days=180, now=None) -> list:
    """Detached, ranked qualified enterprises with a fresh public channel.

    Balanced export selection freezes existing deduplication/channel ownership
    before the stricter delivery check. It therefore cannot move a shared phone
    or mailbox to another enterprise merely because the owner fails delivery.
    Returned rows retain only channels eligible for this delivery policy.
    """
    current = observation_time(now) if now is not None else datetime.now(timezone.utc)
    days = int(freshness_days)
    if days < 0:
        raise ValueError("freshness_days cannot be negative")
    selected = filter_companies(companies, mode="balanced", suppressions=suppressions,
                                freshness_days=days, now=current)
    result = []
    form_owners = {}
    for item in ownership_order(selected):
        if item.get("contact_form_url"):
            form_owners.setdefault(item["contact_form_url"], item.get("company_id"))
    for company in qualification_cohort(selected):
        if company.get("status") == "excluded" or _website_ambiguous(company):
            continue
        original = deepcopy(company)
        contacts = []
        for contact in company.get("contacts") or []:
            email = contact.get("email")
            email_valid = bool(email and contact.get("email_status") in {None, "", "unverified", "unknown", "verified"}
                and contact.get("mx_status") in {None, "", "valid", "unknown", "not_checked"}
                and contact.get("syntax_status") in {None, "", "valid", "unknown", "not_checked"}
                and contact.get("disposable_status") not in {"disposable", "yes", "true"}
                and validate_email(email, mx_lookup=lambda _: "not_checked")["syntax_status"] == "valid"
                and _channel_evidence(company, contact, "email", email, current, days))
            phone = contact.get("phone")
            phone_valid = bool(phone and contact.get("phone_status") == "valid"
                and validate_phone(phone, company.get("country_code"))["phone_status"] == "valid"
                and _channel_evidence(company, contact, "phone", phone, current, days))
            if not email_valid:
                for field in EMAIL_CHANNEL_FIELDS:
                    contact[field] = None
            if not phone_valid:
                for field in PHONE_FIELDS:
                    contact[field] = None
            if email_valid or phone_valid:
                contacts.append(contact)
        company["contacts"] = contacts
        form = company.get("contact_form_url")
        if not _form_evidence(company, current, days) or form_owners.get(form) != company.get("company_id"):
            company["contact_form_url"] = None
        if not contacts and not company.get("contact_form_url"):
            continue
        redact_qualification(company, original, {contact["email"] for contact in contacts if contact.get("email")},
            {contact["phone"] for contact in contacts if contact.get("phone")}, suppression_sets(suppressions))
        # Rebuild sources and aggregates after channel removal using the same
        # export redaction logic. A form-only enterprise survives balanced mode.
        clean = filter_companies([company], mode="balanced", freshness_days=days, now=current)[0]
        clean["contactable"] = True
        clean["delivery_channels"] = (["email"] if clean.get("email_count") else []) + (
            ["phone"] if clean.get("phone_count") else []) + (["contact_form"] if clean.get("contact_form_url") else [])
        clean["delivery_policy_version"] = DELIVERY_POLICY_VERSION
        for row in clean.get("sources") or []:
            if row.get("entity_id") == clean.get("company_id") and row.get("field") == "contactable":
                row["value"] = True
                row["method"] = "derived_delivery_selection"
        result.append(score_company(clean))
    return sorted(result, key=lambda row: (-row["lead_score"], str(row.get("company_id") or ""), normalize_name(row.get("company_name"))))


def select_export_companies(companies, *, mode="balanced", filters=None, suppressions=None,
                            freshness_days=180, now=None, delivery_only=False, delivery_limit=None):
    """Return the common output projection and its uncapped delivery count.

    Reapply delivery after channel filters so counts and primary channels always
    reflect the selected records. File encoders contain no delivery predicates.
    """
    eligible_count = None
    export_input = companies
    if delivery_only:
        export_input = delivery_companies(companies, suppressions=suppressions,
                                         freshness_days=freshness_days, now=now)
        eligible_count = len(export_input)
    selected = filter_companies([row for row in export_input if row.get("status") != "excluded"],
        mode=mode, filters=filters, suppressions=suppressions, freshness_days=freshness_days, now=now)
    if delivery_only:
        selected = delivery_companies(selected, suppressions=suppressions,
                                      freshness_days=freshness_days, now=now)
        if delivery_limit is not None:
            if isinstance(delivery_limit, bool) or int(delivery_limit) < 1:
                raise ValueError("delivery_limit must be positive")
            selected = selected[:int(delivery_limit)]
    return selected, eligible_count
