"""bind conversations to file-root workspaces

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-11-20 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, Sequence[str], None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Attach each conversation to an optional file-root workspace."""
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('root_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            'fk_conversations_root_id_file_roots',
            'file_roots',
            ['root_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(
            batch_op.f('ix_conversations_root_id'), ['root_id'], unique=False
        )
        batch_op.create_index(
            'ix_conversations_agent_root', ['agent_id', 'root_id'], unique=False
        )


def downgrade() -> None:
    """Detach conversations from workspaces without touching their history."""
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_index('ix_conversations_agent_root')
        batch_op.drop_index(batch_op.f('ix_conversations_root_id'))
        batch_op.drop_constraint(
            'fk_conversations_root_id_file_roots', type_='foreignkey'
        )
        batch_op.drop_column('root_id')
