"""latest_page

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06 17:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('watched_source', sa.Column('latest_page', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('watched_source', 'latest_page')
