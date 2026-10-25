"""Independent single-user Skill baseline; legacy databases are never upgraded."""
from alembic import op
from app.persistence.models import Base

revision = 'skill_v2_001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError('Destructive downgrade is disabled; restore an isolated backup instead')
