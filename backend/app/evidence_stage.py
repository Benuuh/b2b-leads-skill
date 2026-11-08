"""Experimental, offline evidence-boundary validator; not a research entrypoint.

Inputs are supplied observations, never inferred facts. This prototype intentionally
does not submit candidates or integrate with the production retention lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = 'evidence-stage-prototype-0.1'
MAX_PAGE_BYTES = 2 * 1024 * 1024


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def literal_value_in_excerpt(value: str, excerpt: str) -> bool:
    """Reject values embedded in larger identifiers; this is not semantic proof."""
    # Dots and hyphens are ordinary punctuation after prose, but can extend an
    # email or URL into a different address and must remain part of its boundary.
    boundary = r'[\w@.+-]' if '@' in value or '://' in value else r'[\w]'
    pattern = rf'(?<!{boundary})' + re.escape(value.casefold()) + rf'(?!{boundary})'
    return re.search(pattern, excerpt.casefold()) is not None


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Source(Strict):
    source_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    url: str = Field(min_length=1, max_length=2048)
    source_type: Literal['directory', 'official_site', 'search_snippet', 'other']
    collected_at: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def verify(self):
        from urllib.parse import urlsplit
        parsed = urlsplit(self.url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('source URL must be an HTTP(S) URL without credentials')
        try:
            timestamp = datetime.fromisoformat(self.collected_at.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('invalid collection time') from exc
        if timestamp.utcoffset() is None:
            raise ValueError('collection time must include a timezone')
        raw = self.text.encode('utf-8')
        if len(raw) > MAX_PAGE_BYTES or digest(raw) != self.sha256:
            raise ValueError('source text exceeds limit or does not match hash')
        return self


class Entry(Strict):
    candidate_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    source_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    subject: str = Field(min_length=1, max_length=200)
    # Python Unicode character offsets into the complete captured source text.
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class Observation(Strict):
    candidate_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    source_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    subject: str = Field(min_length=1, max_length=200)
    field: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=500)
    method: str = Field(min_length=1, max_length=120)
    excerpt: str = Field(min_length=1, max_length=5000)
    kind: Literal['evidence', 'lead']


class Artifact(Strict):
    version: Literal['evidence-stage-prototype-0.1']
    run_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,80}$')
    sources: list[Source] = Field(min_length=1, max_length=100)
    entries: list[Entry] = Field(max_length=500)
    observations: list[Observation] = Field(max_length=2000)

    @model_validator(mode='after')
    def total_size(self):
        if len(canonical(self.model_dump())) > 6 * 1024 * 1024:
            raise ValueError('artifact exceeds 6 MiB')
        return self


def inspect(payload: dict) -> dict:
    """Return individual errors; evidence is not a qualification decision."""
    artifact = Artifact.model_validate(payload)
    sources = {s.source_id: s for s in artifact.sources}
    if len(sources) != len(artifact.sources):
        raise ValueError('duplicate source_id')
    entries = {}
    subjects = {}
    for entry in artifact.entries:
        key = (entry.candidate_id, entry.source_id)
        source = sources.get(entry.source_id)
        if key in entries or source is None or entry.end <= entry.start or entry.end > len(source.text):
            raise ValueError('invalid or duplicate entry boundary')
        if entry.subject not in source.text[entry.start:entry.end]:
            raise ValueError('entry does not contain its subject')
        if entry.candidate_id in subjects and subjects[entry.candidate_id] != entry.subject:
            raise ValueError('candidate subject changed across sources')
        subjects[entry.candidate_id] = entry.subject
        for other in entries.values():
            if (other.source_id == entry.source_id and other.candidate_id != entry.candidate_id
                    and max(other.start, entry.start) < min(other.end, entry.end)):
                raise ValueError('different candidates have overlapping source boundaries')
        entries[key] = entry

    rows = []
    for index, obs in enumerate(artifact.observations):
        source = sources.get(obs.source_id)
        entry = entries.get((obs.candidate_id, obs.source_id))
        reason = None
        if source is None or entry is None or obs.subject != entry.subject:
            reason = 'ENTRY_UNVERIFIED'
        elif obs.excerpt not in source.text[entry.start:entry.end] or obs.subject not in obs.excerpt:
            reason = 'EXCERPT_UNVERIFIED'
        elif any(other.candidate_id != obs.candidate_id and other.source_id == obs.source_id
                 and other.subject.casefold() != obs.subject.casefold()
                 and literal_value_in_excerpt(other.subject, obs.excerpt) for other in artifact.entries):
            # Mentioning a second known entity in the same excerpt requires
            # human attribution, even when the entry spans do not overlap.
            reason = 'MULTIPLE_SUBJECTS'
        elif obs.kind == 'evidence' and source.source_type == 'search_snippet':
            reason = 'SEARCH_SNIPPET_ONLY'
        elif obs.kind == 'evidence' and ('inferred' in obs.method.casefold()
                                          or obs.method.casefold() in {'search', 'search_result', 'search_summary'}):
            reason = 'UNOBSERVED_METHOD'
        # This literal check is deliberately conservative: normalized country codes,
        # synonyms and translated claims require human review, never guessed here.
        elif obs.kind == 'evidence' and not literal_value_in_excerpt(obs.value, obs.excerpt):
            reason = 'VALUE_NOT_LITERAL'
        rows.append({'index': index, 'candidate_id': obs.candidate_id, 'source_id': obs.source_id,
                     'status': 'needs_review' if reason else ('discovered' if obs.kind == 'lead' else 'observed'),
                     'reason_code': reason})
    # Only fields known to be single-valued are compared here; this is a
    # conservative warning, not a substitute for the core conflict policy.
    single_fields = {'company_name', 'website_url', 'entity.headquarters_country',
                     'entity.registered_country', 'entity.relationship'}
    groups = {}
    for index, obs in enumerate(artifact.observations):
        if rows[index]['status'] == 'observed' and obs.field in single_fields:
            groups.setdefault((obs.candidate_id, obs.field), []).append(index)
    for indexes in groups.values():
        if len({artifact.observations[i].value.casefold() for i in indexes}) > 1:
            for i in indexes:
                rows[i]['status'], rows[i]['reason_code'] = 'needs_review', 'CONFLICTING_VALUES'
    return {'version': VERSION, 'run_id': artifact.run_id, 'input_sha256': digest(canonical(payload)),
            'records': rows, 'note': 'Boundary validation is not qualification, conversion, or admission.'}


def page(payload: dict, *, cursor: int = 0, expected_input_sha256: str | None = None,
         limit: int = 20, max_bytes: int = 16384) -> dict:
    """Bounded result page with a content-bound cursor; errors are never omitted."""
    if not 0 <= cursor or not 1 <= limit <= 100 or not 512 <= max_bytes <= 65536:
        raise ValueError('invalid page bounds')
    result = inspect(payload)
    if cursor and expected_input_sha256 != result['input_sha256']:
        raise ValueError('cursor belongs to a different artifact')
    artifact = Artifact.model_validate(payload)
    sources = {source.source_id: source for source in artifact.sources}
    records = []
    for row, obs in zip(result['records'], artifact.observations):
        source = sources.get(obs.source_id)
        records.append({**row, 'subject': obs.subject, 'field': obs.field, 'value': obs.value,
                        'source_url': source.url if source else None, 'kind': obs.kind,
                        'excerpt': obs.excerpt[:160], 'excerpt_truncated': len(obs.excerpt) > 160})
    if cursor > len(records):
        raise ValueError('cursor exceeds result count')
    items = []
    end = cursor
    while end < len(records) and len(items) < limit:
        trial = {'version': VERSION, 'input_sha256': result['input_sha256'],
                 'records': [*items, records[end]], 'next_cursor': end + 1}
        if len(canonical(trial)) > max_bytes:
            if not items:
                raise ValueError('one record exceeds the byte limit')
            break
        items.append(records[end])
        end += 1
    output = {'version': VERSION, 'input_sha256': result['input_sha256'],
              'records': items, 'next_cursor': end if end < len(records) else None}
    if len(canonical(output)) > max_bytes:
        raise ValueError('page envelope exceeds the byte limit')
    return output


def detail(payload: dict, *, candidate_id: str, source_id: str) -> dict:
    """Return the complete retained source and selected observations by ID."""
    result = inspect(payload)
    artifact = Artifact.model_validate(payload)
    entry = next((e for e in artifact.entries if e.candidate_id == candidate_id
                  and e.source_id == source_id), None)
    if entry is None:
        raise ValueError('candidate/source entry not found')
    source = next(s for s in artifact.sources if s.source_id == source_id)
    indexes = [i for i, obs in enumerate(artifact.observations)
               if obs.candidate_id == candidate_id and obs.source_id == source_id]
    return {'version': VERSION, 'input_sha256': result['input_sha256'],
            'source': source.model_dump(), 'entry': entry.model_dump(),
            'observations': [artifact.observations[i].model_dump() for i in indexes],
            'records': [result['records'][i] for i in indexes]}


# Restrict conversion to known fields. Values are copied, never guessed or
# normalized; a source must literally contain the supplied value and subject.
FIELD_MAP = {
    'company_name': ('company_name', False),
    'website_url': ('website_url', False),
    'entity.country_code': ('country_code', False),
    'entity.headquarters_country': ('headquarters_country', False),
    'entity.registered_country': ('registered_country', False),
    'entity.relationship': ('entity_relationship', False),
    'business.models': ('business_models', True),
    'offering.products': ('offering_products', True),
    'offering.services': ('offering_services', True),
    'customer.industries': ('customer_industries', True),
    'email': ('email', True),
    'phone': ('phone', True),
}


def draft(payload: dict, customer_spec: dict, candidate_ids: list[str], *, as_of: str) -> dict:
    """Build and precheck an isolated candidate subset without writes or network.

    The caller must still review evidence. This is a development prototype,
    not an admission operation; the production entrypoint is intentionally not
    changed. A candidate with any questionable observation is withheld whole.
    """
    from app.runs import validate_input
    from app.schemas import parse_segment
    from app.rules.evidence import website_identity

    if not candidate_ids or len(candidate_ids) > 20 or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError('candidate_ids must contain 1 to 20 distinct IDs')
    timestamp = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
    if timestamp.utcoffset() is None:
        raise ValueError('as_of must include a timezone')
    normalized_spec, spec_version = parse_segment(customer_spec)
    if spec_version != 2 or normalized_spec['evidence_policy']['evidence_version'] != 'evidence-2':
        raise ValueError('prototype supports only evidence-2 runs')
    freshness = normalized_spec['evidence_policy']['freshness_days']
    inspection = inspect(payload)
    artifact = Artifact.model_validate(payload)
    sources = {s.source_id: s for s in artifact.sources}
    subject_ids = {}
    for entry in artifact.entries:
        if entry.candidate_id in candidate_ids:
            subject_ids.setdefault(entry.subject.casefold(), set()).add(entry.candidate_id)
    ambiguous_ids = set().union(*(ids for ids in subject_ids.values() if len(ids) > 1))
    candidates, indexes, errors = [], [], []
    for input_index, candidate_id in enumerate(candidate_ids):
        if candidate_id in ambiguous_ids:
            errors.append({'index': input_index, 'candidate_id': candidate_id,
                           'reason_code': 'DUPLICATE_SUBJECT_REVIEW', 'path': f'candidates[{input_index}]'})
            continue
        indices = [i for i, o in enumerate(artifact.observations) if o.candidate_id == candidate_id]
        evidence = [i for i in indices if artifact.observations[i].kind == 'evidence']
        # A website that only exists as a discovery lead has no ownership evidence.
        # Withhold it instead of silently dropping the site from the draft.
        lead_site = any(artifact.observations[i].field == 'website_url'
                        and artifact.observations[i].kind == 'lead' for i in indices)
        problem = next((inspection['records'][i] for i in evidence
                        if inspection['records'][i]['status'] != 'observed'), None)
        unsupported = next((artifact.observations[i].field for i in evidence
                            if artifact.observations[i].field not in FIELD_MAP), None)
        identities = [i for i in evidence if artifact.observations[i].field == 'company_name']
        stale = any(timestamp - datetime.fromisoformat(sources[artifact.observations[i].source_id]
                    .collected_at.replace('Z', '+00:00')) > timedelta(days=freshness)
                    or datetime.fromisoformat(sources[artifact.observations[i].source_id]
                    .collected_at.replace('Z', '+00:00')) > timestamp + timedelta(minutes=5)
                    for i in evidence if artifact.observations[i].source_id in sources)
        if lead_site:
            errors.append({'index': input_index, 'candidate_id': candidate_id,
                           'reason_code': 'WEBSITE_UNCONFIRMED',
                           'path': f'candidates[{input_index}].website_url'})
            continue
        if problem or unsupported or not identities or stale:
            errors.append({'index': input_index, 'candidate_id': candidate_id,
                           'reason_code': problem['reason_code'] if problem else
                           ('UNSUPPORTED_FIELD' if unsupported else 'MISSING_IDENTITY' if not identities
                            else 'EVIDENCE_OUT_OF_DATE'),
                           'path': f'candidates[{input_index}]'})
            continue
        name_observation = artifact.observations[identities[0]]
        # Name is exactly the selected subject; mentions of another name are
        # not sufficient evidence to substitute the candidate's identity.
        if name_observation.value != name_observation.subject:
            errors.append({'index': input_index, 'candidate_id': candidate_id,
                           'reason_code': 'IDENTITY_MISMATCH', 'path': f'candidates[{input_index}]'})
            continue
        origin = sources[name_observation.source_id]
        item = {'company_name': name_observation.subject, 'source_url': origin.url,
                'source_type': origin.source_type, 'retrieved_at': origin.collected_at,
                'source_excerpt': name_observation.excerpt, 'sources': []}
        for i in evidence:
            obs = artifact.observations[i]
            source = sources[obs.source_id]
            target, multiple = FIELD_MAP[obs.field]
            if multiple:
                values = item.setdefault(target, [])
                if obs.value not in values:
                    values.append(obs.value)
            elif target in item and item[target] != obs.value:
                # Country/website conflicts that survived policy-specific
                # checks are withheld rather than resolved by order.
                problem = {'reason_code': 'CONFLICTING_VALUES'}
                break
            else:
                item[target] = obs.value
            item['sources'].append({'field': obs.field, 'value': obs.value,
                'source_url': source.url, 'source_type': source.source_type,
                'retrieved_at': source.collected_at, 'method': obs.method,
                'source_excerpt': obs.excerpt})
        if problem:
            errors.append({'index': input_index, 'candidate_id': candidate_id,
                           'reason_code': problem['reason_code'], 'path': f'candidates[{input_index}]'})
            continue
        if item.get('website_url') and website_identity(item, now=timestamp)['status'] != 'confirmed':
            errors.append({'index': input_index, 'candidate_id': candidate_id,
                           'reason_code': 'WEBSITE_UNCONFIRMED',
                           'path': f'candidates[{input_index}].website_url'})
            continue
        candidates.append(item)
        indexes.append(input_index)
    checked = validate_input(customer_spec, {'candidates': candidates})
    accepted = []
    for result in checked['records']:
        original_index = indexes[result['index']]
        if result['admissible']:
            accepted.append(candidates[result['index']])
        else:
            path = result['path'].replace(f"candidates[{result['index']}]", f'candidates[{original_index}]', 1)
            errors.append({'index': original_index, 'candidate_id': candidate_ids[original_index],
                           'reason_code': result['reason_code'], 'path': path,
                           'message': result['message']})
    output = {'candidates': accepted}
    input_hash = digest(canonical({'artifact': payload, 'spec': customer_spec,
                                   'candidate_ids': candidate_ids, 'as_of': as_of}))
    return {'version': VERSION, 'input_sha256': input_hash, 'output_sha256': digest(canonical(output)),
            'chunk_id': 'stage-' + input_hash, 'submittable': output,
            'errors': sorted(errors, key=lambda item: item['index']),
            'note': 'Precheck is not qualification or admission; review and use the existing entrypoint.'}


def write_subset(result: dict, directory) -> dict:
    """Atomically publish a content-addressed subset in a caller-owned test directory.

    This intentionally does not integrate with production artifact retention.
    It is suitable only for synthetic fixtures until that lifecycle is wired in.
    """
    from pathlib import Path
    if result.get('version') != VERSION or not isinstance(result.get('submittable'), dict):
        raise ValueError('invalid draft result')
    payload = result['submittable']
    if set(payload) != {'candidates'} or not isinstance(payload['candidates'], list) or len(payload['candidates']) > 20:
        raise ValueError('invalid subset')
    body = canonical(payload)
    checksum = digest(body)
    if checksum != result.get('output_sha256'):
        raise ValueError('subset hash mismatch')
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('output directory is required')
    final = root / f'subset-{checksum}.json'
    # A same-directory hard link provides atomic create-if-absent, without
    # replacing an existing file under concurrent retries.
    descriptor, temporary = tempfile.mkstemp(prefix='.stage-subset-', suffix='.tmp', dir=root)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, final)
        except FileExistsError:
            if final.is_symlink() or not final.is_file() or digest(final.read_bytes()) != checksum:
                raise ValueError('existing subset differs or is not a regular file')
        return {'path': str(final), 'sha256': checksum, 'chunk_id': result.get('chunk_id')}
    finally:
        os.unlink(temporary)
