"""tool execution call identity

Revision ID: c9d1e2f3a4b5
Revises: b7e4c1d2a9f0
Create Date: 2026-11-16 20:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = 'b7e4c1d2a9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tool_executions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tool_call_id', sa.String(length=128), nullable=True))
        batch_op.create_unique_constraint(
            'uq_tool_execution_call', ['execution_id', 'tool_call_id']
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tool_executions', schema=None) as batch_op:
        batch_op.drop_constraint('uq_tool_execution_call', type_='unique')
        batch_op.drop_column('tool_call_id')
