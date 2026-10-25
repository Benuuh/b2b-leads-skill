"""PostgreSQL integration tests always use a fresh isolated database."""
import secrets

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


@pytest.fixture
def clean_db(pg_database):
    from app.persistence import transaction
    from app.persistence.models import Base
    with transaction() as session:
        names = [name for name in Base.metadata.tables if "schema" not in name]
        session.execute(text("TRUNCATE " + ", ".join('"' + name + '"' for name in names) + " CASCADE"))
    return pg_database
