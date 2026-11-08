"""Data-source adapters: unverified candidate pool and raw record envelopes."""
revision = 'skill_v2_003'
down_revision = 'skill_v2_002'
branch_labels = None
depends_on = None


def upgrade():
    from alembic import op
    from app.persistence import DISCOVERY_TABLES
    from app.persistence.models import Base
    Base.metadata.create_all(op.get_bind(), tables=[
        Base.metadata.tables[name] for name in DISCOVERY_TABLES])


def downgrade():
    raise RuntimeError('Destructive downgrade is disabled; restore an isolated backup instead')
