"""Additive shared-directory page budget ledger. No discovery uniqueness change."""
revision = 'skill_v2_004'
down_revision = 'skill_v2_003'
branch_labels = None
depends_on = None


def upgrade():
    from alembic import op
    from app.persistence import PAGE_BUDGET_TABLES
    from app.persistence.models import Base
    Base.metadata.create_all(op.get_bind(), tables=[
        Base.metadata.tables[name] for name in PAGE_BUDGET_TABLES])


def downgrade():
    raise RuntimeError(
        'Destructive downgrade is disabled. skill_v2_004 is additive and a '
        '2.7.0 process will not read it. Restore an isolated pre-upgrade '
        'backup of the database and artifact directory instead.')
