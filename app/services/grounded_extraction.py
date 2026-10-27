"""Grounded, chunked document extraction.

Why this module exists
----------------------
The original pipeline sent the document (or a parsed table) straight to a
prompt that said "extract these seven footwear fields". When a new document
did not contain those fields, the model still had a schema to fill — the
classic recipe for a plausible hallucination. It also had a hard 200-item /
12k-token ceiling and no chunking, so large documents were silently cut short.

This pipeline is deliberately two-phase:

1. TRANSCRIBE — read the document and return every labelled value/table row
   with an exact location and an ``evidence`` quote. No field schema is
   mentioned, so there is nothing to force.
2. MAP — receive only the transcribed items and select which item (by id)
   represents each requested field. A field can only be filled by an item
   that actually exists; every value is traceable.

Additional guarantees:
- Text documents are grounded against the parsed source text: an item whose
  value/evidence does not occur in the source is dropped.
- Large documents are split into chunks (pages/characters/table cells) and
  processed with bounded concurrency.
- Vision documents (scans, photos) are split into small PDF slices/images.
- Demo mode uses deterministic text heuristics and never calls the network.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field

from app.config import settings
from app.services.document_ingest import (
    ParsedDocument,
    convert_image_for_vision,
    split_pdf_for_vision,
)
from app.services.extraction import (
    PARTIES_CAP,
    _create_with_retry,
    cap_parties,
    table_locations,
)
from app.services.extraction_prompts import FIELD_GUIDE_FOOTWEAR

# Bounded work per document. These are high enough for manufacturer packs
# (hundreds of pages) while keeping one document from monopolising a worker.
CHUNK_CHARS = 24_000
MAX_CHUNKS = 80
LIGHT_CHUNKS = 1
MAX_ITEMS_PER_CHUNK = 300
MAX_ITEMS_TOTAL = 8_000
MAP_CHUNK_ITEMS = 300
VISION_PDF_PAGES = 12
MAX_VISION_CHUNKS = 12
CONCURRENCY = 4
MAX_TRANSCRIBE_TOKENS = 10_000
MAX_MAP_TOKENS = 4_000

FOOTWEAR_KEYS = {
    "product_name", "sku", "upper_material", "upper_composition",
    "leather_origin", "sole_material", "reach_compliance",
}


class GroundedExtractionError(Exception):
    """The model could not be reached, or every chunk failed."""


@dataclass
class GroundedResult:
    items: list[dict] = field(default_factory=list)
    fields: list[dict] = field(default_factory=list)
    document_type: str = ""
    language: str = ""
    summary: str = ""
    parties: list[dict] = field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    vision: bool = False


# ---------------------------------------------------------------------------
# Prompt contracts
# ---------------------------------------------------------------------------
TRANSCRIBE_SYSTEM = """You are a document transcription engine for TraceFlow AI,
an evidence system for product-compliance documents. You do NOT answer questions
and you do NOT extract a fixed schema. You transcribe what the document says.

Return STRICT JSON only:
{
  "document_type": "short label, e.g. supplier declaration, test report, BOM, invoice, certificate, email, spreadsheet, unknown",
  "language": "ISO 639-1 code when obvious, else unknown",
  "summary": "1-3 sentences describing what this document is and what it contains",
  "items": [
    {"label": "the label/heading exactly as printed",
     "value": "the value exactly as printed",
     "unit": "unit exactly as printed, or null",
     "location": "Page N / Slide N / Paragraph N / sheet + cell / Image 1",
     "evidence": "a short EXACT quote from the document containing the value"}
  ]
}

HARD RULES:
1. Transcribe ONLY what is visible in the source. Never guess, infer, complete,
   translate, modernise, or normalise a value.
2. Every item MUST include an evidence quote copied from the source. If you
   cannot quote it, do not return it.
3. If the document is unreadable, blank, or has no labelled values, return
   "items": [] and explain why in "summary".
4. A table row may become one item per labelled value. Do not drop rows just
   because they do not look like product passports.
5. Do not output prose or markdown. JSON only.
"""

TRANSCRIBE_TEXT_TASK = """Transcribe every labelled value and table row in the
source below. The bracketed labels (e.g. [Page 3], [Paragraph 12]) are the
location strings to cite. Return the JSON object described in the system
prompt. Do not invent values. Cap 300 items for this chunk.

