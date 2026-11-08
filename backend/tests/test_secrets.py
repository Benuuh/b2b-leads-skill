"""Spec b2b-leads-2.6 §3.5 密钥启动校验。"""
import pytest

from app import secrets as secrets_module
from app.errors import AppError
from app.secrets import ENV_NAMES, secrets_status, validate_startup_secrets


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(secrets_module, 'runtime_env', lambda: 'development')


def test_development_without_envelope_is_dev_insecure():
    status = secrets_status()
    assert status['envelope'] == 'dev-insecure'
    assert status['cursor'] == 'missing'
    assert status['provider'] == 'n/a'
    # 开发环境不拒绝启动。
    assert validate_startup_secrets()['envelope'] == 'dev-insecure'


def test_production_without_envelope_refuses_startup(monkeypatch):
    monkeypatch.setattr(secrets_module, 'runtime_env', lambda: 'production')
    assert secrets_status()['envelope'] == 'missing'
    with pytest.raises(AppError) as error:
        validate_startup_secrets()
    assert error.value.code == 'SECRETS_MISSING'
    assert error.value.details['secrets']['envelope'] == 'missing'


def test_envelope_secret_present_is_ok(monkeypatch):
    monkeypatch.setenv(ENV_NAMES['envelope'], 'unit-test-secret')
    assert secrets_status()['envelope'] == 'ok'


def test_cursor_secret_length_enforced(monkeypatch):
    monkeypatch.setenv(ENV_NAMES['cursor'], 'short')
    assert secrets_status()['cursor'] == 'invalid_length'
    with pytest.raises(AppError) as error:
        validate_startup_secrets()
    assert 'cursor' in error.value.details['secrets']
    monkeypatch.setenv(ENV_NAMES['cursor'], 'x' * 32)
    assert secrets_status()['cursor'] == 'ok'
    validate_startup_secrets()


def test_provider_n_a_until_paid_sources_enabled(monkeypatch):
    assert secrets_status()['provider'] == 'n/a'


def test_capabilities_reports_secrets():
    from app.capabilities import capabilities
    payload = capabilities()
    assert set(payload['secrets']) == {'envelope', 'cursor', 'provider'}
