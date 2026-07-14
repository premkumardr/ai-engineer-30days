CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    document_id TEXT,
    document_name TEXT,
    page_number INTEGER,
    section_title TEXT,
    content TEXT,
    embedding VECTOR(3072),
    created_at TIMESTAMP DEFAULT NOW()
);
