"""Public business contact extraction with conservative validation and field provenance.

DNS/MX confirms a domain can receive mail, never the existence of a mailbox.
The extractor deliberately does not probe SMTP or infer telephone numbers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html as html_module
import json
import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
import phonenumbers
from .evidence import imported_channel_sources, literal_value, website_identity, subject_excerpt
from app.web.parsing import parsed_html

EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+)", re.I)
GENERIC = {"info", "contact", "hello", "office", "admin", "enquiries", "inquiries", "mail", "reception"}
ROLES = {"sales", "support", "operations", "ops", "marketing", "billing", "accounts", "hr", "careers", "export", "import", "service"}
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "live.com"}
SOCIAL_DOMAINS = {"linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com", "tiktok.com", "xing.com", "github.com"}
SOURCE_WEIGHT = {"official_site": .9, "company_website": .9, "registry": .85, "maps": .8,
                 "directory": .7, "association": .7, "exhibition": .7, "news": .65,
                 "public_social": .6, "search": .5, "inferred": .4}


def _id(prefix, *values):
    return prefix + "_" + hashlib.sha256("|".join(str(v or "") for v in values).encode()).hexdigest()[:20]


def _time(value=None):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


def _stamp(value=None):
    return _time(value).isoformat().replace("+00:00", "Z")


def normalize_domain(value):
    try:
        host = urlsplit(str(value) if "://" in str(value) else "https://" + str(value)).hostname or ""
        return host.lower().removeprefix("www.").rstrip(".").encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""


def validate_email(value, *, mx_lookup=None):
    email = unquote(html_module.unescape(str(value or ""))).strip().removeprefix("mailto:").split("?", 1)[0].lower()
    syntax = "valid"
    try:
        local, domain = email.rsplit("@", 1)
        domain = domain.encode("idna").decode("ascii")
        email = local + "@" + domain
        if (len(email) > 254 or len(local) > 64 or not local or local.startswith(".") or local.endswith(".")
                or ".." in local or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local, re.I)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", domain, re.I)):
            syntax = "invalid"
    except (ValueError, UnicodeError):
        syntax = "invalid"
    mx = "not_checked"
    if syntax == "valid":
        try:
            result = (mx_lookup or (lambda _: "unknown"))(email.rsplit("@", 1)[1])
            mx = result if isinstance(result, str) and result in {"valid", "invalid", "unknown", "not_checked"} else ("valid" if result else "invalid")
        except Exception:
            mx = "unknown"
    return {"email": email, "syntax_status": syntax, "mx_status": mx,
            "email_status": "invalid" if syntax == "invalid" or mx == "invalid" else "unverified",
            "catch_all_status": "not_checked", "smtp_status": "not_performed", "disposable_status": "unknown",
            "is_role_account": bool(email.split("@", 1)[0] in GENERIC | ROLES)}


def validate_phone(value, country_code=None):
    raw = unquote(str(value or "")).removeprefix("tel:").strip()
    result = {"phone": None, "phone_raw": raw, "phone_extension": None,
              "phone_status": "invalid", "line_type": "unknown"}
    try:
        number = phonenumbers.parse(raw, str(country_code).upper() if country_code else None)
        result["phone_extension"] = number.extension or None
        if phonenumbers.is_possible_number(number):
            result["phone"] = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
            result["phone_status"] = "valid" if phonenumbers.is_valid_number(number) else "risky"
            number_type = phonenumbers.number_type(number)
            result["line_type"] = {
                phonenumbers.PhoneNumberType.FIXED_LINE: "fixed_line",
                phonenumbers.PhoneNumberType.MOBILE: "mobile",
                phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_line_or_mobile",
                phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
                phonenumbers.PhoneNumberType.VOIP: "voip",
                phonenumbers.PhoneNumberType.PREMIUM_RATE: "premium_rate",
            }.get(number_type, "unknown")
    except phonenumbers.NumberParseException:
        pass
    return result


def _decoded(text):
    text = html_module.unescape(str(text or ""))
    text = re.sub(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\))\s*", "@", text, flags=re.I)
    return re.sub(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\))\s*", ".", text, flags=re.I)


def _emails(text):
    return list(dict.fromkeys(m.group(1).strip(".,;:") for m in EMAIL_RE.finditer(_decoded(text))))


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _types(value):
    return {str(x).rsplit("/", 1)[-1].lower() for x in (value if isinstance(value, list) else [value])}


def _strings(value):
    if value is None:
        return []
    return [str(x) for x in value] if isinstance(value, list) else [str(value)]


def is_contact_form_fields(fields):
    """A newsletter/login/search box is not a business inquiry form.

    Imported research observations use the same minimum evidence as parsed HTML:
    an email/telephone reply field and a message/inquiry field in one actual form.
    """
    if not isinstance(fields, list):
        return False
    names = [str(value).lower() for value in fields if isinstance(value, str)]
    reply = any(re.search(r"email|e-mail|phone|telephone|\btel\b", name) for name in names)
    message = any(re.search(r"message|textarea|inquiry|enquiry|comments?|details", name) for name in names)
    return reply and message


def _email_type(email, named=False, inferred=False):
    if inferred:
        return "inferred"
    local, domain = email.rsplit("@", 1)
    if domain in PERSONAL_DOMAINS:
        return "personal_business"
    if local in GENERIC:
        return "company_generic"
    if local in ROLES:
        return "role_based"
    return "named_business" if named else "role_based"


def extract_company(candidate: dict, pages: list[dict], segment: dict, *, mx_lookup=None, now=None) -> dict:
    current = _time(now)
    collected = _stamp(candidate.get("retrieved_at") or current)
    website = str(candidate.get("website_url") or "")
    domain = normalize_domain(website or candidate.get("domain") or "")
    countries = segment.get("countries") or segment.get("country_codes") or []
    if isinstance(countries, str):
        countries = [countries]
    country = candidate.get("country_code")
    phone_region = country or (countries[0] if len(countries) == 1 else None)
    company_id = candidate.get("company_id") or _id("co", domain, candidate.get("company_name"), country,
                                                   candidate.get("branch_id") or candidate.get("branch_name"),
                                                   candidate.get("address") if not domain else None)
    source_url = candidate.get("source_url") or website
    source_type = candidate.get("source_type") or "official_site"
    company_name_is_hint = bool(candidate.get("company_name_is_domain_hint") or not candidate.get("company_name"))
    company = {"company_id": company_id, "company_name": candidate.get("company_name") or domain,
               "domain": domain or None, "website_url": website or None, "country_code": country,
               "region": candidate.get("region"), "city": candidate.get("city"), "address": candidate.get("address"),
               "industry_text": candidate.get("industry_text"), "company_type": candidate.get("company_type"),
               "employee_count": candidate.get("employee_count"), "contacts": [], "sources": [],
               "contact_form_url": None, "social_urls": [],
               "conflicts": [dict(conflict) for conflict in candidate.get("conflicts", [])
                   if isinstance(conflict, dict) and conflict.get("field") in {"domain", "website_url", "website"}],
               "missing": [], "demo_only": bool(candidate.get("demo_only", False))}
    company.update(source_id=candidate.get('source_id') or 'unknown', query_id=candidate.get('query_id') or 'unknown')
    for field in ("branch_id", "branch_name", "location_id", "revenue", "linkedin_url", "industry_codes", "description", "brands", "certifications", "products", "knows_about"):
        if candidate.get(field) is not None:
            company[field] = candidate[field]
    sources, contacts = company["sources"], company["contacts"]
    binding = website_identity(candidate, pages, now=current)
    company['website_binding_status'] = binding['status']
    company['website_binding_evidence'] = binding['evidence']
    source_keys = set()
    freshness = int(segment.get("contact_freshness_days") or segment.get("freshness_days") or 180)

    lineage_by_url = {}
    for evidence in [candidate, *candidate.get('sources', []), *pages]:
        if not isinstance(evidence, dict):
            continue
        reference = evidence.get('final_url') or evidence.get('source_url') or evidence.get('url')
        if not reference:
            continue
        lineage = {'source_id':evidence.get('source_id') or 'unknown',
                   'query_id':evidence.get('query_id') or 'unknown'}
        refs = lineage_by_url.setdefault(reference, [])
        if lineage not in refs:
            refs.append(lineage)

    def lineage_for(url):
        refs = lineage_by_url.get(url) or [{'source_id':'unknown', 'query_id':'unknown'}]
        # Multiple queries may legitimately cite one page; retain them all and
        # avoid silently picking one as authoritative for a channel.
        ids = {ref['source_id'] for ref in refs}
        queries = {ref['query_id'] for ref in refs}
        return {'source_id':next(iter(ids)) if len(ids) == 1 else 'unknown',
                'query_id':next(iter(queries)) if len(queries) == 1 else 'unknown',
                'source_query_refs':[dict(ref) for ref in refs]}

    observed_text = {}
    for page in pages:
        observed_text[page.get('final_url') or page.get('url')] = _decoded(unquote(str(page.get('html') or '') + ' ' + str(page.get('text') or '')))
    for observation in [candidate, *candidate.get('sources', [])]:
        if isinstance(observation, dict) and observation.get('source_excerpt'):
            observed_text.setdefault(observation.get('source_url'), str(observation['source_excerpt']))

    def excerpt_for(field, value, url):
        text = observed_text.get(url, '')
        if field in {'email', 'phone'} and not literal_value(field, value, text, country or phone_region):
            return None
        if not text:
            return None
        # Retain a small literal slice around the channel, including raw phone
        # formatting rather than manufacturing a normalized-number excerpt.
        index = text.casefold().find(str(value).casefold())
        if field == 'email':
            return text[index:index + len(str(value))] if index >= 0 else None
        if field == 'phone':
            try:
                match = next((m for m in phonenumbers.PhoneNumberMatcher(text, country or phone_region,
                    leniency=phonenumbers.Leniency.POSSIBLE) if literal_value('phone', value, m.raw_string, country or phone_region)), None)
                return match.raw_string if match else None
            except (ValueError, phonenumbers.NumberParseException):
                return None
        index = max(0, index)
        return text[max(0, index - 100):index + max(200, len(str(value)) + 100)]

    def provenance(entity_id, field, value, url, kind, retrieved_at, method="extracted", confidence=None, excerpt=None):
        if value is None or value == "" or value == []:
            return
        key = (entity_id, field, json.dumps(value, sort_keys=True, ensure_ascii=False), url, method)
        if key in source_keys:
            return
        source_keys.add(key)
        sources.append({"entity_id": entity_id, "field": field, "value": value, "source_type": kind,
                        "source_url": url or source_url, "retrieved_at": _stamp(retrieved_at), "method": method,
                         "source_excerpt": excerpt or excerpt_for(field, value, url or source_url),
                        "confidence": round(confidence if confidence is not None else SOURCE_WEIGHT.get(kind, .65), 3),
                        **lineage_for(url or source_url)})

    for evidence in binding['evidence']:
        provenance(company_id, 'website_url', website, evidence['source_url'],
                   'official_site' if normalize_domain(evidence['source_url']) == domain else 'directory',
                   evidence['retrieved_at'], evidence['method'], excerpt=evidence['source_excerpt'])

    def set_company(field, value, url, kind, at, method="extracted"):
        if value is None or value == "":
            return
        if field in {"description", "contact_form_url"} and company.get(field):
            # JSON-LD, OpenGraph and page descriptions are complementary copy.
            # Multiple actually observed forms are alternative public channels.
            # Keep the first preferred value and preserve each observation.
            provenance(company_id, field, value, url, kind, at, method)
            return
        prior = company.get(field)
        equivalent = lambda item: re.findall(r'\w+', str(item).casefold())
        same = prior not in (None, "") and str(prior).casefold() == str(value).casefold()
        if field in {"address", "city", "region"} and prior:
            def location_tokens(item):
                text = str(item)
                if field == "address" and company.get("country_code") == "US":
                    text = re.sub(r',\s*(?:USA|United States(?: of America)?)\s*$', '', text, flags=re.I)
                    text = re.sub(r'\b(\d{5})-\d{4}\b', r'\1', text)
                    # A known state may be omitted by a structured address whose
                    # street, locality and ZIP are otherwise identical.
                    import pycountry
                    region = str(company.get('region') or '').strip()
                    state = pycountry.subdivisions.get(code='US-' + region.upper()) or next(
                        (state for state in pycountry.subdivisions.get(country_code='US')
                         if state.name.casefold() == region.casefold()), None)
                    if state:
                        aliases = '|'.join(re.escape(value) for value in (state.code[3:], state.name))
                        text = re.sub(r'\b(?:' + aliases + r')\b(?=\W*\d{5}\b)', '', text, flags=re.I)
                    aliases = {'north': 'n', 'south': 's', 'east': 'e', 'west': 'w',
                        'northeast': 'ne', 'northwest': 'nw', 'southeast': 'se', 'southwest': 'sw',
                        'street': 'st', 'avenue': 'ave', 'boulevard': 'blvd', 'road': 'rd',
                        'drive': 'dr', 'lane': 'ln', 'court': 'ct', 'parkway': 'pkwy',
                        'highway': 'hwy', 'suite': 'ste'}
                    return [aliases.get(token, token) for token in equivalent(text)]
                return equivalent(text)
            same = location_tokens(prior) == location_tokens(value)
        if field == "region" and prior and company.get("country_code") == "US":
            import pycountry
            def state_code(item):
                text = str(item).strip()
                found = pycountry.subdivisions.get(code='US-' + text.upper())
                if found:
                    return found.code
                return next((state.code for state in pycountry.subdivisions.get(country_code='US')
                             if state.name.casefold() == text.casefold()), text.casefold())
            same = state_code(prior) == state_code(value)
        if same:
            provenance(company_id, field, value, url, kind, at, method)
            return
        if company.get(field) not in (None, "") and str(company[field]).casefold() != str(value).casefold():
            conflict = {"field": field, "values": [company[field], value], "source_url": url}
            if conflict not in company["conflicts"]:
                company["conflicts"].append(conflict)
        else:
            company[field] = value
        provenance(company_id, field, value, url, kind, at, method)

    def observe_form(form_url, kind, at, fields):
        set_company("contact_form_url", form_url, form_url, kind, at, "observed_contact_form")
        for source in sources:
            if (source.get("entity_id") == company_id and source.get("field") == "contact_form_url"
                    and source.get("value") == form_url and source.get("source_url") == form_url
                    and source.get("method") == "observed_contact_form"):
                source["form_fields"] = list(fields)

    def public_link(value, base):
        from app.web.security import normalize_url
        try:
            return normalize_url(urljoin(base, str(value)), fixture=company["demo_only"])
        except Exception:
            return None

    def add_social(value, url, kind, at, method):
        normalized = public_link(value, url)
        if not normalized:
            return
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "").lower()
        if not any(host == allowed or host.endswith("." + allowed) for allowed in SOCIAL_DOMAINS):
            return
        if not parsed.path.strip("/") or any(part in parsed.path.lower() for part in ("/share", "/sharer", "/intent", "/login", "/signup")):
            return
        if normalized not in company["social_urls"]:
            company["social_urls"].append(normalized)
        provenance(company_id, "social_urls", [normalized], url, kind, at, method)

    def published_name(value, url, kind, at, method):
        nonlocal company_name_is_hint
        if value and company_name_is_hint:
            company["company_name"] = str(value)
            sources[:] = [source for source in sources if not(source["entity_id"] == company_id and source["field"] == "company_name")]
            provenance(company_id, "company_name", str(value), url, kind, at, method)
            company_name_is_hint = False

    def public_values(value):
        result = []
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                item = item.get("name") or item.get("identifier") or item.get("value")
            if item is not None and str(item).strip():
                result.append(str(item).strip())
        return list(dict.fromkeys(result))

    def candidate_observations(field, value):
        def equivalent(observed):
            if field == "email":
                clean = lambda item: unquote(str(item)).lower().removeprefix("mailto:").split("?", 1)[0].strip()
                return clean(observed) == clean(value)
            if field == "phone":
                left, right = validate_phone(observed, country or phone_region), validate_phone(value, country or phone_region)
                return left["phone"] == right["phone"] if left["phone"] and right["phone"] else str(observed).strip() == str(value).strip()
            return str(observed).strip().casefold() == str(value).strip().casefold()
        return [observation for observation in candidate.get("sources", []) if isinstance(observation, dict)
                and observation.get(field) is not None and any(equivalent(observed) for observed in _strings(observation[field]))]

    for field in ("company_id", "company_name", "domain", "website_url", "country_code", "region", "city", "address", "industry_text", "company_type", "employee_count", "branch_id", "branch_name", "location_id", "revenue", "linkedin_url", "industry_codes", "description", "brands", "certifications", "products", "knows_about"):
        observations = candidate_observations(field, company.get(field))
        if observations:
            for observation in observations:
                provenance(company_id, field, company.get(field), observation.get("source_url") or source_url,
                           observation.get("source_type") or source_type, observation.get("retrieved_at") or collected, "discovered")
        else:
            provenance(company_id, field, company.get(field), source_url, source_type, collected, "discovered")
    for discovery in candidate.get("sources", []):
        if not isinstance(discovery, dict):
            continue
        discovery_url = discovery.get("source_url") or discovery.get("url")
        if not discovery_url:
            continue
        discovery_type = discovery.get("source_type") or source_type
        discovery_time = discovery.get("retrieved_at") or discovery.get("fetched_at") or collected
        if discovery.get("field") and "value" in discovery:
            provenance(company_id, discovery["field"], discovery["value"], discovery_url, discovery_type,
                       discovery_time, discovery.get("method") or "discovered_candidate", excerpt=discovery.get('source_excerpt'))
            if subject_excerpt(company['company_name'], discovery.get('source_excerpt')):
                provenance(company_id, 'company_name', company['company_name'], discovery_url, discovery_type,
                           discovery_time, 'observed_page_subject', excerpt=discovery.get('source_excerpt'))
        else:
            observed_name = discovery.get('company_name') or company['company_name']
            if subject_excerpt(observed_name, discovery.get('source_excerpt')):
                provenance(company_id, "company_name", observed_name,
                           discovery_url, discovery_type, discovery_time, "discovered_candidate", excerpt=discovery.get('source_excerpt'))
            for field in ("domain", "website_url", "country_code", "region", "city", "address", "industry_text", "company_type", "employee_count"):
                if discovery.get(field) is not None:
                    provenance(company_id, field, discovery[field], discovery_url, discovery_type, discovery_time, "discovered_candidate")

    def add_contact(email=None, phone=None, *, name=None, title=None, url=None, kind="official_site", at=None,
                    inferred=False, method="extracted", explicit=False, excerpt=None):
        email_data = validate_email(email, mx_lookup=mx_lookup) if email else {"email": None, "email_status": "unknown", "syntax_status": "not_checked", "mx_status": "not_checked",
            "catch_all_status": "not_checked", "smtp_status": "not_performed", "disposable_status": "unknown", "is_role_account": False}
        phone_data = validate_phone(phone, company.get("country_code") or phone_region) if phone else {"phone": None, "phone_raw": None, "phone_extension": None, "phone_status": "unknown", "line_type": "unknown"}
        # A name must come from an explicit Person/card; a page-wide name is never attached.
        name = str(name).strip() if name and explicit else None
        title = str(title).strip() if title and name else None
        if not email and not phone and not name:
            return None
        normalized_email = email_data["email"]
        key = normalized_email or phone_data["phone"] or (phone_data["phone_raw"] if phone else None)
        existing = next((c for c in contacts if (normalized_email and c.get("email") == normalized_email)
                         or (not normalized_email and key and key in (c.get("phone"), c.get("phone_raw")))
                         or (not key and name and c.get("contact_name") == name and c.get("title") == title)), None)
        at = _stamp(at or collected)
        authority = SOURCE_WEIGHT.get("inferred" if inferred else kind, .65)
        days = max(0, (current - _time(at)).days)
        freshness_factor = 1 if days <= 30 else .9 if days <= 90 else .75 if days <= 180 else .5
        validation_bonus = .05 if email_data["mx_status"] == "valid" else 0
        score = min(1, authority * freshness_factor + validation_bonus)
        tags = ["public_business"]
        if normalized_email:
            tags.append(email_data["email_status"])
            if inferred:
                tags.append("inferred")
        if name or (normalized_email and normalized_email.rsplit("@", 1)[1] in PERSONAL_DOMAINS):
            tags.append("personal_business")
        if days > freshness:
            tags.append("stale")
        item = {"contact_id": _id("ct", company_id, key or name, "" if key else title), "company_id": company_id,
                "contact_name": name, "title": title, **email_data, **phone_data,
                "email_type": _email_type(normalized_email, bool(name), inferred) if normalized_email and "@" in normalized_email else None,
                "phone_type": ("mobile_business" if name and phone_data["line_type"] == "mobile" else "direct_business" if name else "company_main") if phone else None,
                "source_urls": [url or source_url], "retrieved_at": at, "last_verified_at": _stamp(current) if email or phone else None,
                "confidence": round(score, 3), "tags": tags, "inferred": bool(inferred), "opt_out": False,
                "confidence_components": {"source_authority": authority, "freshness_factor": freshness_factor,
                    "validation_bonus": validation_bonus, "cross_confirmation_bonus": 0},
                "binding_status": "inferred" if inferred else "explicit" if name and (email or phone) else "unbound" if name else "company"}
        item.update(lineage_for(url or source_url))
        observed_item = dict(item)
        if existing:
            item = existing
            refs = item.setdefault('source_query_refs', [])
            for ref in lineage_for(url or source_url)['source_query_refs']:
                if ref not in refs:
                    refs.append(ref)
            for field in ('source_id', 'query_id'):
                values = {ref[field] for ref in refs}
                item[field] = next(iter(values)) if len(values) == 1 else 'unknown'
            if url and url not in item["source_urls"]:
                item["source_urls"].append(url)
            components = item["confidence_components"]
            prior_base = components["source_authority"] * components["freshness_factor"] + components["validation_bonus"]
            if score > prior_base:
                components.update(source_authority=authority, freshness_factor=freshness_factor, validation_bonus=validation_bonus)
            origins = {urlsplit(str(reference)).hostname or reference for reference in item["source_urls"] if reference}
            components["cross_confirmation_bonus"] = min(.2, max(0, len(origins) - 1) * .1)
            item["confidence"] = round(min(1, max(score, prior_base) + components["cross_confirmation_bonus"]), 3)
            if name and not item["contact_name"]:
                item.update(contact_name=name, title=title, binding_status="inferred" if inferred else "explicit")
                item["email_type"] = _email_type(normalized_email, True, inferred) if normalized_email else None
                item["tags"] = list(dict.fromkeys(item["tags"] + tags))
            elif name and item.get("contact_name") != name:
                item.setdefault("associated_people", []).append({"contact_name": name, "title": title, "source_url": url})
                item["tags"] = list(dict.fromkeys(item.get("tags", []) + ["binding_conflict"]))
                company["conflicts"].append({"field": "contact_binding", "values": [item.get("contact_name"), name], "source_url": url})
            if phone and not item.get("phone_raw"):
                item.update(phone_data)
                item["phone_type"] = "direct_business" if name else "company_main"
        else:
            contacts.append(item)
        method = "pattern_inferred" if inferred else method + ("+bound" if name and (email or phone) else "")
        observed_fields = {"email", "email_type", "email_status", "syntax_status", "mx_status", "catch_all_status", "smtp_status", "disposable_status", "is_role_account"} if email else set()
        if phone:
            observed_fields.update({"phone", "phone_raw", "phone_extension", "phone_type", "phone_status", "line_type"})
        if name:
            observed_fields.update({"contact_name", "title"})
        factual_fields = {"email", "email_type", "email_status", "syntax_status", "mx_status", "catch_all_status", "smtp_status", "disposable_status", "is_role_account",
                          "phone", "phone_raw", "phone_extension", "phone_type", "phone_status", "line_type", "contact_name", "title"}
        for field, combined_value in item.items():
            if field not in {"source_urls", "associated_people"}:
                if field in factual_fields and field not in observed_fields:
                    continue
                value = observed_item.get(field) if field in factual_fields else combined_value
                provenance(item["contact_id"], field, value, url or source_url,
                           "inferred" if inferred else kind, at, method if field in {"email", "phone", "phone_raw", "contact_name", "title"} else "derived_" + method, score, excerpt=excerpt)
        return item

    for email in _strings(candidate.get("email") or candidate.get("main_email")):
        observations = imported_channel_sources(candidate, 'email', email, now=current)
        for observation in observations:
            add_contact(email=email, url=observation.get("source_url") or source_url,
                        kind=observation.get("source_type") or source_type, at=observation.get("retrieved_at") or collected,
                        method='agent_page_observation', excerpt=observation['source_excerpt'])
    for phone in _strings(candidate.get("phone") or candidate.get("main_phone")):
        observations = imported_channel_sources(candidate, 'phone', phone, now=current)
        for observation in observations:
            add_contact(phone=phone, url=observation.get("source_url") or source_url,
                        kind=observation.get("source_type") or source_type, at=observation.get("retrieved_at") or collected,
                        method='agent_page_observation', excerpt=observation['source_excerpt'])

    # A bare /contact URL is only a page hint. Research imports must record the
    # form fields actually observed at that URL before it becomes a channel.
    form_url = public_link(candidate.get("contact_form_url"), source_url) if candidate.get("contact_form_url") else None
    for observation in candidate.get("sources", []):
        if not isinstance(observation, dict) or not form_url:
            continue
        observed_url = public_link(observation.get("source_url"), source_url) if observation.get("source_url") else None
        observed_value = public_link(observation.get("value"), source_url) if observation.get("value") else None
        try:
            observed_at = datetime.fromisoformat(str(observation.get("retrieved_at") or "").replace("Z", "+00:00"))
            valid_time = observed_at.tzinfo is not None and observed_at <= current
        except (ValueError, TypeError):
            valid_time = False
        if (observation.get("field") == "contact_form_url" and observed_value == form_url == observed_url
                and observation.get("method") == "observed_contact_form" and valid_time
                and observation.get("source_type") != "inferred"
                and is_contact_form_fields(observation.get("form_fields"))):
            observe_form(form_url, observation.get("source_type") or source_type,
                         observed_at, observation["form_fields"])

    for page in pages:
        if page.get("fetch_status") and page["fetch_status"] not in {"ok", "success", "completed", "fetched", 200, "200"}:
            continue
        url = page.get("final_url") or page.get("url") or website
        kind = page.get("source_type") or "official_site"
        at = page.get("fetched_at") or collected
        soup = parsed_html(page.get("html") or "")
        nodes = []
        for script in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
            try:
                nodes.extend(_walk_json(json.loads(script.string or script.get_text())))
            except (ValueError, TypeError):
                pass
        for node in nodes:
            types = _types(node.get("@type"))
            if "person" in types and node.get("name"):
                name, title = node["name"], node.get("jobTitle") or node.get("roleName")
                email_values = [v for raw in _strings(node.get("email")) for v in _emails(raw)]
                phones = _strings(node.get("telephone"))
                if not email_values and not phones:
                    add_contact(name=name, title=title, url=url, kind=kind, at=at, explicit=True, method="json_ld")
                for index, email in enumerate(email_values):
                    add_contact(email=email, phone=phones[index] if index < len(phones) else None, name=name, title=title,
                                url=url, kind=kind, at=at, explicit=True, method="json_ld")
                for phone in phones[len(email_values):]:
                    add_contact(phone=phone, name=name, title=title, url=url, kind=kind, at=at, explicit=True, method="json_ld")
            elif types & {"organization", "corporation", "localbusiness", "professionalservice"}:
                if node.get("url") and domain and normalize_domain(node["url"]) != domain:
                    continue
                for social in _strings(node.get("sameAs")):
                    add_social(social, url, kind, at, "json_ld_same_as")
                published_name(node.get("name"), url, kind, at, "json_ld")
                set_company("description", node.get("description"), url, kind, at, "json_ld")
                industry = public_values(node.get("industry"))
                if industry:
                    set_company("industry_text", "; ".join(industry), url, kind, at, "json_ld_explicit_industry")
                codes = [f"{label}:{code}" for key, label in (("naics", "NAICS"), ("isicV4", "ISIC"), ("isic", "ISIC"), ("sic", "SIC"))
                         for code in public_values(node.get(key))]
                if codes:
                    set_company("industry_codes", list(dict.fromkeys(codes)), url, kind, at, "json_ld")
                for field, values in (("brands", public_values(node.get("brand"))),
                                      ("certifications", public_values(node.get("hasCertification") or node.get("certifications"))),
                                      ("products", public_values(node.get("products"))),
                                      ("knows_about", public_values(node.get("knowsAbout")))):
                    if values:
                        set_company(field, values, url, kind, at, "json_ld_explicit")
                from app.rules.qualification import quantitative_value
                revenue = quantitative_value(node.get("annualRevenue") or node.get("revenue"))
                set_company("revenue", revenue, url, kind, at, "json_ld")
                address = node.get("address")
                if isinstance(address, dict):
                    for field, key in (("region", "addressRegion"), ("city", "addressLocality"), ("country_code", "addressCountry")):
                        value = address.get(key)
                        if isinstance(value, dict):
                            value = value.get("name")
                        if field == "country_code" and value:
                            import pycountry
                            try:
                                value = pycountry.countries.lookup(str(value)).alpha_2
                            except LookupError:
                                value = None
                        set_company(field, value, url, kind, at, "json_ld")
                    set_company("address", ", ".join(str(address[k]) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode") if address.get(k)), url, kind, at, "json_ld")
                elif isinstance(address, list):
                    # Organization.address may be an array of operating offices.
                    # Its Python representation is never a scalar street address
                    # and does not assert that each office is headquarters.
                    locations = company.setdefault('office_addresses', [])
                    for location in address:
                        if isinstance(location, (dict, str)) and location not in locations:
                            locations.append(location)
                    provenance(company_id, 'office_addresses', address, url, kind, at, 'json_ld_locations')
                elif address:
                    set_company("address", str(address), url, kind, at, "json_ld")
                count = node.get("numberOfEmployees")
                count = quantitative_value(count)
                set_company("employee_count", count, url, kind, at, "json_ld")
                for email in _strings(node.get("email")):
                    for value in _emails(email):
                        add_contact(email=value, url=url, kind=kind, at=at, method="json_ld")
                for phone in _strings(node.get("telephone")):
                    add_contact(phone=phone, url=url, kind=kind, at=at, method="json_ld")
            elif "contactpoint" in types or (node.get("contactType") and (node.get("email") or node.get("telephone"))):
                for email in _strings(node.get("email")):
                    for value in _emails(email):
                        add_contact(email=value, url=url, kind=kind, at=at, method="json_ld_contact_point")
                for phone in _strings(node.get("telephone")):
                    add_contact(phone=phone, url=url, kind=kind, at=at, method="json_ld_contact_point")
        for card in soup.select('[itemtype*="Person"], .team-member, .staff-member, .contact-person, [data-contact-name]'):
            name_node = card.select_one('[itemprop="name"], .name, h2, h3, h4')
            title_node = card.select_one('[itemprop="jobTitle"], .position, .role, .job-title, .title')
            name = card.get("data-contact-name") or (name_node.get_text(" ", strip=True) if name_node else None)
            title = title_node.get_text(" ", strip=True) if title_node else None
            if not name or len(name) > 100:
                continue
            emails = _emails(card.get_text(" ", strip=True) + " " + " ".join(str(a.get("href") or "") for a in card.find_all("a")))
            phones = [a.get("href")[4:] for a in card.select('a[href^="tel:"]')]
            if not emails and not phones:
                add_contact(name=name, title=title, url=url, kind=kind, at=at, explicit=True)
            for index, email in enumerate(emails):
                add_contact(email=email, phone=phones[index] if index < len(phones) else None, name=name, title=title, url=url, kind=kind, at=at, explicit=True)
            for phone in phones[len(emails):]:
                add_contact(phone=phone, name=name, title=title, url=url, kind=kind, at=at, explicit=True)
        for link in soup.select('a[href^="mailto:"]'):
            for email in _emails(unquote(link.get("href"))):
                add_contact(email=email, url=url, kind=kind, at=at, method="mailto")
        for link in soup.select('a[href^="tel:"]'):
            add_contact(phone=link.get("href")[4:], url=url, kind=kind, at=at, method="tel_link")
        for link in soup.select('a[href]'):
            add_social(link.get("href"), url, kind, at, "official_public_link")
        for form in soup.find_all("form"):
            fields = [" ".join(str(node.get(key) or "") for key in ("name", "type", "id")) + " " + node.name
                      for node in form.find_all(["input", "textarea", "select"])
                      if node.get("type", "").lower() not in {"hidden", "submit", "button"}]
            if is_contact_form_fields(fields):
                form_page = public_link(url, url)
                if form_page:
                    observe_form(form_page, kind, at, fields)
        for meta in soup.select('meta[property="og:site_name"]'):
            published_name(meta.get("content"), url, kind, at, "meta_og_site_name")
        for meta in soup.select('meta[name="description"], meta[property="og:description"]'):
            if meta.get("content"):
                set_company("description", meta.get("content"), url, kind, at, "meta_description")
        text_view = parsed_html(page.get("html") or "", remove=('script', 'style', 'noscript'))
        text = text_view.get_text("\n", strip=True) + "\n" + str(page.get("text") or "")
        for email in _emails(text):
            add_contact(email=email, url=url, kind=kind, at=at, method="text_or_obfuscation")
        # phonenumbers uses regional metadata and avoids arbitrary digit-only text guesses.
        try:
            for match in phonenumbers.PhoneNumberMatcher(text, company.get("country_code") or phone_region):
                add_contact(phone=match.raw_string, url=url, kind=kind, at=at, method="text_phone")
        except (ValueError, phonenumbers.NumberParseException):
            pass
        for meta in soup.select('meta[itemprop="email"], meta[name="email"]'):
            for email in _emails(meta.get("content")):
                add_contact(email=email, url=url, kind=kind, at=at, method="meta")

    usable = [c for c in contacts if (c.get("email") and c["email_status"] != "invalid") or (c.get("phone") and c["phone_status"] != "invalid")]
    for field, predicate in (("no_email", lambda c: c.get("email") and c["email_status"] != "invalid"),
                             ("no_phone", lambda c: c.get("phone") and c["phone_status"] != "invalid"),
                             ("no_named_contact", lambda c: c.get("contact_name"))):
        if not any(predicate(c) for c in contacts):
            company["missing"].append(field)
    if not website:
        company["missing"].append("no_website")
    if not country:
        company["missing"].append("no_country")
    company["main_email"] = next((c["email"] for c in usable if c.get("email") and not c.get("contact_name") and not c["inferred"]), None)
    company["main_phone"] = next((c["phone"] for c in usable if c.get("phone") and not c.get("contact_name")), None)
    public_main_phones = list(dict.fromkeys(c["phone"] for c in usable if c.get("phone") and not c.get("contact_name")))
    if len(public_main_phones) > 1:
        company["conflicts"].append({"field": "main_phone", "values": public_main_phones, "reason": "multiple_public_values"})
    active_entities = {company_id, *(c["contact_id"] for c in contacts)}
    sources[:] = [s for s in sources if s["entity_id"] in active_entities]
    company["status"] = "insufficient" if not usable or not country else "needs_review" if company["conflicts"] else "qualified"
    company["source_count"] = len({s["source_url"] for s in sources if s.get("source_url")})
    company["confidence"] = round(sum(c["confidence"] for c in usable) / len(usable), 3) if usable else .3
    company["last_verified_at"] = _stamp(current)
    for field in ("main_email", "main_phone", "status", "source_count", "confidence", "missing", "conflicts", "last_verified_at"):
        provenance(company_id, field, company.get(field), source_url, source_type, collected, "derived_aggregate")
    return company
