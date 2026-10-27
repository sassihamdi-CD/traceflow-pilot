"""Shared SQL helpers: product detail with computed states + readiness."""
from __future__ import annotations

from app.services.readiness import compute_readiness
from app.services.state import compute_state


async def fetch_field_definitions(conn, category: str) -> list[dict]:
    rows = await conn.execute(
        "SELECT field_key, label, required, display_order FROM field_definitions "
        "WHERE category = %s ORDER BY display_order",
        (category,),
    )
    return [
        {"field_key": r[0], "label": r[1], "required": bool(r[2]), "display_order": r[3]}
        for r in await rows.fetchall()
    ]


async def product_detail(conn, workspace_id: str, product_id: str) -> dict | None:
    cur = await conn.execute(
        "SELECT id, sku, name, category, manufacturer_name, public_slug, "
        "passport_published, published_at FROM products WHERE id = %s AND workspace_id = %s",
        (product_id, workspace_id),
    )
    p = await cur.fetchone()
    if p is None:
        return None
    keys = ["id", "sku", "name", "category", "manufacturer_name", "public_slug",
            "passport_published", "published_at"]
    product = dict(zip(keys, p))

    defs = await fetch_field_definitions(conn, product["category"])
    cur = await conn.execute(
        "SELECT id, field_key, value, unit, status, document_id, location, "
        "extracted_by, reviewed_by, reviewed_at, supersedes_id, created_at "
        "FROM field_values WHERE product_id = %s AND workspace_id = %s ORDER BY created_at",
        (product_id, workspace_id),
    )
    cols = ["id", "field_key", "value", "unit", "status", "document_id", "location",
            "extracted_by", "reviewed_by", "reviewed_at", "supersedes_id", "created_at"]
    values = [dict(zip(cols, r)) for r in await cur.fetchall()]
    for v in values:
        for k in ("id", "document_id", "supersedes_id"):
            if v[k] is not None:
                v[k] = str(v[k])
        if v["reviewed_at"] is not None:
            v["reviewed_at"] = v["reviewed_at"].isoformat()
        v["created_at"] = v["created_at"].isoformat()

    by_field: dict[str, list[str]] = {}
    for v in values:
        by_field.setdefault(v["field_key"], []).append(v["status"])

    fields = []
    counts = {"verified": 0, "proposed": 0, "missing": 0, "conflicting": 0}
    for d in defs:
        comp = compute_state(by_field.get(d["field_key"], []))
        if d["required"]:
            counts[comp["state"]] += 1
        fields.append({**d, **comp,
                       "values": [v for v in values if v["field_key"] == d["field_key"]]})

    total_required = sum(1 for d in defs if d["required"])
    readiness = compute_readiness(counts["verified"], counts["proposed"],
                                  counts["missing"], counts["conflicting"], total_required)
    cur = await conn.execute(
        "SELECT d.id, d.filename, d.storage_key, d.page_count, d.supplier_id, d.uploaded_at,"
        " d.extraction_status, d.extracted_count, d.extraction_error, d.detected_parties,"
        " d.extraction_truncated, s.name,"
        " (SELECT COUNT(*) FROM document_items di WHERE di.document_id = d.id) AS items_count,"
        " d.document_type, d.document_summary, d.document_language, d.mime_type,"
        " d.file_size, d.is_scanned, d.extraction_version, d.extracted_text_chars"
        " FROM documents d LEFT JOIN suppliers s ON s.id = d.supplier_id"
        " WHERE d.product_id = %s AND d.workspace_id = %s ORDER BY d.uploaded_at",
        (product_id, workspace_id),
    )
    docs = []
    for r in await cur.fetchall():
        parties = r[9] if isinstance(r[9], list) else []
        docs.append({"id": str(r[0]), "filename": r[1], "storage_key": r[2],
                     "page_count": r[3], "supplier_id": str(r[4]) if r[4] else None,
                     "supplier_name": r[11],
                     "uploaded_at": r[5].isoformat(), "extraction_status": r[6],
                     "extracted_count": r[7], "extraction_error": r[8],
                     "truncated": bool(r[10]),
                     "items_count": r[12],
                     "detected_parties": parties,
                     "document_type": r[13],
                     "document_summary": r[14],
                     "document_language": r[15],
                     "mime_type": r[16],
                     "file_size": r[17],
                     "is_scanned": bool(r[18]),
                     "extraction_version": r[19],
                     "extracted_text_chars": r[20]})
    return {"product": product, "fields": fields, "counts": counts,
            **readiness, "total_required": total_required, "documents": docs}
