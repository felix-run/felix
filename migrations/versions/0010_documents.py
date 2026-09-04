"""Document chunks: an operator-owned corpus an agent can search.

Revision ID: 0010_documents
Revises: 0009_memory_recall
Create Date: 2026-09-03

Long-term memory stores *facts an agent wrote*. This stores *text an operator ingested* —
different lifecycle, different trust, different write path — so it gets its own table rather
than another `kind` in `memory_vectors`, whose supersession, trust ranking and turn-interval
columns all mean nothing here.

Shaped after `0009_memory_recall`, deliberately: a generated `content_tsv` with a GIN index
for the lexical channel, a nullable pgvector column with an HNSW index for the semantic one,
and hybrid fusion over both. Nullable is the important half — `FELIX_MEMORY_EMBEDDER` is
`none` by default, so a deployment that ingests documents without an embedder must still get
full-text retrieval rather than a NOT NULL violation per chunk. That constraint is exactly
what made `memory_vectors` unwritable in production for months.

`document_chunks` joins the RLS policy set below rather than waiting for a follow-up. A
tenant table added outside `_TENANT_TABLES` is one the isolation policy silently does not
cover, and the corpus is the kind of thing tenants most expect to be private.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_documents"
down_revision: str | None = "0009_memory_recall"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors `0009`'s bounds. HNSW indexes at most 2000 dimensions, and the dimension has to be
# fixed in the DDL, so this records the deployment's choice rather than inventing one.
_MIN_DIM = 8
_MAX_DIM = 2000
_DEFAULT_DIM = 768


def _embedding_dim() -> int:
    from felix.config import get_settings

    raw = int(getattr(get_settings(), "memory_embedding_dim", _DEFAULT_DIM) or _DEFAULT_DIM)
    return max(_MIN_DIM, min(raw, _MAX_DIM))


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.Text(), server_default="", nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("metadata", sa.dialects.postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("embedding_model", sa.Text(), server_default="", nullable=False),
    )

    # Grouping and replacement both go through `doc_id`; listing is a DISTINCT over it.
    op.execute("CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc ON document_chunks (tenant_id, doc_id)")

    op.execute(
        """
        ALTER TABLE document_chunks
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_chunks_content_tsv ON document_chunks USING GIN (content_tsv)"
    )

    # pgvector may be absent on a deployment that never enabled it; the extension is created
    # by 0001_baseline, so this only adds the column. Nullable — see the module docstring.
    op.execute(f"ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding vector({_embedding_dim()})")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
        ON document_chunks USING hnsw (embedding vector_cosine_ops)
        """
    )

    # Same policy shape as every other tenant table (`0006_tenant_rls`). Applied
    # unconditionally so the schema is reproducible; the application declares
    # `app.rls_bypass` when `FELIX_DATABASE_RLS` is off.
    op.execute("ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON document_chunks")
    op.execute(
        """
        CREATE POLICY felix_tenant_isolation ON document_chunks
        USING (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.rls_bypass', true) = 'on'
            OR tenant_id = current_setting('app.tenant_id', true)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS felix_tenant_isolation ON document_chunks")
    op.execute("DROP INDEX IF EXISTS idx_doc_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS idx_doc_chunks_content_tsv")
    op.execute("DROP INDEX IF EXISTS idx_doc_chunks_doc")
    op.execute("DROP TABLE IF EXISTS document_chunks")
