-- 020: universal document ingestion metadata + richer evidence.
--
-- The pilot now accepts PDF, DOCX, XLSX/XLS, CSV/TSV, ODS/ODT, PPTX/ODP,
-- text/markup, email and images. These columns let the reviewer see what the
-- document actually was, whether it was a scan, how much text was read, and
-- which pipeline version produced the proposals. document_items gains the
-- exact evidence quote so every transcription can be audited.
--
-- Idempotent: safe to re-run (IF NOT EXISTS guards).

ALTER TABLE documents ADD COLUMN IF NOT EXISTS mime_type TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_size BIGINT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_sha256 TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_type TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_summary TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS document_language TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS extracted_text_chars INT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS is_scanned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS extraction_version TEXT;

ALTER TABLE document_items ADD COLUMN IF NOT EXISTS evidence TEXT;
ALTER TABLE document_items ADD COLUMN IF NOT EXISTS page_number INT;

ALTER TABLE field_values ADD COLUMN IF NOT EXISTS evidence TEXT;

CREATE INDEX IF NOT EXISTS idx_documents_workspace_uploaded
  ON documents (workspace_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_sha256
  ON documents (workspace_id, content_sha256);
CREATE INDEX IF NOT EXISTS idx_document_items_doc_created
  ON document_items (document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_workspace_status
  ON extraction_jobs (workspace_id, status, created_at);
