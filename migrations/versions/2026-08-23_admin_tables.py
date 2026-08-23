"""admin tables

Revision ID: d8d3c7d79f5b
Revises: fd533a196e23
Create Date: 2026-08-23 17:32:33.533305

Brings the panel's three tables — `admin`, `role` and the `roles_admins` association —
under Alembic. They were previously created outside it, by `db.create_all()` in the
Flask panel's `init_db()`, which meant the schema of a whole third of the database was
whatever the last-imported Flask model happened to say.

Because of that, a database that has ever started the Flask panel already has these
tables and `create_table` would fail on it with a bare `DuplicateTableError`. That case
is detected up front and reported with instructions instead — see `_reject_legacy_tables`.

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

CREATED_TABLES = ("admin", "role", "roles_admins")


def _reject_legacy_tables() -> None:
    """Fail loudly if the Flask panel's `init_db()` got here first.

    Adapting those tables in place is not offered on purpose. Every password in them is
    a Flask-Security `pbkdf2_sha512` digest, and the replacement panel verifies only
    `scrypt$<salt>$<digest>` — so an in-place upgrade would leave admin rows nobody can
    log in as, which is worse than failing, because it looks like it worked. Dropping
    them here is not offered either: they hold operator accounts, and destroying those
    silently is not a migration's decision to make.
    """
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    legacy = sorted(existing.intersection(CREATED_TABLES))
    if not legacy:
        return

    msg = (
        f"Tables {legacy} already exist. They were created outside Alembic by the Flask panel's "
        "init_db(), which this migration replaces. Their stored passwords are in Flask-Security's "
        "format and cannot be verified by the new scrypt hashing, so there is nothing in them to "
        "preserve. Back them up if you want the email addresses, then run:\n"
        "  DROP TABLE IF EXISTS roles_admins, admin, role;\n"
        "and re-run `alembic upgrade head`. A default superuser is seeded on first start."
    )
    raise RuntimeError(msg)


def upgrade() -> None:
    _reject_legacy_tables()

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
