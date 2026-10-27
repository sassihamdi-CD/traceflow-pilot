"""Universal ingestion + grounded extraction regression tests.

These run without keys, DB, storage, or network. They pin the two properties
that matter for the pilot:

- Every common supplier format can be parsed into text/tables (or vision).
- Values the model returns are checked against the parsed source; a value that
  is not in the document is dropped instead of becoming a passport field.
"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from app.services import grounded_extraction as gx
from app.services.document_ingest import ParsedDocument, ParsedPage, parse_document
from app.services.grounded_extraction import (
    _ground_items,
    _sanitize_fields,
    extract_document,
)


def _xlsx_bytes(rows: list[tuple[str, str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Decl"
    for i, (label, value) in enumerate(rows, start=1):
        ws.cell(row=i, column=1, value=label)
        ws.cell(row=i, column=2, value=value)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx_bytes(paragraphs: list[str]) -> bytes:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _pptx_bytes(lines: list[str]) -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = lines[0]
    box = slide.shapes.add_textbox(0, 0, 100, 100)
    box.text_frame.text = "\n".join(lines[1:])
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_parse_text_csv_html_and_email():
    txt = parse_document("notes.txt", b"Upper material: cotton\nOrigin: Italy")
    assert txt.kind == "text" and "cotton" in txt.source_text.lower()

    csv = parse_document(
        "bom.csv",
        b"component,material,origin\nupper,cotton,Italy\nsole,rubber,Vietnam\n",
    )
    assert csv.kind == "table"
    assert "cotton" in csv.source_text.lower()
    assert any(c["value"] == "cotton" for c in csv.tables)

    html = parse_document(
        "declaration.html",
        b"<html><body><h1>Declaration</h1><p>Upper: cotton</p></body></html>",
    )
    assert "Declaration" in html.source_text and "<" not in html.source_text

    eml = parse_document(
        "supplier.eml",
        b"From: a@example.com\r\nSubject: BOM\r\n\r\nUpper: cotton\r\n",
    )
    assert "BOM" in eml.source_text and "cotton" in eml.source_text


def test_parse_docx_pptx_and_xlsx():
    docx = parse_document("spec.docx", _docx_bytes(["Upper: cotton", "Origin: Italy"]))
    assert docx.kind == "docx"
    assert "Upper: cotton" in docx.source_text

    pptx = parse_document("deck.pptx", _pptx_bytes(["Supplier deck", "Material: cotton"]))
    assert pptx.kind == "pptx"
    assert "Material: cotton" in pptx.source_text

    xlsx = parse_document("decl.xlsx", _xlsx_bytes([("Upper", "cotton"), ("Origin", "Italy")]))
    assert xlsx.kind == "table"
    assert any(c["value"] == "cotton" for c in xlsx.tables)


def test_parse_unknown_or_corrupt_is_typed():
    from app.services.document_ingest import DocumentParseError

    with pytest.raises(DocumentParseError) as e:
        parse_document("photo.png", b"not-a-png")
    assert e.value.code in {"not_an_image", "corrupt_image"}


def test_grounding_drops_invented_values():
    items = [
        {"label": "Upper", "value": "Cotton", "evidence": "100% cotton", "location": "p.1"},
        {"label": "Upper", "value": "Unicorn leather", "evidence": "unicorn", "location": "p.9"},
    ]
    kept = _ground_items(items, "Material: 100% cotton. Origin: Italy.")
    assert [i["value"] for i in kept] == ["Cotton"]


def test_field_mapping_requires_a_transcribed_item():
    items = [{"_id": 1, "value": "Cotton", "unit": None, "location": "p.1"}]
    fields = _sanitize_fields(
        [
            {"field_key": "upper_material", "value": "Cotton", "id": 1},
            {"field_key": "upper_material", "value": "Unicorn", "id": 99},
        ],
        items,
        {"upper_material"},
    )
    assert [f["value"] for f in fields] == ["Cotton"]


def test_extract_document_end_to_end_grounded(monkeypatch):
    parsed = ParsedDocument(
        filename="spec.txt",
        ext=".txt",
        kind="text",
        mime="text/plain",
        pages=[ParsedPage("text", "Upper: cotton\nOrigin: Italy", 1)],
        page_count=1,
        text_chars=25,
    )

    async def fake_call_json(system, content, max_tokens):
        if system == gx.TRANSCRIBE_SYSTEM:
            return {
                "document_type": "supplier declaration",
                "language": "en",
                "summary": "Declaration of materials.",
                "items": [
                    {"label": "Upper", "value": "Cotton", "evidence": "Upper: cotton",
                     "location": "text"},
                    {"label": "Origin", "value": "Unicorn", "evidence": "Origin: unicorn",
                     "location": "text"},
                ],
            }
        return {
            "fields": [
                {"field_key": "upper_material", "value": "Cotton", "id": 1,
                 "location": "text"},
                {"field_key": "upper_material", "value": "Unicorn", "id": 2,
                 "location": "text"},
            ]
        }

    monkeypatch.setattr(gx, "_call_json", fake_call_json)
    monkeypatch.setattr(
        gx, "settings",
        SimpleNamespace(demo_mode=False, anthropic_api_key="test", anthropic_model="test"),
    )

    result = asyncio.run(
        extract_document(parsed, [{"field_key": "upper_material", "label": "Upper material"}])
    )
    assert [i["value"] for i in result.items] == ["Cotton"]
    assert [f["value"] for f in result.fields] == ["Cotton"]
    assert result.document_type == "supplier declaration"
    assert result.summary == "Declaration of materials."


def test_mapping_failure_marks_document_truncated(monkeypatch):
    parsed = ParsedDocument(
        filename="spec.txt", ext=".txt", kind="text", mime="text/plain",
        pages=[ParsedPage("text", "Upper: cotton", 1)], page_count=1, text_chars=13,
    )

    async def fake_call_json(system, content, max_tokens):
        if system == gx.TRANSCRIBE_SYSTEM:
            return {
                "items": [{"label": "Upper", "value": "cotton",
                           "evidence": "Upper: cotton", "location": "text"}],
            }
        raise RuntimeError("simulated mapping outage")

    monkeypatch.setattr(gx, "_call_json", fake_call_json)
    monkeypatch.setattr(
        gx, "settings",
        SimpleNamespace(demo_mode=False, anthropic_api_key="test", anthropic_model="test"),
    )
    result = asyncio.run(
        extract_document(parsed, [{"field_key": "upper_material", "label": "Upper"}])
    )
    assert result.truncated is True
    assert any("mapping" in w.lower() for w in result.warnings)


def test_presign_is_unavailable_cleanly_without_storage(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from app.auth import Caller
    from app.routes import products as products_mod

    monkeypatch.setattr(products_mod, "storage_configured", lambda: False)
    caller = Caller(user_id="tester", workspace_id="ws", role="reviewer")
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            products_mod.presign_document(
                "11111111-1111-1111-1111-111111111111",
                {"filename": "big.pdf", "size": 1024},
                caller,
            )
        )
    assert e.value.status_code == 409
    assert e.value.detail["code"] == "presign_unavailable"


def test_legacy_doc_is_rejected_with_actionable_hint(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from app.auth import Caller
    from app.routes import products as products_mod

    caller = Caller(user_id="tester", workspace_id="ws", role="reviewer")
    with pytest.raises(HTTPException) as e:
        asyncio.run(
            products_mod.presign_document(
                "11111111-1111-1111-1111-111111111111",
                {"filename": "old.doc", "size": 1024},
                caller,
            )
        )
    assert e.value.status_code == 415
    assert e.value.detail["code"] == "legacy_doc"


def test_zip_bomb_is_rejected_before_opening_container():
    import zipfile

    from app.services import storage
    from app.services.document_ingest import DocumentParseError

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * (20 * 1024 * 1024))
    body = buf.getvalue()

    with pytest.raises(DocumentParseError) as e:
        parse_document("bomb.docx", body)
    assert e.value.code in {"zip_bomb", "zip_too_large"}

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e2:
        storage.verify_file_content("bomb.docx", ".docx", body)
    assert e2.value.status_code == 415
    assert e2.value.detail["code"] in {"zip_bomb", "zip_too_large"}


def test_text_budget_caps_pages_and_documents():
    from app.services.document_ingest import (
        MAX_PAGE_CHARS,
        MAX_TEXT_CHARS,
        _take_text,
    )

    text, used, cut = _take_text("x" * (MAX_PAGE_CHARS + 10), 0)
    assert cut and len(text) == MAX_PAGE_CHARS and used == MAX_PAGE_CHARS

    text, used, cut = _take_text("y" * 100, MAX_TEXT_CHARS - 50)
    assert cut and len(text) == 50 and used == MAX_TEXT_CHARS

    text, used, cut = _take_text("z", MAX_TEXT_CHARS)
    assert text == "" and used == MAX_TEXT_CHARS and cut


def test_demo_extraction_caps_items(monkeypatch):
    from app.services import grounded_extraction as gx
    from app.services.document_ingest import ParsedDocument, ParsedPage

    parsed = ParsedDocument(
        filename="huge.txt", ext=".txt", kind="text", mime="text/plain",
        pages=[ParsedPage("Document", "\n".join(
            f"Substance {i}: value {i}" for i in range(9000)
        ), 1)],
        page_count=1, text_chars=9000 * 20,
    )
    result = gx._demo_extract(parsed, [{"field_key": "upper_material", "label": "Upper"}])
    assert len(result.items) == gx.MAX_ITEMS_TOTAL
    assert result.truncated is True
