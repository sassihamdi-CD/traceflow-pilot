"""Full per-document transcription (Track A).

The 7-field passport mapping in field_values stays untouched. This module
stores EVERY labeled value the document states (e.g. every chemical
substance + limit + unit + location) in document_items, shown on the
per-document view.

Extraction entry points are failure-tolerant ([] on any error) so the
passport path continues unaffected. Storage is NOT: it runs in a savepoint
and propagates errors, because silently committing half a transcription is
worse than surfacing the failure to the worker.
"""
from __future__ import annotations

import base64

FULL_ITEMS_SYSTEM = (
    "You are the evidence transcriber for TraceFlow AI. "
    "Transcribe EVERY labeled value and table row stated in the supplied PDF "
    "as a JSON array of objects with exactly the keys label, value, unit, location. "
    "Copy labels and values verbatim as printed. "
    "Include a unit ONLY when the source prints one next to the value, else null. "
    "Location must pinpoint the evidence (e.g. 'Page 3'). "
    "Never invent, infer, or complete values. No prose, no markdown."
)

FULL_ITEMS_TASK = (
    "Transcribe EVERY labeled value/table row in this PDF as "
    "{label, value, unit|null, location} JSON array, verbatim, "
    "no invention, cap 200 items. "
    "Return ONLY the JSON array."
)

TABLE_ITEMS_SYSTEM = (
    "You are the evidence transcriber for TraceFlow AI. "
    "Transcribe EVERY labeled value and table row in the supplied parsed spreadsheet "
    "as a JSON array of objects with exactly the keys label, value, unit, location. "
    "Copy labels and values verbatim as printed. "
    "Include a unit ONLY when the source prints one next to the value, else null. "
    "Location must be the given sheet / cell coordinate. "
    "Never invent, infer, or complete values. No prose, no markdown."
)

TABLE_ITEMS_TASK = (
    "Transcribe EVERY labeled value and table row in this parsed spreadsheet as "
    "{label, value, unit|null, location} JSON array. label = the row/column label "
    "as printed, value = the cell value verbatim, location = the given sheet / cell. "
    "Never invent, infer, or complete values. Cap 200 items. Return ONLY the JSON array."
)

MAX_ITEMS = 2000
MAX_LEN = 2000


def parse_items_lenient(text: str) -> list[dict]:
    """Extract every COMPLETE {...} object from the reply, even when the
    model is cut off by max_tokens mid-array.

    Full transcriptions routinely exceed the token budget, so the trailing
    object is truncated and strict json.loads fails on the whole payload.
    This scanner walks top-level braces (string/escape aware) and keeps
    each object that parses, skipping the truncated tail. Never raises.
    """
    try:
        items: list[dict] = []
        depth = 0
        in_str = False
        esc = False
        start = -1
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                if depth == 0:
                    start = i
                depth += 1
            elif ch == '}':
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start != -1:
                        try:
                            import json as _json

                            obj = _json.loads(text[start:i + 1])
                            if isinstance(obj, dict):
                                items.append(obj)
                        except Exception:
                            pass
                        start = -1
        return items
    except Exception:
        return []


async def extract_full_items_pdf(body: bytes) -> list[dict]:
    """Transcribe all labeled values from PDF bytes. Returns [] on any error.

    Uses its own system prompt + large token budget: the shared passport
    helper caps at 2000 tokens with the 7-field contract, which truncates
    full transcriptions mid-array (unparsable -> silently []).
    """
    try:
        from anthropic import AsyncAnthropic

        from app.config import settings
        from app.services.extraction import _create_with_retry, parse_extraction_payload

        if settings.demo_mode:
            from app.services.demo_extraction import full_pdf

            return sanitize_doc_items(full_pdf(body))
        if not settings.anthropic_api_key:
            return []
        b64 = base64.standard_b64encode(body).decode("ascii")
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await _create_with_retry(
            client,
            model=settings.anthropic_model,
            max_tokens=12000,
            system=FULL_ITEMS_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                        },
                        {"type": "text", "text": FULL_ITEMS_TASK},
                    ],
                }
            ],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            return sanitize_doc_items(parse_extraction_payload(text))
        except Exception:
            # Truncated mid-array (max_tokens): salvage complete rows.
            return sanitize_doc_items(parse_items_lenient(text))
    except Exception as e:
        print(f"extract_full_items_pdf fallback: {type(e).__name__}")
        return []


def table_to_doc_items(table: list[dict]) -> list[dict]:
    """Deterministic fallback: every non-empty cell becomes one item.

    Never hallucinates and never loses a cell when the model call fails. The
    label is the coordinate so the reviewer can always trace the value.
    """
    from app.services.extraction import TruncatedList

    items = TruncatedList()
    for cell in table or []:
        if not isinstance(cell, dict) or not cell.get("value"):
            continue
        if len(items) >= MAX_ITEMS:
            items.truncated = True
            break
        sheet = str(cell.get("sheet") or "Table")
        location = f"{sheet} / {cell.get('cell')}"
        items.append({
            "label": location,
            "value": str(cell["value"]),
            "unit": None,
            "location": location,
        })
    return items


