"""No identity, workspace, segment, source catalog or generic queue tables."""
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from . import utcnow


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Run(Record, Base):
    __tablename__ = 'skill_runs'
    spec_json: Mapped[dict] = mapped_column(JSONB)
    spec_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='running', index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    demo_only: Mapped[bool] = mapped_column(Boolean, default=False)
    plan_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    stats_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    errors_json: Mapped[list] = mapped_column(JSONB, default=list)
    active_seconds: Mapped[float] = mapped_column(Float, default=0)
    active_started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    process_token: Mapped[str | None] = mapped_column(String(80))
    process_until: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class Job(Record, Base):
    __tablename__ = 'skill_jobs'
    run_id: Mapped[str] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    entity_key: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict] = mapped_column(JSONB)
    output_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    state: Mapped[str] = mapped_column(String(20), default='queued')
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_token: Mapped[str | None] = mapped_column(String(80))
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    error_json: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint('run_id', 'entity_key'),)


class Company(Record, Base):
    __tablename__ = 'skill_companies'
    run_id: Mapped[str] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    identity_key: Mapped[str] = mapped_column(String(64))
    data_json: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('run_id', 'identity_key'),)


class Snapshot(Record, Base):
    __tablename__ = 'skill_snapshots'
    run_id: Mapped[str] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey('skill_companies.id', ondelete='CASCADE'), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    artifact_ref: Mapped[str | None] = mapped_column(Text)
    artifact_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))


class Suppression(Record, Base):
    __tablename__ = 'skill_suppressions'
    type: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint('type', 'value'),)


class Export(Record, Base):
    __tablename__ = 'skill_exports'
    run_id: Mapped[str] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    format: Mapped[str] = mapped_column(String(10))
    filename: Mapped[str] = mapped_column(Text)
    artifact_ref: Mapped[str | None] = mapped_column(Text)
    artifact_hash: Mapped[str] = mapped_column(String(64))
    report_ref: Mapped[str | None] = mapped_column(Text)
    report_hash: Mapped[str | None] = mapped_column(String(64))
    manifest_json: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class Operation(Record, Base):
    __tablename__ = 'skill_operations'
    run_id: Mapped[str] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    command: Mapped[str] = mapped_column(String(30))
    key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default='completed')
    result_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint('run_id', 'command', 'key'),)


class Observation(Record, Base):
    __tablename__ = 'skill_observations'
    run_id: Mapped[str | None] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    job_id: Mapped[str | None] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    data_json: Mapped[dict] = mapped_column(JSONB)


class Audit(Record, Base):
    __tablename__ = 'skill_audits'
    run_id: Mapped[str | None] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    action: Mapped[str] = mapped_column(String(60))
    details_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class MaintenanceState(Record, Base):
    __tablename__ = 'skill_maintenance'
    last_run_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))


class HostLimit(Base):
    __tablename__ = 'skill_host_limits'
    host: Mapped[str] = mapped_column(String(253), primary_key=True)
    last_started_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    next_allowed_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(80))
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RobotsCache(Base):
    __tablename__ = 'skill_robots_cache'
    origin: Mapped[str] = mapped_column(String(512), primary_key=True)
    status: Mapped[int] = mapped_column(Integer)
    rules_text: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True), index=True)


class PendingArtifactDelete(Base):
    __tablename__ = 'skill_pending_artifact_deletes'
    artifact_ref: Mapped[str] = mapped_column(String(1024), primary_key=True)
    requested_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))


class ProspectSourceCandidate(Record, Base):
    """未核实候选池（spec b2b-leads-2.6 §3.1）。

    硬约束：这里不是候选公司。不进合格计数、不进去重池；
    只有 agent 带 evidence ``advance`` 之后才进入正式候选。
    """
    __tablename__ = 'prospect_source_candidates'
    run_id: Mapped[str] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    source: Mapped[str] = mapped_column(String(40))
    source_ref: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(512))
    domain: Mapped[str | None] = mapped_column(String(253))
    country: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[dict] = mapped_column(JSONB, default=dict)


class ProspectSourceEnvelope(Record, Base):
    """原始记录信封：审计时还原"当时源里给的是什么"，平时不读。"""
    __tablename__ = 'prospect_source_envelopes'
    run_id: Mapped[str] = mapped_column(ForeignKey('skill_runs.id', ondelete='CASCADE'), index=True)
    source: Mapped[str] = mapped_column(String(40))
    source_ref: Mapped[str] = mapped_column(Text)
    envelope_ciphertext: Mapped[str] = mapped_column(Text)
    envelope_sha256: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
