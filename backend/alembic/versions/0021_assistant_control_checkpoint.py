"""Persist upstream cancellation identity before chat submission."""
from alembic import op
import sqlalchemy as sa

revision = '0021_assistant_control'
down_revision = '0020_assistant_documents'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('assistant_compute_gate', sa.Column('control_token', sa.String(64)))
    op.add_column('assistant_compute_gate', sa.Column('control_epoch', sa.String(64)))


def downgrade():
    op.drop_column('assistant_compute_gate', 'control_epoch')
    op.drop_column('assistant_compute_gate', 'control_token')
