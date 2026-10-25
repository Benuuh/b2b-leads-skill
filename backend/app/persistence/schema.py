"""Additive local Skill schema changes, shared by prepare and Alembic."""
from sqlalchemy import inspect, text


def add_operational_tables(connection):
    from .models import HostLimit, PendingArtifactDelete, RobotsCache
    HostLimit.__table__.create(connection, checkfirst=True)
    PendingArtifactDelete.__table__.create(connection, checkfirst=True)
    RobotsCache.__table__.create(connection, checkfirst=True)
    columns = {column['name'] for column in inspect(connection).get_columns('skill_exports')}
    if 'report_hash' not in columns:
        connection.execute(text('ALTER TABLE skill_exports ADD COLUMN report_hash varchar(64)'))
