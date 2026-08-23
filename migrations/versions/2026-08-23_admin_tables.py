"""admin tables

Revision ID: d8d3c7d79f5b
Revises: fd533a196e23
Create Date: 2026-08-23 17:32:33.533305

Brings the panel's three tables — `admin`, `role` and the `roles_admins` association —
under Alembic. They were previously created outside it, by `db.create_all()` in the
Flask panel's `init_db()`, which meant the schema of a whole third of the database was
whatever the last-imported Flask model happened to say.

Autogenerate also emitted `op.create_unique_constraint(None, 'users', ['id'])` here.
That is the repo's known cosmetic `alembic check` drift — `big_int_pk` sets
`unique=True` on a primary key, so the model looks to autogenerate like it wants a
second unique constraint on top of the PK. It is deliberately NOT carried into this
migration; it has nothing to do with the admin tables and would add a redundant index
on every users row.

`fs_uniquifier` and `confirmed_at` from the old Flask `AdminModel` are intentionally
absent: both are Flask-Security-Too internals (session invalidation and the disabled
email-confirmation flow) and that library is being removed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8d3c7d79f5b'
down_revision: Union[str, None] = 'fd533a196e23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('admin',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('first_name', sa.String(length=255), nullable=True),
    sa.Column('last_name', sa.String(length=255), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password', sa.String(length=255), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text("TIMEZONE('utc', now())"), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('role',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('roles_admins',
    sa.Column('admin_id', sa.Integer(), nullable=False),
    sa.Column('role_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['admin_id'], ['admin.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['role.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('admin_id', 'role_id')
    )


def downgrade() -> None:
    op.drop_table('roles_admins')
    op.drop_table('role')
    op.drop_table('admin')