SOURCE:
{source}
"""

TRANSCRIBE_PDF_TASK = """Transcribe every labelled value and table row in this
PDF. Cite the printed page reference (e.g. "Page 3"). Return the JSON object
described in the system prompt. Do not invent values. Cap 300 items.
"""

TRANSCRIBE_IMAGE_TASK = """Transcribe every labelled value and table row in this
image. Cite "Image 1". Return the JSON object described in the system prompt.
Do not invent values. If the image is unreadable, return items: [].
"""

FIELD_MAP_SYSTEM = """You are a strict field mapper for TraceFlow AI.

You receive:
- REQUESTED FIELDS: field keys, human labels, and (when available) semantics.
- TRANSCRIBED ITEMS: values already extracted from ONE document, each with an
  integer id, label, value, unit and location.

Your job: for each requested field, choose the ONE item that explicitly states
that field. You may only use the provided items. You may NOT add knowledge,
infer, translate, reformat, or invent a value.

CRITICAL RULES:
1. If no item clearly and explicitly matches a field, OMIT that field. Returning
   an empty list is correct and expected for documents that do not contain the
   requested information.
2. Never force a value into a field. A manufacturer/supplier name is NOT a
   product name. A generic "country of origin" is NOT leather origin. A REACH
   mention inside a reference standard is NOT product compliance.
3. Copy value, unit and location from the chosen item. Do not rewrite them.
4. Use the item id you selected. One item per field; if several occurrences
   exist, return the clearest one.
5. Output STRICT JSON only: {"fields": [{"field_key": "...", "id": 12,
   "value": "...", "unit": null, "location": "Page 3"}]}. No prose.
