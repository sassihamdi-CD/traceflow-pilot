from __future__ import annotations

import json

import nanoid
from fastapi import APIRouter, Depends, Form, UploadFile
from starlette.concurrency import run_in_threadpool

from app import errors

from app.auth import Caller, ensure_workspace_exists, get_caller, require_member, require_reviewer
from app.db import get_pool
from app.schemas import ProductCreate, uuid_or_404
from app.services.detail import fetch_field_definitions, product_detail
from app.services.listing import list_products
from app.services.extraction import (
    csv_to_table,
    extract_parties_pdf,
    extract_pdf_native,
    extract_table_mapped,
    sanitize_items,
    validate_item_locations,
    xlsx_to_table,
)
from app.services.storage import (
    ALLOWED_EXTENSIONS,
    check_size,
    classify_file,
    content_type_for,
    delete_from_storage,
    ext_of,
    head_storage,
    key_belongs_to,
    max_size_mb,
    new_storage_key,
    presign_put,
    read_storage_head,
    storage_configured,
    upload_to_storage,
    verify_file_content,
    ZIP_EXTENSIONS,
)

router = APIRouter()


@router.get("/api/products")
async def get_products(caller: Caller = Depends(require_member)):
    pool = await get_pool()
    async with pool.connection() as conn:
        return await list_products(conn, caller.workspace_id)


@router.post("/api/products")
async def create_product(body: ProductCreate, caller: Caller = Depends(require_reviewer)):
    pool = await get_pool()
    slug = nanoid.generate(size=12)
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        cur = await conn.execute(
            "INSERT INTO products (workspace_id, sku, name, category, manufacturer_name, public_slug)"
            " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (caller.workspace_id, body.sku, body.name, body.category, body.manufacturer_name, slug),
        )
        pid = str((await cur.fetchone())[0])
        await conn.execute(
            "INSERT INTO audit_log (workspace_id, actor, action, entity_type, entity_id, detail)"
            " VALUES (%s,%s,'product.created','product',%s,%s::jsonb)",
            (caller.workspace_id, caller.user_id, pid, json.dumps({"sku": body.sku})),
        )
    return {"id": pid, "public_slug": slug}


@router.patch("/api/products/{product_id}")
async def rename_product(product_id: str, body: dict, caller: Caller = Depends(require_reviewer)):
    """Rename a dossier (name / sku / manufacturer_name). Identity fields only;
    evidence and history are untouched. One audit row, same transaction."""
    uuid_or_404(product_id)
    allowed = {k: (body.get(k) or "").strip() for k in ("name", "sku", "manufacturer_name")}
    allowed = {k: v for k, v in allowed.items() if v}
    if not allowed:
        raise errors.nothing_to_update()
    import json as _json
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            sets = ", ".join(f"{k} = %s" for k in allowed)
            cur = await conn.execute(
                f"UPDATE products SET {sets} WHERE id = %s AND workspace_id = %s",
                (*allowed.values(), product_id, caller.workspace_id),
            )
            if cur.rowcount == 0:
                raise errors.not_found("That item")
            await conn.execute(
                "INSERT INTO audit_log (workspace_id, actor, action, entity_type, entity_id, detail)"
                " VALUES (%s,%s,'product.renamed','product',%s,%s::jsonb)",
                (caller.workspace_id, caller.user_id, product_id, _json.dumps(allowed)),
            )
    return {"ok": True, "updated": allowed}


@router.patch("/api/documents/{document_id}")
async def link_document(document_id: str, body: dict, caller: Caller = Depends(require_reviewer)):
    """Link a document to a supplier (or unlink with supplier_id null).
    Used by the one-click 'detected party → supplier' flow and manual linking."""
    uuid_or_404(document_id, "That document")
    supplier_id = body.get("supplier_id")
    if supplier_id is not None:
        uuid_or_404(str(supplier_id), "That supplier")
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            if supplier_id is not None:
                cur = await conn.execute(
                    "SELECT id FROM suppliers WHERE id = %s AND workspace_id = %s",
                    (supplier_id, caller.workspace_id),
                )
                if await cur.fetchone() is None:
                    raise errors.not_found("That supplier")
            cur = await conn.execute(
                "UPDATE documents SET supplier_id = %s WHERE id = %s AND workspace_id = %s",
                (supplier_id, document_id, caller.workspace_id),
            )
            if cur.rowcount == 0:
                raise errors.not_found("That document")
            await conn.execute(
                "INSERT INTO audit_log (workspace_id, actor, action, entity_type, entity_id)"
                " VALUES (%s,%s,'document.linked','document',%s)",
                (caller.workspace_id, caller.user_id, document_id),
            )
    return {"ok": True, "supplier_id": supplier_id}


