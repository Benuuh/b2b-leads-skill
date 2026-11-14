"""PostgreSQL integration tests always use a fresh isolated database."""
import secrets
from datetime import datetime, timezone

import psycopg
from psycopg import sql
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings


@pytest.fixture(scope="session")
def pg_database(tmp_path_factory):
    from app import persistence
    if not settings.database_url:
        pytest.skip("Configure PostgreSQL; SQLite is not an acceptance substitute")
    original_url, original_artifacts = settings.database_url, settings.artifact_root
    original_fixture = settings.fixture_mode
    url = make_url(original_url)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    name = "sca_test_" + secrets.token_hex(6)
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings.database_url = url.set(database=name).render_as_string(hide_password=False)
    settings.artifact_root = tmp_path_factory.mktemp("artifacts")
    settings.fixture_mode = True
    persistence.reset_engine()
    persistence.initialize()
    try:
        yield name
    finally:
        persistence.reset_engine()
        settings.database_url, settings.artifact_root = original_url, original_artifacts
        settings.fixture_mode = original_fixture
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(autouse=True)
def _supply_confirm_hash(monkeypatch, request):
    """Pass a real confirmation hash into ``runs.create`` unless a test opts out.

    The core still checks the hash. Tests marked ``no_auto_confirm`` call the
    unwrapped function and must supply or omit the hash themselves.
    """
    if request.node.get_closest_marker('no_auto_confirm'):
        yield
        return
    import importlib
    module = importlib.import_module('app.runs.create')
    original = module.create

    def wrapped(run_id, spec, confirm_hash=None, **kwargs):
        if confirm_hash is None:
            from app.segment_brief import inspect_segment
            confirm_hash = inspect_segment(spec)['confirm_hash']
        return original(run_id, spec, confirm_hash=confirm_hash, **kwargs)

    monkeypatch.setattr(module, 'create', wrapped)
    import app.runs as runs_pkg
    monkeypatch.setattr(runs_pkg, 'create', wrapped)
    yield


@pytest.fixture
def clean_db(pg_database):
    from app.persistence import transaction
    from app.persistence.models import Base
    with transaction() as session:
        names = [name for name in Base.metadata.tables if "schema" not in name]
        session.execute(text("TRUNCATE " + ", ".join('"' + name + '"' for name in names) + " CASCADE"))
    return pg_database


PACKAGING_NAME = 'Klarwerk Verpackungen GmbH'
PACKAGING_SOURCE = 'https://packaging-association.org/members/klarwerk'
UAE_NAME = 'Gulf Retail Systems LLC'
UAE_SOURCE = 'https://uae-it-association.org/members/gulf-retail'


def _now():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def packaging_spec():
    """SPEC §12.A: German reusable industrial packaging manufacturer."""
    return {
        'spec_version': 2,
        'name': '德国可重复使用工业包装制造商',
        'industry': 'industrial packaging',
        'countries': ['DE'],
        'quantity': 200,
        'paid_sources': False,
        'budget': 0,
        'qualification_policy': {
            'country_scope': 'headquarters',
            'required_all': [
                {'id': 'hq', 'field': 'entity.headquarters_country', 'operator': 'in', 'value': ['DE']},
                {'id': 'maker', 'field': 'business.models', 'operator': 'includes', 'value': 'manufacturer'},
                {'id': 'product', 'field': 'offering.products', 'operator': 'provides',
                 'value': 'reusable industrial packaging'},
            ],
            'exclude_any': [
                {'id': 'dealer_only', 'field': 'business.models', 'operator': 'includes',
                 'value': 'distributor', 'unknown_policy': 'allow_unconfirmed'},
            ],
            'contact_policy': {'mode': 'any',
                               'allowed_types': ['business_email', 'business_phone', 'contact_form'],
                               'named_person_required': False},
        },
        'evidence_policy': {'freshness_days': 180},
        'execution_policy': {'company_workers': 2, 'native_inflight': 2,
                             'snapshot_every_new_qualified': 10, 'snapshot_interval_seconds': 900},
    }


