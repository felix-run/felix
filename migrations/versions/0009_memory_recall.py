"""Make memory_vectors a real record: supersession, provenance, and search columns.

Revision ID: 0009_memory_recall
Revises: 0008_fiber_leases
Create Date: 2026-08-23

`spec.memory.store: pgvector` has been declared by every bundled manifest and backed
by nothing usable. The vector column and its HNSW index have existed since
0001_baseline, but the column is `NOT NULL` with no default and `put_memory` never
supplied a vector — so **every insert into this table has failed on a real Postgres**,
silently, because the only caller wraps it in `except: logger.debug(...)`. Long-term
memory has never stored a row outside the in-memory twin.

This drops that constraint and adds the columns that make supersession, provenance and
hybrid recall possible.

`memory_vectors` is already in `0006_tenant_rls._TENANT_TABLES`, so its policy covers
the new columns. `memory_vector_config` deliberately has no `tenant_id` — it records
what dimension the deployment's vector column was built at, which is deployment
config rather than tenant data, and so is correctly outside the policy set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_memory_recall"
down_revision: str | None = "0008_fiber_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The vector column is `vector(768)` from the baseline, and HNSW needs the dimension
# fixed, so this only records what the column already is rather than choosing it.
# Bounded because pgvector indexes at most 2000 dimensions.
_MIN_DIM = 8
_MAX_DIM = 2000
_DEFAULT_DIM = 768


def _embedding_dim() -> int:
    from felix.config import get_settings

    raw = int(getattr(get_settings(), "memory_embedding_dim", _DEFAULT_DIM) or _DEFAULT_DIM)
    return max(_MIN_DIM, min(raw, _MAX_DIM))


def upgrade() -> None:
    op.add_column("memory_vectors", sa.Column("topic_key", sa.Text(), nullable=True))
    op.add_column(
        "memory_vectors",
        sa.Column("importance", sa.Float(), server_default=sa.text("0.5"), nullable=False),
    )
    op.add_column(
        "memory_vectors",
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
    )
    op.add_column("memory_vectors", sa.Column("superseded_by", sa.Text(), nullable=True))
    op.add_column(
        "memory_vectors",
        sa.Column("updated_at", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("memory_vectors", sa.Column("last_used_at", sa.BigInteger(), nullable=True))
    op.add_column(
        "memory_vectors",
        sa.Column("thread_id", sa.Text(), server_default="", nullable=False),
    )
    op.add_column("memory_vectors", sa.Column("embedding_dim", sa.Integer(), nullable=True))
    op.add_column(
        "memory_vectors",
        sa.Column("embedding_model", sa.Text(), server_default="", nullable=False),
    )

    # Existing rows predate all of this. `superseded_seq IS NOT NULL` was the only way a
    # row could be inactive before, so it is what `status` has to agree with.
    op.execute("UPDATE memory_vectors SET updated_at = created_at WHERE updated_at = 0")
    op.execute("UPDATE memory_vectors SET status = 'superseded' WHERE superseded_seq IS NOT NULL")

    # Full-text over content, and over the topic key as identifier-ish text. 'simple'
    # for topics on purpose: they are dotted identifiers ("user.timezone"), so stemming
    # and stopword removal would only lose signal.
    op.execute(
        """
        ALTER TABLE memory_vectors
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
        """
    )
    op.execute(
        """
        ALTER TABLE memory_vectors
        ADD COLUMN IF NOT EXISTS topic_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple', replace(coalesce(topic_key, ''), '.', ' '))
        ) STORED
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_memory_content_tsv ON memory_vectors USING GIN (content_tsv)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_memory_topic_tsv ON memory_vectors USING GIN (topic_tsv)")

    # memory_vectors had no index at all beyond its PK; list_active was a seq scan.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_active
        ON memory_vectors (tenant_id, manifest_id, status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_topic
        ON memory_vectors (tenant_id, manifest_id, topic_key, status)
        WHERE topic_key IS NOT NULL
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_memory_turn ON memory_vectors (tenant_id, origin_seq)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_vector_config (
            id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            dim integer,
            embedding_model text NOT NULL DEFAULT '',
            created_at bigint NOT NULL DEFAULT 0
        )
        """
    )

    # `embedding vector(768) NOT NULL` and its HNSW index already exist — added by
    # 0001_baseline, invisibly from `db/models.py`, which is why they were easy to miss.
    #
    # NOT NULL with no default is the bug: `put_memory` never supplied a vector, so
    # every insert into this table failed on a real Postgres with a NotNullViolation,
    # and `capture.py` swallowed it into a debug log. Long-term memory has never
    # written a row in production. A memory without an embedding has to be storable —
    # the whole design degrades to full-text when no embedder is configured.
    op.execute("ALTER TABLE memory_vectors ALTER COLUMN embedding DROP NOT NULL")

    # Record the dimension the column was actually built at, so runtime can detect a
    # mismatched embedder rather than failing per row. 768 matches both the baseline
    # column and `bge-base-en-v1.5`, the model the rest of the repo already defaults to.
    op.execute(
        f"""
        INSERT INTO memory_vector_config (id, dim, created_at)
        VALUES (1, {_embedding_dim()}, 0)
        ON CONFLICT (id) DO UPDATE SET dim = EXCLUDED.dim
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_turn")
    op.execute("DROP INDEX IF EXISTS idx_memory_topic")
    op.execute("DROP INDEX IF EXISTS idx_memory_active")
    op.execute("DROP INDEX IF EXISTS idx_memory_topic_tsv")
    op.execute("DROP INDEX IF EXISTS idx_memory_content_tsv")
    op.execute("DROP TABLE IF EXISTS memory_vector_config")
    op.execute("ALTER TABLE memory_vectors ALTER COLUMN embedding SET NOT NULL")
    op.execute("ALTER TABLE memory_vectors DROP COLUMN IF EXISTS topic_tsv")
    op.execute("ALTER TABLE memory_vectors DROP COLUMN IF EXISTS content_tsv")
    for column in (
        "embedding_model",
        "embedding_dim",
        "thread_id",
        "last_used_at",
        "updated_at",
        "superseded_by",
        "status",
        "importance",
        "topic_key",
    ):
        op.execute(f"ALTER TABLE memory_vectors DROP COLUMN IF EXISTS {column}")