@router.get("/api/products/{product_id}")
async def get_product(product_id: str, caller: Caller = Depends(require_member)):
    uuid_or_404(product_id)
    pool = await get_pool()
    async with pool.connection() as conn:
        detail = await product_detail(conn, caller.workspace_id, product_id)
    if detail is None:
        raise errors.not_found("That item")
    return detail


async def _run_extraction(document_id: str, product_id: str, ext: str, body: bytes, workspace_id: str):
    """Legacy in-process extraction (kept for tests/backfill only).

    NOT on the upload path anymore: uploads enqueue one row in
    extraction_jobs and the standalone worker (scripts/worker.py) is the
    SOLE extractor, so each document yields exactly one set of
    field_values. Calling this alongside the worker would double-insert.

    Always records the outcome on the document row (done + count, or failed +
    error message) so the reviewer can tell "empty but processed" apart from
    "processing failed". Failure still yields zero rows, never partial data.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async def mark(status: str, count: int = 0, error: str | None = None, truncated: bool = False):
            await conn.execute(
                "UPDATE documents SET extraction_status = %s, extracted_count = %s,"
                " extraction_error = %s, extraction_truncated = %s WHERE id = %s",
                (status, count, error, truncated, document_id),
            )

        cur = await conn.execute(
            "SELECT category FROM products WHERE id = %s AND workspace_id = %s",
            (product_id, workspace_id),
        )
        row = await cur.fetchone()
        if row is None:
            await mark("failed", 0, "product gone")
            return
        defs = await fetch_field_definitions(conn, row[0])
        table = None
        try:
            if ext == ".pdf":
                items = await extract_pdf_native(body, defs)
            elif ext == ".xlsx":
                table = xlsx_to_table(body)
                items = await extract_table_mapped(table, defs)
            else:  # .csv
                table = csv_to_table(body)
                items = await extract_table_mapped(table, defs)
        except Exception as e:
            await mark("failed", 0, f"{type(e).__name__}: {e}"[:500])
            return
        allowed = {d["field_key"] for d in defs}
        clean = validate_item_locations(sanitize_items(items, allowed), ext, table)
        for it in clean:
            await conn.execute(
                "INSERT INTO field_values (workspace_id, product_id, field_key, value, unit,"
                " status, document_id, location, extracted_by)"
                " VALUES (%s,%s,%s,%s,%s,'proposed',%s,%s,'ai_system')",
                (workspace_id, product_id, it["field_key"], it["value"],
                 it["unit"], document_id, it["location"]),
            )
        truncated = bool(getattr(table, "truncated", False))
        await mark("done", len(clean), truncated=truncated)
        # Track A: full per-document transcription (every labeled value).
        # Passport mapping above stays untouched. PDF and tables both get a
        # full transcription; a storage failure is logged without killing the
        # already-extracted passport values.
        try:
            from app.services.document_items import (
                extract_full_items_pdf,
                extract_full_items_table,
                store_document_items,
            )
            if ext == ".pdf":
                full = await extract_full_items_pdf(body)
            elif table is not None:
                full = await extract_full_items_table(table)
            else:
                full = []
            if full:
                stored = await store_document_items(conn, workspace_id, document_id, full)
                if getattr(stored, "truncated", False):
                    truncated = True
                    await mark("done", len(clean), truncated=True)
        except Exception as e:
            print(f"document_items fallback: {type(e).__name__}: {e}")
        # Who is on this document? Stored for one-click supplier linking.
        # Failure-tolerant by design: [] when detection fails.
        if ext == ".pdf":
            parties = await extract_parties_pdf(body)
            if getattr(parties, "truncated", False) and not truncated:
                truncated = True
                await mark("done", len(clean), truncated=True)
            if parties:
                import json as _json
                await conn.execute(
                    "UPDATE documents SET detected_parties = %s::jsonb WHERE id = %s",
                    (_json.dumps(parties), document_id),
                )


@router.post("/api/products/{product_id}/documents")
async def upload_document(
    product_id: str,
    file: UploadFile,
    supplier_id: str | None = Form(None),
    caller: Caller = Depends(require_reviewer),
):
    """Store the file + enqueue ONE extraction job. Single-path extraction:
    the worker (scripts/worker.py) is the sole extractor — no BackgroundTasks
    here, so a document can never yield two sets of field_values."""
    uuid_or_404(product_id, "That product")
    # Normalize empty-string supplier_id from HTML forms ("") to None
    # so the FK insert can't 500 on a zero-length id.
    if supplier_id is not None and not str(supplier_id).strip():
        supplier_id = None
    if supplier_id is not None:
        uuid_or_404(supplier_id, "That supplier")
    body = await file.read()
    filename = file.filename or ""
    ext = ext_of(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise errors.unsupported_type(filename or "that file")
    if len(body) > 100 * 1024 * 1024:
        raise errors.file_too_large(100)
    check_size(ext, len(body))  # 413 per-type caps, before any storage/DB work
    ext = classify_file(filename, body[:8])  # 415 here on type/content mismatch
    verify_file_content(filename, ext, body)  # 415 deep content check
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        cur = await conn.execute(
            "SELECT id FROM products WHERE id = %s AND workspace_id = %s",
            (product_id, caller.workspace_id),
        )
        if await cur.fetchone() is None:
            raise errors.not_found("That product")
        if supplier_id:
            cur = await conn.execute(
                "SELECT id FROM suppliers WHERE id = %s AND workspace_id = %s",
                (supplier_id, caller.workspace_id),
            )
            if await cur.fetchone() is None:
                raise errors.not_found("That supplier")
        key = new_storage_key(caller.workspace_id, product_id, file.filename or "upload")
        try:
            # boto3 is synchronous; never block the event loop on R2 I/O.
            await run_in_threadpool(upload_to_storage, key, body, content_type_for(ext))
        except Exception as exc:  # noqa: BLE001
            print(f"storage upload failed: {type(exc).__name__}: {exc}")
            raise errors.storage_unavailable() from exc
        try:
            cur = await conn.execute(
                "INSERT INTO documents (workspace_id, product_id, supplier_id, filename, storage_key,"
                " extraction_status, extracted_count, mime_type, file_size)"
                " VALUES (%s,%s,%s,%s,%s,'extracting',0,%s,%s) RETURNING id",
                (caller.workspace_id, product_id, supplier_id, file.filename, key,
                 content_type_for(ext), len(body)),
            )
            doc_id = str((await cur.fetchone())[0])
            # If the job cannot be queued the document must not be left in
            # 'extracting' forever: fail the request (the transaction rolls back).
            try:
                await conn.execute(
                    "INSERT INTO extraction_jobs (workspace_id, document_id, status)"
                    " VALUES (%s,%s,'queued')",
                    (caller.workspace_id, doc_id),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"extraction_jobs insert failed: {type(exc).__name__}: {exc}")
                raise errors.queue_unavailable() from exc
            await conn.execute(
                "INSERT INTO audit_log (workspace_id, actor, action, entity_type, entity_id, detail)"
                " VALUES (%s,%s,'document.uploaded','document',%s,%s::jsonb)",
                (caller.workspace_id, caller.user_id, doc_id, json.dumps({"filename": file.filename or ""})),
            )
        except Exception:
            # The connection context rolls back; remove the object that would
            # otherwise be orphaned because no documents row points at it.
            try:
                await run_in_threadpool(delete_from_storage, key)
            except Exception as cleanup_exc:  # noqa: BLE001
                print(f"storage cleanup failed: {type(cleanup_exc).__name__}: {cleanup_exc}")
            raise
    return {"document_id": doc_id, "status": "extracting"}


@router.post("/api/products/{product_id}/documents/presign")
async def presign_document(product_id: str, body: dict, caller: Caller = Depends(require_reviewer)):
    """Step 1 of large-file upload: return a direct-to-R2 PUT URL.

    The browser uploads the bytes straight to Cloudflare R2, bypassing
    Vercel's ~4.5 MB request-body limit. The object is only registered as a
    document after ``/documents/complete`` verifies it exists.
    """
    uuid_or_404(product_id, "That product")
    filename = str(body.get("filename") or "").strip()
    size = body.get("size")
    ext = ext_of(filename)
    if not filename or not isinstance(size, int) or size <= 0:
        raise errors.missing_field("the file name and size")
    # Validate type + per-type cap before issuing a signed URL.
    if ext not in ALLOWED_EXTENSIONS:
        raise errors.unsupported_type(filename)
    if ext == ".doc":
        raise errors.file_content_problem("legacy_doc")
    check_size(ext, size)
    if not storage_configured():
        raise errors.presign_unavailable()

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM products WHERE id = %s AND workspace_id = %s",
            (product_id, caller.workspace_id),
        )
        if await cur.fetchone() is None:
            raise errors.not_found("That product")
    key = new_storage_key(caller.workspace_id, product_id, filename)
    content_type = content_type_for(ext)
    try:
        url = await run_in_threadpool(presign_put, key, content_type)
    except Exception as exc:  # noqa: BLE001
        print(f"presign failed: {type(exc).__name__}: {exc}")
        raise errors.storage_unavailable() from exc
    return {
        "upload_url": url,
        "storage_key": key,
        "content_type": content_type,
        "max_size_mb": max_size_mb(ext),
    }


@router.post("/api/products/{product_id}/documents/complete")
async def complete_document(product_id: str, body: dict, caller: Caller = Depends(require_reviewer)):
    """Step 2 of large-file upload: verify the R2 object and enqueue it.

    The key must live under this workspace/product prefix, the object must
    exist, and its real size must fit the type cap. A failed DB write deletes
    the object so no orphan is left behind.
    """
    uuid_or_404(product_id, "That product")
    filename = str(body.get("filename") or "").strip()
    storage_key = str(body.get("storage_key") or "").strip()
    supplier_id = body.get("supplier_id")
    if supplier_id is not None and not str(supplier_id).strip():
        supplier_id = None
    ext = ext_of(filename)
    if not filename or not storage_key:
        raise errors.missing_field("the file name and storage key")
    if not key_belongs_to(storage_key, caller.workspace_id, product_id):
        raise errors.not_found("That upload")
    if ext == ".doc":
        try:
            await run_in_threadpool(delete_from_storage, storage_key)
        except Exception:
            pass
        raise errors.file_content_problem("legacy_doc")
    if supplier_id is not None:
        uuid_or_404(str(supplier_id), "That supplier")

    try:
        head = await run_in_threadpool(head_storage, storage_key)
        actual_size = int(head.get("ContentLength") or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"upload verify failed: {type(exc).__name__}: {exc}")
        raise errors.upload_incomplete() from exc
    check_size(ext, actual_size)
    try:
        head_bytes = await run_in_threadpool(read_storage_head, storage_key, 65536)
        classify_file(filename, head_bytes)
        # Zip containers need the central directory (at the end), so their full
        # validation happens in the worker parser. Everything else can be
        # checked from the first 64 KiB.
        if ext not in ZIP_EXTENSIONS:
            verify_file_content(filename, ext, head_bytes)
    except Exception as exc:  # noqa: BLE001
        # Remove an invalid object rather than leaving it orphaned.
        try:
            await run_in_threadpool(delete_from_storage, storage_key)
        except Exception:
            pass
        if isinstance(exc, Exception) and hasattr(exc, "status_code"):
            raise
        raise errors.file_content_problem("corrupt_container") from exc

    pool = await get_pool()
    doc_id = None
    try:
        async with pool.connection() as conn:
            await ensure_workspace_exists(conn, caller.workspace_id)
            cur = await conn.execute(
                "SELECT id FROM products WHERE id = %s AND workspace_id = %s",
                (product_id, caller.workspace_id),
            )
            if await cur.fetchone() is None:
                raise errors.not_found("That product")
            if supplier_id:
                cur = await conn.execute(
                    "SELECT id FROM suppliers WHERE id = %s AND workspace_id = %s",
                    (supplier_id, caller.workspace_id),
                )
                if await cur.fetchone() is None:
                    raise errors.not_found("That supplier")
            cur = await conn.execute(
                "INSERT INTO documents (workspace_id, product_id, supplier_id, filename, storage_key,"
                " extraction_status, extracted_count, mime_type, file_size)"
                " VALUES (%s,%s,%s,%s,%s,'extracting',0,%s,%s) RETURNING id",
                (caller.workspace_id, product_id, supplier_id, filename, storage_key,
                 content_type_for(ext), actual_size),
            )
            doc_id = str((await cur.fetchone())[0])
            await conn.execute(
                "INSERT INTO extraction_jobs (workspace_id, document_id, status)"
                " VALUES (%s,%s,'queued')",
                (caller.workspace_id, doc_id),
            )
            await conn.execute(
                "INSERT INTO audit_log (workspace_id, actor, action, entity_type, entity_id, detail)"
                " VALUES (%s,%s,'document.uploaded','document',%s,%s::jsonb)",
                (caller.workspace_id, caller.user_id, doc_id,
                 json.dumps({"filename": filename, "size": actual_size, "direct": True})),
            )
    except Exception:
        try:
            await run_in_threadpool(delete_from_storage, storage_key)
        except Exception:
            pass
        raise
    return {"document_id": doc_id, "status": "extracting"}
