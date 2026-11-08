"""Startup secret validation（spec b2b-leads-2.6 §3.5）.

原则：宁可起不来，不要跑一半崩。缺密钥时在命令启动即拒，
而不是让 ``discover`` 跑到一半崩掉、留下不一致的 run 状态。

三个密钥：
- ``PROSPECT_SOURCE_RAW_ENVELOPE_SECRET``：原始记录信封加密密钥。
  生产缺失 → 拒绝启动；开发缺失 → 降级明文存信封并打 ``dev-insecure`` 标记。
- ``PROSPECT_CURSOR_SECRET``：游标签名密钥（S13 预埋，未实现分页）。
  不分页可不配；一旦配置，长度必须 ≥32 字节。
- ``PROVIDER_CREDENTIAL_KEY``：付费数据源凭据密钥。当前 ``paid_sources=0``，
  状态恒为 ``n/a``；未来接入付费源后缺失即拒。
"""
from __future__ import annotations

import os

from app.config import ROOT  # noqa: F401  导入即加载 ROOT/.env

# 与其他密钥隔离：信封密钥独立、游标密钥独立，不混用。
ENV_NAMES = {
    'envelope': 'PROSPECT_SOURCE_RAW_ENVELOPE_SECRET',
    'cursor': 'PROSPECT_CURSOR_SECRET',
    'provider': 'PROVIDER_CREDENTIAL_KEY',
}
CURSOR_SECRET_MIN_BYTES = 32
PRODUCTION_ENVS = {'production', 'prod'}


def runtime_env():
    """部署环境名；未设置视为开发环境（本地单用户 Skill 的默认形态）。"""
    return os.getenv('SCA_ENV', 'development').strip().lower()


def paid_sources_enabled():
    from app.capabilities import LIMITS
    return int(LIMITS.get('paid_sources', 0)) > 0


def secrets_status():
    """Return {envelope, cursor, provider} statuses; offline, no DB, no network."""
    envelope = os.getenv(ENV_NAMES['envelope'], '').strip()
    if envelope:
        envelope_status = 'ok'
    elif runtime_env() in PRODUCTION_ENVS:
        envelope_status = 'missing'
    else:
        # 开发降级：信封明文存储，调用方必须把这个标记透出。
        envelope_status = 'dev-insecure'
    cursor = os.getenv(ENV_NAMES['cursor'], '').strip()
    if not cursor:
        # 不分页时可不配；状态如实报告，不算错误。
        cursor_status = 'missing'
    elif len(cursor.encode('utf-8')) >= CURSOR_SECRET_MIN_BYTES:
        cursor_status = 'ok'
    else:
        cursor_status = 'invalid_length'
    if not paid_sources_enabled():
        provider_status = 'n/a'
    else:
        provider_status = 'ok' if os.getenv(ENV_NAMES['provider'], '').strip() else 'missing'
    return {'envelope': envelope_status, 'cursor': cursor_status, 'provider': provider_status}


def validate_startup_secrets():
    """启动即拒：生产缺信封密钥、游标密钥长度不足、付费源缺凭据都拒绝。

    返回当前状态字典；有问题时抛 ``AppError``（命令不执行任何后续动作）。
    """
    from app.errors import AppError
    status = secrets_status()
    problems = []
    if status['envelope'] == 'missing':
        problems.append('生产环境缺少信封密钥 ' + ENV_NAMES['envelope'] +
                        '；没有它 discover 会在信封写入时崩溃，拒绝启动')
    if status['cursor'] == 'invalid_length':
        problems.append(ENV_NAMES['cursor'] + ' 长度必须 ≥32 字节')
    if status['provider'] == 'missing':
        problems.append('已启用付费数据源但缺少 ' + ENV_NAMES['provider'])
    if problems:
        raise AppError('SECRETS_MISSING', '；'.join(problems),
                       status=423, details={'secrets': status})
    return status
