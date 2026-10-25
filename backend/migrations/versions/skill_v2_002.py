"""Durable host throttles, artifact deletion outbox and report digests."""
from alembic import op
from app.persistence.schema import add_operational_tables

revision = 'skill_v2_002'
down_revision = 'skill_v2_001'
branch_labels = None
depends_on = None


def upgrade():
    add_operational_tables(op.get_bind())


def downgrade():
    raise RuntimeError('Destructive downgrade is disabled; restore an isolated backup instead')