async def extract_full_items_table(table: list[dict]) -> list[dict]:
    """Transcribe a parsed XLSX/CSV table into labeled rows.

    XLSX/CSV used to get no full transcription at all (only the 7-field
    mapping), so all other numeric details were invisible. Fall back to the
    deterministic cell-per-item view if the model is unavailable.
    """
    try:
        import json as _json

        from anthropic import AsyncAnthropic

        from app.config import settings
        from app.services.extraction import _create_with_retry, parse_extraction_payload

        if settings.demo_mode or not settings.anthropic_api_key or not table:
            return table_to_doc_items(table)
        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await _create_with_retry(
            client,
            model=settings.anthropic_model,
            max_tokens=12000,
            system=TABLE_ITEMS_SYSTEM,
            messages=[{
                "role": "user",
                "content": [{"type": "text", "text": TABLE_ITEMS_TASK + "\n\n" + _json.dumps(table)}],
            }],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            parsed = sanitize_doc_items(parse_extraction_payload(text))
        except Exception:
            parsed = sanitize_doc_items(parse_items_lenient(text))
        return parsed or table_to_doc_items(table)
    except Exception as e:
        print(f"extract_full_items_table fallback: {type(e).__name__}")
        return table_to_doc_items(table)


def sanitize_doc_items(items: object) -> list[dict]:
    """Keep only well-formed rows: non-empty label+value strings.

    Truncates every string to 500 chars and caps the list at 200 items.
    Returns a TruncatedList: `.truncated` is True when the 200-item cap bit
    off rows OR any string was shortened at 500 chars, so the caller can
    surface the flag on the document row + UI badge.
    Never raises: [] on malformed input.
    """
    from app.services.extraction import TruncatedList

    clean = TruncatedList()
    try:
        if not isinstance(items, list):
            return clean
        for it in items:
            if not isinstance(it, dict):
                continue
            label = it.get("label")
            value = it.get("value")
            if not isinstance(label, str) or not label.strip():
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            if len(clean) >= MAX_ITEMS:
                clean.truncated = True
                break
            unit = it.get("unit")
            location = it.get("location")
            label_s = label.strip()
            value_s = value.strip()
            unit_s = unit.strip() if isinstance(unit, str) and unit.strip() else None
            location_s = location.strip() if isinstance(location, str) and location.strip() else None
            if (len(label_s) > MAX_LEN or len(value_s) > MAX_LEN
                    or (unit_s is not None and len(unit_s) > MAX_LEN)
                    or (location_s is not None and len(location_s) > MAX_LEN)):
                clean.truncated = True
            evidence = it.get("evidence")
            evidence_s = evidence.strip() if isinstance(evidence, str) and evidence.strip() else None
            if evidence_s is not None and len(evidence_s) > MAX_LEN:
                clean.truncated = True
            clean.append({
                "label": label_s[:MAX_LEN],
                "value": value_s[:MAX_LEN],
                "unit": unit_s[:MAX_LEN] if unit_s is not None else None,
                "location": location_s[:MAX_LEN] if location_s is not None else None,
                "evidence": evidence_s[:MAX_LEN] if evidence_s is not None else None,
            })
        return clean
    except Exception:
        return clean


async def store_document_items(conn, workspace_id: str, document_id: str, items: list[dict]) -> list[dict]:
    """Replace this document's transcription atomically.

    Works with async and sync psycopg connections. Deletes the previous rows
    first (idempotent retries), then inserts inside a savepoint so a failure
    cannot commit half a transcription. Errors propagate to the caller.
    """
    import psycopg

    clean = sanitize_doc_items(items)
    if not clean:
        return []

    values = [
        (workspace_id, document_id, it["label"], it["value"],
         it["unit"], it["location"], it.get("evidence"))
        for it in clean
    ]
    insert_sql = (
        "INSERT INTO document_items"
        " (workspace_id, document_id, label, value, unit, location, evidence)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)"
    )

    def _delete_and_insert_sync() -> None:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM document_items WHERE workspace_id = %s AND document_id = %s",
                    (workspace_id, document_id),
                )
                cur.executemany(insert_sql, values)

    if isinstance(conn, psycopg.AsyncConnection):
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM document_items WHERE workspace_id = %s AND document_id = %s",
                (workspace_id, document_id),
            )
            async with conn.cursor() as cur:
                await cur.executemany(insert_sql, values)
    else:
        _delete_and_insert_sync()
    return clean