"""


def _field_guide(field_defs: list[dict]) -> str:
    keys = {d.get("field_key") for d in field_defs}
    if keys == FOOTWEAR_KEYS:
        return FIELD_GUIDE_FOOTWEAR
    lines = ["Requested fields (extract ONLY these; omit anything not explicitly stated):"]
    for d in field_defs:
        label = d.get("label") or d.get("field_key")
        lines.append(f"- {d['field_key']}: {label}. Use the value exactly as printed.")
    return "\n".join(lines)


def _norm(text: object) -> str:
    """Case/punctuation-insensitive form used for grounding checks."""
    value = str(text or "").casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _json_payload(text: str) -> dict:
    """Parse a JSON object (or an array, wrapped as {"items": [...]})."""
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = "\n".join(
            ln for ln in candidate.splitlines() if not ln.strip().startswith("```")
        ).strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(candidate[start:end + 1])
            except json.JSONDecodeError as exc:
                raise GroundedExtractionError(f"model returned malformed JSON: {exc}") from exc
        else:
            start, end = candidate.find("["), candidate.rfind("]")
            if start == -1 or end <= start:
                raise GroundedExtractionError("model returned no JSON payload")
            try:
                data = json.loads(candidate[start:end + 1])
            except json.JSONDecodeError as exc:
                raise GroundedExtractionError(f"model returned malformed JSON: {exc}") from exc
    if isinstance(data, list):
        return {"items": data}
    if not isinstance(data, dict):
        raise GroundedExtractionError("model payload is not an object")
    return data


def _sanitize_items(raw: object, location_hint: str = "") -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for it in raw:
        if not isinstance(it, dict):
            continue
        label = it.get("label")
        value = it.get("value")
        if not isinstance(label, str) or not label.strip():
            label = it.get("field") or it.get("name") or "Value"
        if not isinstance(value, str) or not value.strip():
            continue
        unit = it.get("unit")
        location = it.get("location") or location_hint
        evidence = it.get("evidence") or it.get("quote") or ""
        row = {
            "label": str(label).strip()[:300],
            "value": value.strip()[:2000],
            "unit": unit.strip()[:50] if isinstance(unit, str) and unit.strip() else None,
            "location": str(location).strip()[:200] if location else None,
            "evidence": str(evidence).strip()[:600] if evidence else None,
        }
        key = (_norm(row["label"]), _norm(row["value"]), _norm(row["location"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
        if len(out) >= MAX_ITEMS_PER_CHUNK:
            break
    return out


def _ground_items(items: list[dict], source_text: str) -> list[dict]:
    """Drop items that cannot be found in the parsed source text.

    Vision documents have no text to check against, so they pass through (the
    prompt still requires an evidence quote). For text documents, this is the
    hard stop against invented values.
    """
    source = _norm(source_text)
    if not source:
        return items
    kept: list[dict] = []
    for it in items:
        value = _norm(it.get("value"))
        evidence = _norm(it.get("evidence"))
        if not value:
            continue
        if value in source:
            kept.append(it)
        elif evidence and evidence in source:
            # Value formatting may differ (80% vs "80 %"), but the quote is real.
            kept.append(it)
    return kept


def _chunk_pages(pages, max_chars: int = CHUNK_CHARS) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    size = 0
    first_label = ""
    for page in pages:
        text = page.text or ""
        if not text.strip():
            continue
        block = f"[{page.label}]\n{text}\n"
        if buf and size + len(block) > max_chars:
            chunks.append((first_label, "\n".join(buf)))
            buf, size, first_label = [], 0, ""
        if not buf:
            first_label = page.label
        buf.append(block)
        size += len(block)
        if len(chunks) >= MAX_CHUNKS:
            break
    if buf:
        chunks.append((first_label, "\n".join(buf)))
    return chunks[:MAX_CHUNKS]


def _chunk_items(items: list[dict], per_chunk: int = MAP_CHUNK_ITEMS) -> list[list[dict]]:
    return [items[i:i + per_chunk] for i in range(0, len(items), per_chunk)]


def _chunk_table(table: list[dict], per_chunk: int = 800) -> list[list[dict]]:
    return [table[i:i + per_chunk] for i in range(0, len(table), per_chunk)]


def _client():
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _call_json(system: str, content, max_tokens: int) -> dict:
    client = _client()
    resp = await _create_with_retry(
        client,
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _json_payload(text)


# ---------------------------------------------------------------------------
# Phase 1 — transcription
# ---------------------------------------------------------------------------
async def _transcribe_text_chunk(label: str, source: str) -> dict:
    return await _call_json(
        TRANSCRIBE_SYSTEM,
        [{"type": "text", "text": TRANSCRIBE_TEXT_TASK.format(source=source)}],
        MAX_TRANSCRIBE_TOKENS,
    )


async def _transcribe_pdf_chunk(pdf_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    return await _call_json(
        TRANSCRIBE_SYSTEM,
        [
            {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": TRANSCRIBE_PDF_TASK},
        ],
        MAX_TRANSCRIBE_TOKENS,
    )


async def _transcribe_image(image_bytes: bytes, media_type: str) -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    return await _call_json(
        TRANSCRIBE_SYSTEM,
        [
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": TRANSCRIBE_IMAGE_TASK},
        ],
        MAX_TRANSCRIBE_TOKENS,
    )


async def _run_chunks(calls: list, light: bool) -> tuple[list[dict], dict, list[str]]:
    """Run chunk coroutines with bounded concurrency; tolerate partial failure."""
    if light and calls:
        calls = calls[:LIGHT_CHUNKS]
    semaphore = asyncio.Semaphore(CONCURRENCY)
    warnings: list[str] = []
    meta: dict = {}
    items: list[dict] = []

    async def one(call):
        async with semaphore:
            try:
                return await call()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"chunk failed: {type(exc).__name__}: {exc}"[:300])
                return None

    results = await asyncio.gather(*(one(c) for c in calls))
    for data in results:
        if not isinstance(data, dict):
            continue
        if not meta:
            for key in ("document_type", "language", "summary"):
                if data.get(key):
                    meta[key] = str(data.get(key))[:2000 if key == "summary" else 120]
        items.extend(_sanitize_items(data.get("items")))
        if len(items) >= MAX_ITEMS_TOTAL:
            break
    if calls and not any(isinstance(r, dict) for r in results):
        raise GroundedExtractionError(
            warnings[0] if warnings else "the extraction model returned no result"
        )
    return items[:MAX_ITEMS_TOTAL], meta, warnings


async def _transcribe_document(parsed: ParsedDocument, light: bool) -> tuple[list[dict], dict, list[str]]:
    if parsed.kind == "table":
        from app.services.document_items import table_to_doc_items

        raw_items = table_to_doc_items(parsed.tables)
        return list(raw_items), {
            "document_type": "spreadsheet",
            "summary": "Parsed spreadsheet.",
            "_items_truncated": bool(getattr(raw_items, "truncated", False)),
        }, []

    if parsed.needs_vision:
        if parsed.kind == "image":
            body, mime = convert_image_for_vision(parsed.body, parsed.mime)
            calls = [lambda b=body, m=mime: _transcribe_image(b, m)]
            items, meta, warnings = await _run_chunks(calls, light)
        else:
            slices = split_pdf_for_vision(parsed.body, VISION_PDF_PAGES)[:MAX_VISION_CHUNKS]
            calls = [lambda b=chunk: _transcribe_pdf_chunk(b) for _label, chunk in slices]
            items, meta, warnings = await _run_chunks(calls, light)
        # No source text exists for scans; the evidence quote is the only pin.
        return items, meta, warnings

    chunks = _chunk_pages(parsed.pages)
    if not chunks and parsed.body and parsed.kind == "pdf":
        # Text extraction found nothing but the PDF is readable: treat as scan.
        slices = split_pdf_for_vision(parsed.body, VISION_PDF_PAGES)[:MAX_VISION_CHUNKS]
        calls = [lambda b=chunk: _transcribe_pdf_chunk(b) for _label, chunk in slices]
        items, meta, warnings = await _run_chunks(calls, light)
        return items, meta, warnings
    calls = [lambda label=label, source=source: _transcribe_text_chunk(label, source)
             for label, source in chunks]
    items, meta, warnings = await _run_chunks(calls, light)
    grounded = _ground_items(items, parsed.source_text)
    return grounded, meta, warnings


# ---------------------------------------------------------------------------
# Phase 2 — field mapping (items -> fields)
# ---------------------------------------------------------------------------
def _find_item(item_id: object, value: str, items: list[dict]) -> dict | None:
    if isinstance(item_id, int):
        for it in items:
            if it.get("_id") == item_id:
                return it
    if isinstance(item_id, str) and item_id.isdigit():
        for it in items:
            if it.get("_id") == int(item_id):
                return it
    target = _norm(value)
    if not target:
        return None
    for it in items:
        candidate = _norm(it.get("value"))
        if candidate == target or (len(target) >= 4 and target in candidate):
            return it
    return None


def _sanitize_fields(raw: object, items: list[dict], allowed: set[str]) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("fields", [])
    if not isinstance(raw, list):
        return []
    clean: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for it in raw:
        if not isinstance(it, dict):
            continue
        key = it.get("field_key")
        value = it.get("value")
        if key not in allowed or not isinstance(value, str) or not value.strip():
            continue
        item = _find_item(it.get("id"), value, items)
        if item is None:
            # The model tried to assign a value that is not in the transcription.
            continue
        row = {
            "field_key": key,
            "value": item["value"],
            "unit": item.get("unit") if isinstance(item.get("unit"), str) else it.get("unit"),
            "location": item.get("location"),
        }
        dedupe = (key, _norm(row["value"]))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        clean.append(row)
        if len(clean) >= 100:
            break
    return clean


async def _map_items_to_fields(
    items: list[dict], field_defs: list[dict], light: bool
) -> tuple[list[dict], list[str]]:
    if not field_defs or not items:
        return [], []
    allowed = {d["field_key"] for d in field_defs}
    guide = _field_guide(field_defs)
    warnings: list[str] = []
    found: list[dict] = []

    numbered: list[dict] = []
    for idx, it in enumerate(items[:MAX_ITEMS_TOTAL], start=1):
        numbered.append({
            "_id": idx,
            "label": it.get("label"),
            "value": it.get("value"),
            "unit": it.get("unit"),
            "location": it.get("location"),
        })

    chunks = _chunk_items(numbered)
    if light and chunks:
        chunks = chunks[:LIGHT_CHUNKS]

    async def map_chunk(chunk: list[dict]):
        payload = {
            "requested_fields": [
                {"field_key": d["field_key"], "label": d.get("label")} for d in field_defs
            ],
            "items": chunk,
        }
        content = [
            {"type": "text", "text": guide + "\n\n" + json.dumps(payload, ensure_ascii=False)}
        ]
        try:
            return await _call_json(FIELD_MAP_SYSTEM, content, MAX_MAP_TOKENS)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"field mapping failed: {type(exc).__name__}: {exc}"[:300])
            return None

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(chunk):
        async with semaphore:
            return await map_chunk(chunk)

    results = await asyncio.gather(*(one(c) for c in chunks))
    for data in results:
        if isinstance(data, dict):
            found.extend(_sanitize_fields(data.get("fields", data), numbered, allowed))
    # Deduplicate across chunks, preserving the first clear occurrence.
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in found:
        key = (row["field_key"], _norm(row["value"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped, warnings


async def _map_table_to_fields(
    table: list[dict], field_defs: list[dict], light: bool
) -> tuple[list[dict], list[str]]:
    if not field_defs or not table:
        return [], []
    allowed = {d["field_key"] for d in field_defs}
    guide = _field_guide(field_defs)
    warnings: list[str] = []
    chunks = _chunk_table(table)
    if light and chunks:
        chunks = chunks[:LIGHT_CHUNKS]

    async def map_chunk(chunk: list[dict]):
        payload = {
            "requested_fields": [
                {"field_key": d["field_key"], "label": d.get("label")} for d in field_defs
            ],
            "cells": chunk,
        }
        content = [{"type": "text", "text": (
            guide
            + "\n\nMap cells onto the requested fields. Location MUST be the exact "
              "sheet/cell string from the input. Only use values present in the cells.\n"
            + json.dumps(payload, ensure_ascii=False)
        )}]
        try:
            data = await _call_json(FIELD_MAP_SYSTEM, content, MAX_MAP_TOKENS)
            raw = data.get("fields")
            if raw is None:
                raw = data.get("items")
            return raw, chunk
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"table mapping failed: {type(exc).__name__}: {exc}"[:300])
            return None, chunk

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(chunk):
        async with semaphore:
            return await map_chunk(chunk)

    results = await asyncio.gather(*(one(c) for c in chunks))
    valid_locations = table_locations(table)
    cell_values = {_norm(c.get("value")) for c in table if c.get("value")}
    found: list[dict] = []
    for raw, _chunk in results:
        if not isinstance(raw, list):
            continue
        for it in raw:
            if not isinstance(it, dict):
                continue
            key = it.get("field_key")
            value = it.get("value")
            location = it.get("location")
            if key not in allowed or not isinstance(value, str) or not value.strip():
                continue
            if _norm(value) not in cell_values:
                continue
            if location not in valid_locations:
                # Fall back to the first cell that contains this exact value.
                location = next(
                    (f"{c['sheet']} / {c['cell']}" for c in table
                     if _norm(c.get("value")) == _norm(value)),
                    None,
                )
            found.append({
                "field_key": key,
                "value": value.strip(),
                "unit": it.get("unit") if isinstance(it.get("unit"), str) else None,
                "location": location,
            })
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in found:
        dedupe = (row["field_key"], _norm(row["value"]))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        deduped.append(row)
    return deduped, warnings


# ---------------------------------------------------------------------------
# Parties (who is named on the document?)
# ---------------------------------------------------------------------------
_PARTY_LINE_RE = re.compile(
    r"(?im)^\s*(manufacturer|supplier|vendor|applicant|brand|laboratory|lab|"
    r"fabricante|fornitore|richiedente|marchio|laboratorio)\s*[:–-]\s*(.+?)\s*$"
)
_PARTY_ROLES = {
    "manufacturer": "vendor", "supplier": "supplier", "vendor": "vendor",
    "applicant": "applicant", "brand": "brand", "laboratory": "laboratory",
    "lab": "laboratory", "fabricante": "vendor", "fornitore": "supplier",
    "richiedente": "applicant", "marchio": "brand", "laboratorio": "laboratory",
}


def parties_from_text(text: str) -> list[dict]:
    found: list[dict] = []
    seen: set[str] = set()
    for match in _PARTY_LINE_RE.finditer(text or ""):
        name = match.group(2).strip()[:120]
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        found.append({"name": name, "role": _PARTY_ROLES.get(match.group(1).lower(), "other")})
        if len(found) >= PARTIES_CAP:
            break
    result = cap_parties(found)
    result.truncated = len(found) > PARTIES_CAP
    return result


# ---------------------------------------------------------------------------
# Demo mode (deterministic; never calls the network)
# ---------------------------------------------------------------------------
def _demo_extract(parsed: ParsedDocument, field_defs: list[dict]) -> GroundedResult:
    from app.services.demo_extraction import PDF_LINE_RE, _match_field, map_table

    if parsed.kind == "table":
        fields = list(map_table(parsed.tables, field_defs))
        from app.services.document_items import table_to_doc_items

        raw_items = table_to_doc_items(parsed.tables)
        items = list(raw_items)
        return GroundedResult(
            items=items,
            fields=[
                {"field_key": f["field_key"], "value": f["value"],
                 "unit": f["unit"], "location": f["location"]} for f in fields
            ],
            document_type="spreadsheet",
            language="unknown",
            summary=f"Spreadsheet with {len(parsed.tables)} non-empty cells.",
            parties=parties_from_text(parsed.source_text),
            truncated=bool(parsed.truncated or getattr(raw_items, "truncated", False)),
            warnings=[] if parsed.has_text else ["The spreadsheet contains no readable cells."],
        )

    if parsed.needs_vision:
        return GroundedResult(
            items=[], fields=[], document_type="scan/image", language="unknown",
            summary="Demo mode cannot OCR images or scans.",
            warnings=["Demo mode has no OCR. Turn demo mode off to read scans and photos."],
            vision=True,
        )

    items: list[dict] = []
    found: set[str] = set()
    for page in parsed.pages:
        for line in (page.text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            match = PDF_LINE_RE.match(line)
            if match:
                label, value = match.group(1).strip(), match.group(2).strip()
            else:
                label, value = line[:120], line
            if len(items) >= MAX_ITEMS_TOTAL:
                break
            items.append({
                "label": label, "value": value[:2000], "unit": None,
                "location": page.label, "evidence": line[:600],
            })
            if match:
                key = _match_field(label, field_defs)
                if key and key not in found:
                    found.add(key)
        if len(items) >= MAX_ITEMS_TOTAL:
            break
    fields = []
    for page in parsed.pages:
        for line in (page.text or "").splitlines():
            match = PDF_LINE_RE.match(line.strip())
            if not match:
                continue
            key = _match_field(match.group(1), field_defs)
            if not key:
                continue
            fields.append({"field_key": key, "value": match.group(2).strip(),
                           "unit": None, "location": page.label})
    return GroundedResult(
        items=items,
        fields=fields,
        document_type="text document",
        language="unknown",
        summary=(parsed.source_text[:500] if parsed.has_text
                 else "No readable text found in this document."),
        parties=parties_from_text(parsed.source_text),
        truncated=parsed.truncated or len(items) >= MAX_ITEMS_TOTAL,
        warnings=[] if parsed.has_text else ["No readable text was found in this document."],
    )


def _light_truncated(parsed: ParsedDocument) -> bool:
    """True when light mode deliberately skipped part of the document."""
    if parsed.kind == "table":
        return len(_chunk_table(parsed.tables)) > LIGHT_CHUNKS
    if parsed.needs_vision:
        return len(split_pdf_for_vision(parsed.body, VISION_PDF_PAGES)) > LIGHT_CHUNKS
    return len(_chunk_pages(parsed.pages)) > LIGHT_CHUNKS


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def extract_document(
    parsed: ParsedDocument, field_defs: list[dict], light: bool = False
) -> GroundedResult:
    """Transcribe a parsed document, then map fields from the transcription.

    ``light`` limits work to one chunk (serverless tick / 60s budget). The
    standalone worker uses the full pipeline.
    """
    if settings.demo_mode:
        return _demo_extract(parsed, field_defs)

    if not settings.anthropic_api_key:
        raise GroundedExtractionError("AI is not configured on this server.")

    items, meta, warnings = await _transcribe_document(parsed, light)
    truncated = bool(parsed.truncated or meta.get("_items_truncated"))
    truncated = truncated or len(items) >= MAX_ITEMS_TOTAL
    if light and _light_truncated(parsed):
        truncated = True
    if parsed.kind not in {"table"} and not parsed.needs_vision:
        if len(_chunk_pages(parsed.pages)) >= MAX_CHUNKS:
            truncated = True
            warnings.append(f"document split into more than {MAX_CHUNKS} text chunks; later pages were skipped")
    if parsed.needs_vision and parsed.kind == "pdf_scanned":
        slices = split_pdf_for_vision(parsed.body, VISION_PDF_PAGES)
        if len(slices) > MAX_VISION_CHUNKS:
            truncated = True
            warnings.append(f"scanned PDF has more than {MAX_VISION_CHUNKS * VISION_PDF_PAGES} pages; later pages were skipped")
    if any("chunk failed" in w for w in warnings):
        truncated = True

    if parsed.kind == "table":
        fields, map_warnings = await _map_table_to_fields(parsed.tables, field_defs, light)
    else:
        fields, map_warnings = await _map_items_to_fields(items, field_defs, light)
    if map_warnings:
        warnings.extend(map_warnings)
        # Mapping failures mean some fields may be missing. Surface that as
        # "truncated" so the reviewer retries instead of trusting a silent gap.
        truncated = True

    summary = meta.get("summary") or ""
    if not summary:
        summary = (parsed.source_text[:500] if parsed.has_text
                   else "No readable text was found in this document.")

    return GroundedResult(
        items=items,
        fields=fields,
        document_type=meta.get("document_type", parsed.kind),
        language=meta.get("language", "unknown"),
        summary=summary,
        parties=parties_from_text(parsed.source_text) if not parsed.needs_vision else [],
        truncated=truncated,
        warnings=warnings,
        vision=parsed.needs_vision,
    )
