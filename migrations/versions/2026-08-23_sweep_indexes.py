"""sweep indexes

Revision ID: fd533a196e23
Revises: 7ee1a0d2d8dc
Create Date: 2026-08-23 12:59:48.854555

Indexes for the hourly `payments:expire_premium` sweep, which planned as
`Seq Scan on users` + `Seq Scan on payments` and so grew linearly with both tables,
hourly, forever.

Autogenerate also emitted `op.create_unique_constraint(None, 'users', ['id'])` here.
That is the repo's known cosmetic `alembic check` drift — `big_int_pk` sets
`unique=True` on a primary key, so the model looks to autogenerate like it wants a
second unique constraint on top of the PK. It is deliberately NOT carried into this
migration: it would add a redundant index on every users row and is not what this
change is about.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd533a196e23'
down_revision: Union[str, None] = '7ee1a0d2d8dc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The `still_paid` EXISTS subquery: equality on `status`, range on the expiry.
    op.create_index('ix_payments_status_expires_at', 'payments', ['status', 'subscription_expires_at'], unique=False)
    # Partial: the sweep only ever looks at premium users, a small minority of the
    # table, so indexing just those rows keeps this tiny and leaves the ordinary
    # non-premium write path unburdened.
    op.create_index('ix_users_premium', 'users', ['id'], unique=False, postgresql_where=sa.text('is_premium IS TRUE'))


def downgrade() -> None:
    op.drop_index('ix_users_premium', table_name='users', postgresql_where=sa.text('is_premium IS TRUE'))
    op.drop_index('ix_payments_status_expires_at', table_name='payments')
