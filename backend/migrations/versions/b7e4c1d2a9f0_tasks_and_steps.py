"""tasks and task steps

Revision ID: b7e4c1d2a9f0
Revises: 5c27a2d57d53
Create Date: 2026-11-16 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4c1d2a9f0'
down_revision: Union[str, Sequence[str], None] = '5c27a2d57d53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'agent_tasks',
        sa.Column('agent_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=True),
        sa.Column('goal', sa.Text(), nullable=False),
        sa.Column('constraints', sa.JSON(), nullable=False),
        sa.Column('success_criteria', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('agent_tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_tasks_agent_id'), ['agent_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_tasks_conversation_id'), ['conversation_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_tasks_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_tasks_user_id'), ['user_id'], unique=False)

    op.create_table(
        'task_steps',
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('step_key', sa.String(length=40), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('result', sa.Text(), nullable=True),
        sa.Column('verified', sa.Boolean(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['agent_tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'step_key', name='uq_task_step_key'),
    )
    with op.batch_alter_table('task_steps', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_task_steps_task_id'), ['task_id'], unique=False)

    with op.batch_alter_table('agent_executions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('task_id', sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f('ix_agent_executions_task_id'), ['task_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_agent_executions_task_id',
            'agent_tasks',
            ['task_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('agent_executions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_agent_executions_task_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_agent_executions_task_id'))
        batch_op.drop_column('task_id')

    with op.batch_alter_table('task_steps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_steps_task_id'))

    op.drop_table('task_steps')

    with op.batch_alter_table('agent_tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_tasks_user_id'))
        batch_op.drop_index(batch_op.f('ix_agent_tasks_status'))
        batch_op.drop_index(batch_op.f('ix_agent_tasks_conversation_id'))
        batch_op.drop_index(batch_op.f('ix_agent_tasks_agent_id'))

    op.drop_table('agent_tasks')
