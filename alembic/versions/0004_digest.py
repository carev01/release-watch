"""digest

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-04 09:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('digest',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('cards', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_digest_created_at'), 'digest', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_digest_created_at'), table_name='digest')
    op.drop_table('digest')
