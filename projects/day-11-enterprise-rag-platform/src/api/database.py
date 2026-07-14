# -*- coding: utf-8 -*-
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
import os
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://rag_user:rag_password@localhost:5433/rag_db"
)

# Will be None if DB is unavailable (local/demo mode)
engine = None

def _create_engine():
    global engine
    try:
        engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            # Connection pool tuning
            pool_size=10,          # keep 10 persistent connections
            max_overflow=20,       # allow 20 extra under burst
            pool_pre_ping=True,    # verify connection is alive before use
            pool_recycle=3600,     # recycle connections every hour
        )
        logger.info("DB engine created with pool_size=10")
    except Exception as e:
        logger.warning(f"Could not create DB engine: {e}. Running in demo mode.")
        engine = None

_create_engine()

async def get_db_connection():
    if engine is None:
        return None
    return engine.begin()

async def init_db():
    """
    Initialize optimised database schema.
    Key design decisions:
    - chunks table (not documents) — each row is one ~512-token passage
    - HNSW index for ANN search (faster query time than IVFFlat, no training needed)
    - GIN index on tsvector for fast full-text keyword fallback
    - Composite index on (tenant_id, document_id) for fast per-doc lookups
    - Separate documents table as a lightweight metadata registry
    """
    if engine is None:
        logger.warning("No DB engine — skipping schema init (demo mode).")
        return
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE EXTENSION IF NOT EXISTS vector;

                -- ── Document registry (metadata only, no content) ──────────────
                CREATE TABLE IF NOT EXISTS documents (
                    id          SERIAL PRIMARY KEY,
                    tenant_id   VARCHAR(255) NOT NULL,
                    document_id VARCHAR(512) NOT NULL,
                    filename    TEXT,
                    total_pages INT DEFAULT 1,
                    total_chunks INT DEFAULT 0,
                    char_count  INT DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (tenant_id, document_id)
                );

                -- ── Chunks (the actual searchable passages) ──────────────────
                CREATE TABLE IF NOT EXISTS chunks (
                    id          BIGSERIAL PRIMARY KEY,
                    tenant_id   VARCHAR(255) NOT NULL,
                    document_id VARCHAR(512) NOT NULL,
                    chunk_index INT          NOT NULL,
                    content     TEXT         NOT NULL,
                    source_page INT          DEFAULT 1,
                    char_start  INT          DEFAULT 0,
                    embedding   vector(3072),
                    ts_content  TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
                    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (tenant_id, document_id, chunk_index)
                );

                -- ── Indexes ───────────────────────────────────────────────────

                -- Fast tenant + doc lookups
                CREATE INDEX IF NOT EXISTS idx_chunks_tenant
                    ON chunks(tenant_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_tenant_doc
                    ON chunks(tenant_id, document_id);

                -- HNSW vector index (no training required, fast incremental inserts)
                -- m=16: neighbours per node (higher = better recall, more memory)
                -- ef_construction=128: build-time quality (higher = better recall, slower build)
                CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
                    ON chunks USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 128);

                -- GIN full-text index (fast keyword / BM25-style search)
                CREATE INDEX IF NOT EXISTS idx_chunks_fts
                    ON chunks USING gin(ts_content);

                -- Document registry indexes
                CREATE INDEX IF NOT EXISTS idx_docs_tenant
                    ON documents(tenant_id);
            """))
        logger.info("Optimised DB schema initialised (HNSW + FTS).")
    except Exception as e:
        logger.warning(f"DB init failed: {e}. Running in demo/local mode.")
