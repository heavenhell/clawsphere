CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS ops_knowledge (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'global',
    doc_type TEXT NOT NULL,
    tier INT NOT NULL DEFAULT 2,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(128) NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    permission TEXT NOT NULL DEFAULT 'public',
    version INT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_tenant_type_permission
    ON ops_knowledge(tenant_id, doc_type, permission, tier);
CREATE INDEX IF NOT EXISTS idx_knowledge_embedding
    ON ops_knowledge USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_knowledge_fulltext
    ON ops_knowledge USING gin(to_tsvector('simple', content));

ALTER TABLE ops_knowledge ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON ops_knowledge;
CREATE POLICY tenant_isolation ON ops_knowledge
    USING (tenant_id IN ('global', current_setting('app.tenant_id', true)));
