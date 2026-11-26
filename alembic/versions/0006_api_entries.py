"""api_entries

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('watched_source', sa.Column('api_entries', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('watched_source', 'api_entries')
