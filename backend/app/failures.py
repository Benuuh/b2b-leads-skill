"""Failure classification shared by job accounting, status and reports.

A source failure is never a qualification failure. Only an evidenced fact that
contradicts a necessary condition excludes an enterprise.
"""
from __future__ import annotations

from app.capabilities import FAILURE_CATEGORIES

INPUT_CODES = {
    'VALIDATION_ERROR', 'INVALID_INPUT', 'INPUT_TOO_LARGE', 'INVALID_KEY', 'INVALID_RUN_ID',
    'UNSUPPORTED_SPEC_VERSION', 'UNSUPPORTED_CONDITION', 'UNSUPPORTED_OPTION', 'CONFLICTING_CONFIG',
    'MISSING_EVIDENCE', 'MISSING_SOURCE', 'INVALID_URL', 'INVALID_COUNTRY', 'UNKNOWN_FIELD',
    'IMMUTABLE_IDENTITY', 'NO_NEW_EVIDENCE', 'INPUT_CLOSED', 'INVALID_STATE', 'PROCESS_BUSY',
    'VERSION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'UNSAFE_DATABASE',
    'SEGMENT_NOT_CONFIRMED', 'BRIEF_MAPPING_GAP',
}
CONFLICT_CODES = {'CONFLICTING_EVIDENCE', 'SUBJECT_CONFLICT'}
TRANSIENT_CODES = {'NETWORK_TIMEOUT', 'CONNECTION_ERROR', 'RATE_LIMITED', 'HTTP_ERROR', 'UNSUPPORTED_CONTENT'}
ACCESS_CODES = {'ACCESS_BLOCKED', 'ROBOTS_DISALLOWED', 'ROBOTS_UNKNOWN', 'LOGIN_REQUIRED',
                'OFFSITE_REDIRECT', 'REDIRECT_LIMIT', 'CONTENT_TOO_LARGE'}
CAPACITY_CODES = {'HOST_LIMIT_UNAVAILABLE', 'OBSERVATION_UNAVAILABLE', 'LOOKUP_OBSERVATION_UNAVAILABLE',
                  'TOOL_CAPACITY', 'DEADLINE_EXCEEDED'}
BUDGET_CODES = {'PAGE_BUDGET_EXCEEDED', 'INTERRUPTED_PAGE_BUDGET', 'BUDGET_EXHAUSTED',
                'CANDIDATE_LIMIT', 'TIME_LIMIT'}
EVIDENCE_CODES = {'SNAPSHOT_UNAVAILABLE', 'EVIDENCE_EXPIRED', 'ARTIFACT_MISSING', 'EXPORT_REVOKED',
                  'EXPORT_EMPTY', 'EXPORT_TOO_LARGE'}
CIRCUIT_CATEGORIES = {'transient_network', 'capacity', 'tool_failure'}


def category(code, retryable=False):
    code = str(code or '').upper()
    if code in INPUT_CODES:
        return 'input_invalid'
    if code in CONFLICT_CODES:
        return 'subject_conflict'
    if code in CAPACITY_CODES:
        return 'capacity'
    if code in BUDGET_CODES or code in {'PAGE_BUDGET_EXCEEDED'}:
        return 'budget_exhausted'
    if code in ACCESS_CODES:
        return 'access_denied'
    if code in TRANSIENT_CODES:
        return 'transient_network'
    if code in EVIDENCE_CODES:
        return 'evidence_unavailable'
    return 'transient_network' if retryable else 'tool_failure'


def describe(category_name):
    return FAILURE_CATEGORIES.get(category_name, FAILURE_CATEGORIES['tool_failure'])


def classify(code, retryable=False):
    name = category(code, retryable)
    return {'category': name, 'description': describe(name),
            'retryable': bool(retryable) and name in {'transient_network', 'capacity'}}


def is_circuit_failure(category_name):
    return category_name in CIRCUIT_CATEGORIES
