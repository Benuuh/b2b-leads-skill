"""Database isolation and connection option preservation."""
from sqlalchemy.engine import make_url
import pytest

from app.config import Settings, settings
from app.errors import AppError
from app.persistence.connection import parameters, environment
from app import runtime


def test_plain_postgres_url_uses_installed_psycopg_driver():
    config = Settings(database_url="postgresql://tester:password@localhost/new_skill")
    config.validate()
    assert make_url(config.database_url).drivername == "postgresql+psycopg"


def test_connection_and_tool_options_preserved(monkeypatch):
    url = make_url("postgresql+psycopg://tester:password@localhost/new_skill?sslmode=verify-full&sslrootcert=C%3A%2Froot.crt&target_session_attrs=read-write&application_name=skill-test")
    values = parameters(url)
    assert values["sslmode"] == "verify-full"
    assert values["sslrootcert"] == "C:/root.crt"
    env = environment(url)
    assert env["PGSSLMODE"] == "verify-full"
    assert env["PGSSLROOTCERT"] == "C:/root.crt"
    assert env["PGTARGETSESSIONATTRS"] == "read-write"
    assert env["PGAPPNAME"] == "skill-test"
    calls = []
    monkeypatch.setattr(runtime.psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    runtime._connect(url)
    assert calls[0]["sslmode"] == "verify-full"


def test_prepare_refuses_system_database(monkeypatch):
    monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://localhost/postgres")
    with pytest.raises(AppError) as exc:
        runtime.prepare()
    assert exc.value.code == "UNSAFE_DATABASE"


def test_existing_legacy_connection_derives_isolated_database(monkeypatch):
    from app.config import database_url
    monkeypatch.delenv("SCA_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://tester:password@localhost/old_app?sslmode=require")
    value = make_url(database_url())
    assert value.database == "sca_skill_v2"
    assert value.query["sslmode"] == "require"
