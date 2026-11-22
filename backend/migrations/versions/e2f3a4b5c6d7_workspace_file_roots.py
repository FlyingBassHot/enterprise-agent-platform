"""workspace file roots

Revision ID: e2f3a4b5c6d7
Revises: c9d1e2f3a4b5
Create Date: 2026-11-19 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'c9d1e2f3a4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create logical file roots and agent-to-root grants."""
    op.create_table(
        'file_roots',
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('owner_id', sa.String(length=36), nullable=False),
        sa.Column('storage_path', sa.String(length=512), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('storage_path'),
    )
    with op.batch_alter_table('file_roots', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_file_roots_name'), ['name'], unique=True
        )
        batch_op.create_index(
            batch_op.f('ix_file_roots_owner_id'), ['owner_id'], unique=False
        )

    op.create_table(
        'agent_file_roots',
        sa.Column('agent_id', sa.String(length=36), nullable=False),
        sa.Column('root_id', sa.String(length=36), nullable=False),
        sa.Column('permission', sa.String(length=16), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('(CURRENT_TIMESTAMP)'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['root_id'], ['file_roots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_id', 'root_id', name='uq_agent_file_root'),
    )
    with op.batch_alter_table('agent_file_roots', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_agent_file_roots_agent_id'), ['agent_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_agent_file_roots_root_id'), ['root_id'], unique=False
        )


def downgrade() -> None:
    """Drop workspace file root tables."""
    with op.batch_alter_table('agent_file_roots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_file_roots_root_id'))
        batch_op.drop_index(batch_op.f('ix_agent_file_roots_agent_id'))
    op.drop_table('agent_file_roots')

    with op.batch_alter_table('file_roots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_file_roots_owner_id'))
        batch_op.drop_index(batch_op.f('ix_file_roots_name'))
    op.drop_table('file_roots')