@pytest.fixture
def packaging_candidate():
    now = _now()
    name = PACKAGING_NAME
    return {
        'company_name': name, 'country_code': 'DE', 'headquarters_country': 'DE',
        'entity_relationship': 'independent', 'business_models': ['manufacturer'],
        'offering_products': ['reusable industrial packaging'],
        'industry_text': 'industrial packaging',
        'source_url': PACKAGING_SOURCE, 'source_type': 'directory', 'retrieved_at': now,
        'email': 'sales@klarwerk.de',
        'source_excerpt': f'{name}, Düsseldorf, Germany, manufactures reusable industrial packaging.',
        'sources': [
            {'field': 'company_name', 'value': name, 'source_url': PACKAGING_SOURCE,
             'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name}, Düsseldorf, Germany'},
            {'field': 'entity.headquarters_country', 'value': 'DE', 'source_url': PACKAGING_SOURCE,
             'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name}, headquartered in Düsseldorf, Germany'},
            {'field': 'business.models', 'value': 'manufacturer', 'source_url': PACKAGING_SOURCE,
             'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name} manufactures reusable industrial packaging'},
            {'field': 'offering.products', 'value': 'reusable industrial packaging',
             'source_url': PACKAGING_SOURCE, 'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name} manufactures reusable industrial packaging'},
            {'field': 'email', 'value': 'sales@klarwerk.de', 'source_url': PACKAGING_SOURCE,
             'source_type': 'directory', 'retrieved_at': now, 'method': 'agent_page_observation',
             'source_excerpt': f'{name} — Business email: sales@klarwerk.de'},
        ],
    }


@pytest.fixture
def uae_spec():
    """SPEC §12.B: UAE retail IT integrator; a local branch is acceptable."""
    return {
        'spec_version': 2,
        'name': '阿联酋零售业 IT 集成服务商',
        'industry': 'retail IT integration',
        'countries': ['AE'],
        'quantity': 200,
        'paid_sources': False,
        'budget': 0,
        'qualification_policy': {
            'country_scope': 'operating',
            'required_all': [
                {'id': 'operating', 'field': 'entity.country_code', 'operator': 'in', 'value': ['AE']},
                {'id': 'integrator', 'field': 'offering.services', 'operator': 'provides',
                 'value': 'POS and store system integration'},
                {'id': 'retail', 'field': 'customer.industries', 'operator': 'includes',
                 'value': 'retail'},
            ],
            'exclude_any': [],
            'contact_policy': {'mode': 'any', 'allowed_types': ['contact_form', 'business_email'],
                               'named_person_required': False},
        },
        'evidence_policy': {'freshness_days': 180},
        'execution_policy': {'company_workers': 2, 'native_inflight': 1,
                             'snapshot_every_new_qualified': 10, 'snapshot_interval_seconds': 900},
    }


@pytest.fixture
def uae_candidate():
    now = _now()
    name = UAE_NAME
    form = 'https://gulfretail.ae/enquiry'
    return {
        'company_name': name, 'country_code': 'AE',
        'entity_relationship': 'branch',
        'offering_services': ['POS and store system integration'],
        'customer_industries': ['retail'],
        'industry_text': 'retail IT integration',
        'website_url': 'https://gulfretail.ae/',
        'source_url': UAE_SOURCE, 'source_type': 'directory', 'retrieved_at': now,
        'contact_form_url': form,
        'source_excerpt': f'{name} — UAE branch integrating POS and store systems for retail customers.',
        'sources': [
            {'field': 'company_name', 'value': name, 'source_url': UAE_SOURCE,
             'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name}, Dubai branch'},
            {'field': 'company_name', 'value': name, 'source_url': form,
             'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name} enquiry form'},
            {'field': 'website_url', 'value': 'https://gulfretail.ae/', 'source_url': UAE_SOURCE,
             'source_type': 'directory', 'retrieved_at': now, 'method': 'agent_page_observation',
             'source_excerpt': f'{name}; official website https://gulfretail.ae/'},
            {'field': 'entity.country_code', 'value': 'AE', 'source_url': UAE_SOURCE,
             'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name}, operating in Dubai, United Arab Emirates'},
            {'field': 'offering.services', 'value': 'POS and store system integration',
             'source_url': UAE_SOURCE, 'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name} provides POS and store system integration'},
            {'field': 'customer.industries', 'value': 'retail', 'source_url': UAE_SOURCE,
             'source_type': 'directory', 'retrieved_at': now,
             'source_excerpt': f'{name} serves retail customers across the Gulf'},
            {'field': 'contact_form_url', 'value': form, 'source_url': form,
             'source_type': 'official_site', 'retrieved_at': now,
             'method': 'observed_contact_form', 'form_fields': ['email', 'message'],
             'source_excerpt': f'{name} enquiry form'},
        ],
    }
