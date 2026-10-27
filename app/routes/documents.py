"""Per-document reads: full transcribed values for one document.

Read-only, workspace-scoped. The passport mapping (field_values) is untouched;
this surface exposes document_items written by the Track A full extraction.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app import errors

from app.auth import Caller, ensure_workspace_exists, require_member, require_reviewer
from app.db import get_pool
from app.schemas import uuid_or_404

router = APIRouter()


async def _fetch_document(conn, workspace_id: str, document_id: str) -> dict | None:
    cur = await conn.execute(
        "SELECT d.id, d.product_id, d.filename, d.extraction_status,"
        " d.extracted_count, d.extraction_error, d.uploaded_at, d.extraction_truncated,"
        " d.document_type, d.document_summary, d.document_language, d.mime_type, d.file_size,"
        " d.is_scanned, d.extracted_text_chars, d.extraction_version"
        " FROM documents d WHERE d.id = %s AND d.workspace_id = %s",
        (document_id, workspace_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "product_id": str(row[1]),
        "filename": row[2],
        "extraction_status": row[3],
        "extracted_count": row[4],
        "extraction_error": row[5],
        "uploaded_at": row[6].isoformat() if row[6] is not None else None,
        "truncated": bool(row[7]) if len(row) > 7 else False,
        "document_type": row[8] if len(row) > 8 else None,
        "document_summary": row[9] if len(row) > 9 else None,
        "document_language": row[10] if len(row) > 10 else None,
        "mime_type": row[11] if len(row) > 11 else None,
        "file_size": row[12] if len(row) > 12 else None,
        "is_scanned": bool(row[13]) if len(row) > 13 else False,
        "extracted_text_chars": row[14] if len(row) > 14 else None,
        "extraction_version": row[15] if len(row) > 15 else None,
    }


async def _fetch_items(conn, workspace_id: str, document_id: str) -> list[dict]:
    cur = await conn.execute(
        "SELECT id, label, value, unit, location, created_at, evidence FROM document_items"
        " WHERE workspace_id = %s AND document_id = %s ORDER BY created_at",
        (workspace_id, document_id),
    )
    items = []
    for r in await cur.fetchall():
        items.append({
            "id": str(r[0]),
            "label": r[1],
            "value": r[2],
            "unit": r[3],
            "location": r[4],
            "created_at": r[5].isoformat() if r[5] is not None else None,
            "evidence": r[6] if len(r) > 6 else None,
        })
    return items


@router.get("/api/documents/{document_id}")
async def get_document(document_id: str, caller: Caller = Depends(require_member)):
    """Document detail: doc row + all transcribed items. 404 outside workspace."""
    uuid_or_404(document_id, "That document")
    pool = await get_pool()
    async with pool.connection() as conn:
        doc = await _fetch_document(conn, caller.workspace_id, document_id)
        if doc is None:
            raise errors.not_found("That document")
        doc["items"] = await _fetch_items(conn, caller.workspace_id, document_id)
    return doc


@router.get("/api/documents/{document_id}/items")
async def list_document_items(document_id: str, caller: Caller = Depends(require_member)):
    """All transcribed items for one document. 404 outside workspace."""
    uuid_or_404(document_id, "That document")
    pool = await get_pool()
    async with pool.connection() as conn:
        doc = await _fetch_document(conn, caller.workspace_id, document_id)
        if doc is None:
            raise errors.not_found("That document")
        items = await _fetch_items(conn, caller.workspace_id, document_id)
    return {"document_id": document_id, "count": len(items), "items": items}


@router.post("/api/documents/{document_id}/retry")
async def retry_document(document_id: str, caller: Caller = Depends(require_reviewer)):
    """Re-queue a failed extraction. Keeps verified evidence intact and
    discards only proposed values/transcriptions from the failed attempt."""
    uuid_or_404(document_id, "That document")
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT extraction_status FROM documents"
                " WHERE id = %s AND workspace_id = %s FOR UPDATE",
                (document_id, caller.workspace_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise errors.not_found("That document")
            if row[0] != "failed":
                raise errors.err(
                    409,
                    "not_retryable",
                    "Only a failed extraction can be retried.",
                    "Refresh the page to see the document's current state.",
                )
            await conn.execute(
                "DELETE FROM document_items WHERE document_id = %s AND workspace_id = %s",
                (document_id, caller.workspace_id),
            )
            await conn.execute(
                "DELETE FROM field_values WHERE document_id = %s AND workspace_id = %s"
                " AND status = 'proposed'",
                (document_id, caller.workspace_id),
            )
            await conn.execute(
                "UPDATE documents SET extraction_status = 'pending', extracted_count = 0,"
                " extraction_error = NULL, extraction_truncated = FALSE WHERE id = %s",
                (document_id,),
            )
            await conn.execute(
                "INSERT INTO extraction_jobs (workspace_id, document_id, status)"
                " VALUES (%s,%s,'queued')",
                (caller.workspace_id, document_id),
            )
            await conn.execute(
                "INSERT INTO audit_log (workspace_id, actor, action, entity_type, entity_id)"
                " VALUES (%s,%s,'document.retried','document',%s)",
                (caller.workspace_id, caller.user_id, document_id),
            )
    return {"ok": True, "status": "pending"}
