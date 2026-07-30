"""goal 12: multiple GitHub tokens (one per resource owner)

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-30 12:00:00.000000

A fine-grained PAT is bound to a single GitHub resource owner at mint time, so filing
issues into both a personal account and an org needs one token each. The single
`dev_config.pat_encrypted` column (goal-12 v1) is superseded by the new `dev_pat` table,
keyed by `(user_id, owner)` — filing routes to the token whose owner matches the target
repo's owner.

Data migration (forward-only, secret-preserving): for every `dev_config` row that still
holds a PAT, the owner(s) are derived from that config's `repos_json` (`owner/name` →
`owner`); the encrypted token is copied into a `dev_pat` row per owner, and the legacy
column is NULLed. A config with a PAT but no configured repos can't have its owner
inferred, so its token is left in place (untouched, unread) rather than silently
dropped — the user simply re-adds it through the new multi-token UI.
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_Str = sqlmodel.sql.sqltypes.AutoString


def _owners_from_repos_json(repos_json: str | None) -> list[str]:
    try:
        repos = json.loads(repos_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    owners: set[str] = set()
    for r in repos:
        if isinstance(r, dict):
            full = r.get("full_name") or ""
            if "/" in full:
                owner = full.split("/", 1)[0].strip()
                if owner:
                    owners.add(owner)
    return sorted(owners)


def upgrade() -> None:
    op.create_table(
        "dev_pat",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("owner", _Str(), nullable=False),
        sa.Column("pat_encrypted", _Str(), nullable=False),
        sa.Column("login", _Str(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "owner", name="uq_dev_pat_user_owner"),
    )
    op.create_index("ix_dev_pat_user_id", "dev_pat", ["user_id"])
    op.create_index("ix_dev_pat_owner", "dev_pat", ["owner"])

    # ── Data migration: legacy dev_config.pat_encrypted → dev_pat rows ────────────
    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text(
                "SELECT user_id, pat_encrypted, repos_json, updated_at "
                "FROM dev_config WHERE pat_encrypted IS NOT NULL"
            )
        )
        .mappings()
        .all()
    )

    migrated_user_ids: list[int] = []
    for row in rows:
        owners = _owners_from_repos_json(row["repos_json"])
        if not owners:
            # Owner un-inferrable → leave the legacy token untouched (nothing lost).
            continue
        for owner in owners:
            bind.execute(
                sa.text(
                    "INSERT INTO dev_pat (user_id, owner, pat_encrypted, login, "
                    "updated_at) VALUES (:user_id, :owner, :pat, NULL, :updated_at)"
                ),
                {
                    "user_id": row["user_id"],
                    "owner": owner,
                    "pat": row["pat_encrypted"],
                    "updated_at": row["updated_at"],
                },
            )
        migrated_user_ids.append(row["user_id"])

    # NULL out only the legacy tokens we successfully copied (the secret now lives in
    # dev_pat; don't duplicate it, and don't drop one we couldn't route).
    for user_id in migrated_user_ids:
        bind.execute(
            sa.text(
                "UPDATE dev_config SET pat_encrypted = NULL WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        )


def downgrade() -> None:
    op.drop_index("ix_dev_pat_owner", "dev_pat")
    op.drop_index("ix_dev_pat_user_id", "dev_pat")
    op.drop_table("dev_pat")
