"""identities.provider admits 'google' (T6.0, contracts/NOTES.md D26).

Sign in with Google stores its identity as `('google', <Google sub>)`. Only the CHECK widens;
no column changes (the Apple-only columns stay null for Google rows).

Downgrade narrows the CHECK back to 'apple' and refuses (raises, changing nothing) while any
Google identity exists: deleting them would strand those users without a way to sign in, so
that decision is left to an operator.

Revision ID: 0003_google_identity
Revises: 0002_changeset_reason
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_google_identity"
down_revision: str | None = "0002_changeset_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE identities DROP CONSTRAINT ck_identities_provider")
    op.execute(
        "ALTER TABLE identities ADD CONSTRAINT ck_identities_provider"
        " CHECK (provider IN ('apple', 'google'))"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM identities WHERE provider = 'google') THEN
            RAISE EXCEPTION 'cannot downgrade 0003_google_identity: google identities exist';
          END IF;
        END
        $$;
        """
    )
    op.execute("ALTER TABLE identities DROP CONSTRAINT ck_identities_provider")
    op.execute(
        "ALTER TABLE identities ADD CONSTRAINT ck_identities_provider CHECK (provider IN ('apple'))"
    )
