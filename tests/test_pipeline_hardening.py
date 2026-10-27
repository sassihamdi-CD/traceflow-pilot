"""Pipeline hardening regression tests (no external PDFs, DB, or network).

Covers the collect->clean->extract->classify->passport-evidence hardening:
- file gate depth (CSV beyond head bytes, XLSX opens as workbook, per-type caps)
- post-hoc coordinate validation (table membership, 'Page N' format)
- truncation signaling (500-row / 200x500char / 12-party caps)
- anti-overreach prompt pins (leather_origin, reach_compliance)
- publish->public contract (unpublished 404, empty reference doc -> done,0)
"""
from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

import app.routes.products as products_mod
import app.routes.public as public_mod
from app.services import storage as storage_mod
from app.services.document_items import sanitize_doc_items
from app.services.extraction import (
    TABLE_MAX_ROWS,
    cap_parties,
    csv_to_table,
    validate_item_locations,
    xlsx_to_table,
)
from app.services.extraction_prompts import FIELD_GUIDE_FOOTWEAR

ALLOWED = {"sku", "upper_material", "leather_origin", "reach_compliance"}


# ---------------------------------------------------------------- file gate

def _workbook_bytes(cells: dict[str, str], sheet: str = "Data") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for coord, val in cells.items():
        ws[coord] = val
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_csv_nul_beyond_head_rejected():
    # Innocent 8-byte head, binary payload after it: classify passes,
    # the deep check must 415.
    body = b"a,b,c\n1,2,3\n" + b"x" * 100 + b"\x00\x01\x02binary"
    assert storage_mod.classify_file("evil.csv", body[:8]) == ".csv"
    with pytest.raises(HTTPException) as e:
        storage_mod.verify_file_content("evil.csv", ".csv", body)
    assert e.value.status_code == 415


def test_csv_undecodable_rejected():
    body = "a,b\n".encode("utf-8") + b"\x81\x8d\x90"  # fails utf-8 AND cp1252
    with pytest.raises(HTTPException) as e:
        storage_mod.verify_file_content("bad.csv", ".csv", body)
    assert e.value.status_code == 415


def test_csv_without_table_structure_rejected():
    with pytest.raises(HTTPException) as e:
        storage_mod.verify_file_content("notes.csv", ".csv", b"just some words")
    assert e.value.status_code == 415


def test_csv_valid_passes_including_latin1():
    body = "sostanza;limite\ncromo VI;3 mg/kg àèìòù\n".encode("latin-1")
    assert storage_mod.verify_file_content("decl.csv", ".csv", body) == ".csv"


def test_xlsx_zip_magic_but_corrupt_rejected():
    body = b"PK\x03\x04" + b"garbage" * 100
    assert storage_mod.classify_file("fake.xlsx", body[:8]) == ".xlsx"
    with pytest.raises(HTTPException) as e:
        storage_mod.verify_file_content("fake.xlsx", ".xlsx", body)
    assert e.value.status_code == 415


def test_xlsx_real_workbook_passes():
    body = _workbook_bytes({"A1": "sku", "B2": "ABC-1"})
    assert storage_mod.verify_file_content("real.xlsx", ".xlsx", body) == ".xlsx"


def test_pdf_cap_within_absolute_limit():
    assert 10 * 1024 * 1024 <= storage_mod.MAX_BYTES_BY_EXT[".pdf"]
    assert storage_mod.MAX_BYTES_BY_EXT[".pdf"] <= storage_mod.ABSOLUTE_MAX_BYTES


def test_per_type_size_caps():
    storage_mod.check_size(".pdf", storage_mod.MAX_BYTES_BY_EXT[".pdf"])
    with pytest.raises(HTTPException) as e:
        storage_mod.check_size(".pdf", storage_mod.MAX_BYTES_BY_EXT[".pdf"] + 1)
    assert e.value.status_code == 413
    with pytest.raises(HTTPException) as e2:
        storage_mod.check_size(".xlsx", storage_mod.MAX_BYTES_BY_EXT[".xlsx"] + 1)
    assert e2.value.status_code == 413
    with pytest.raises(HTTPException) as e3:
        storage_mod.check_size(".csv", storage_mod.MAX_BYTES_BY_EXT[".csv"] + 1)
    assert e3.value.status_code == 413
    storage_mod.check_size(".csv", 1024)  # small is fine


# ---------------------------------------------------------------- coordinates

def test_xlsx_invented_location_nulled_value_kept():
    table = xlsx_to_table(_workbook_bytes({"A1": "ABC-1"}))
    assert isinstance(table, list)  # backward compat: still a list
    items = [
        {"field_key": "sku", "value": "ABC-1", "unit": None, "location": "Data / A1"},
        {"field_key": "sku", "value": "ZZZ-9", "unit": None, "location": "Data / Z99"},
        {"field_key": "sku", "value": "YYY-8", "unit": None, "location": "Other / A1"},
        {"field_key": "sku", "value": "WWW-7", "unit": None, "location": 123},
    ]
    out = validate_item_locations(items, ".xlsx", table)
    assert [i["location"] for i in out] == ["Data / A1", None, None, None]
    assert [i["value"] for i in out] == ["ABC-1", "ZZZ-9", "YYY-8", "WWW-7"]


def test_csv_location_must_match_row_and_column():
    table = csv_to_table(b"col1,col2\na,b\n")
    assert table[0] == {"sheet": "CSV", "cell": "Row 2 / col1", "value": "a"}
    items = [
        {"field_key": "sku", "value": "a", "unit": None, "location": "CSV / Row 2 / col1"},
        {"field_key": "sku", "value": "b", "unit": None, "location": "CSV / Row 99 / col1"},
        {"field_key": "sku", "value": "c", "unit": None, "location": "Row 2 / col1"},
    ]
    out = validate_item_locations(items, ".csv", table)
    assert [i["location"] for i in out] == ["CSV / Row 2 / col1", None, None]


def test_pdf_location_page_n_enforced():
    items = [
        {"field_key": "sku", "value": v, "unit": None, "location": loc}
        for v, loc in [
            ("a", "Page 3"), ("b", "Page 12"), ("c", "page 3"),
            ("d", "p. 3"), ("e", "Page three"), ("f", ""), ("g", None),
        ]
    ]
    out = validate_item_locations(items, ".pdf")
    # Case and the common 'p.' abbreviation are normalized, not discarded.
    assert [i["location"] for i in out] == ["Page 3", "Page 12", "Page 3", "Page 3", None, None, None]


def test_unknown_ext_leaves_locations_untouched():
    items = [{"field_key": "sku", "value": "a", "unit": None, "location": "wherever"}]
    assert validate_item_locations(items, ".txt")[0]["location"] == "wherever"


# ---------------------------------------------------------------- truncation

def test_xlsx_over_500_cells_truncated():
    body = _workbook_bytes({f"A{i}": f"v{i}" for i in range(1, 601)})
    rows = xlsx_to_table(body)
    assert len(rows) == TABLE_MAX_ROWS == 500
    assert rows.truncated is True


def test_xlsx_exactly_500_cells_not_truncated():
    body = _workbook_bytes({f"A{i}": f"v{i}" for i in range(1, 501)})
    rows = xlsx_to_table(body)
    assert len(rows) == 500
    assert rows.truncated is False


def test_csv_over_500_cells_truncated():
    body = b"col\n" + b"v\n" * 600
    rows = csv_to_table(body)
    assert len(rows) == 500
    assert rows.truncated is True
    small = csv_to_table(b"col\nv\n")
    assert small.truncated is False


def test_doc_items_2000_cap_truncated():
    from app.services.document_items import MAX_ITEMS

    items = [{"label": f"l{i}", "value": f"v{i}"} for i in range(MAX_ITEMS + 1)]
    out = sanitize_doc_items(items)
    assert len(out) == MAX_ITEMS
    assert out.truncated is True
    assert sanitize_doc_items([{"label": "a", "value": "b"}]).truncated is False


def test_doc_items_long_value_cut_truncated():
    from app.services.document_items import MAX_LEN

    out = sanitize_doc_items([{"label": "a", "value": "x" * (MAX_LEN + 100)}])
    assert len(out[0]["value"]) == MAX_LEN
    assert out.truncated is True


def test_parties_12_cap_truncated():
    data = [{"name": f"Org {i}", "role": "supplier"} for i in range(13)]
    out = cap_parties(data)
    assert len(out) == 12
    assert out.truncated is True
    few = cap_parties([{"name": "Acme", "role": "bogus-role"}])
    assert few == [{"name": "Acme", "role": "other"}]
    assert few.truncated is False
    assert cap_parties([]).truncated is False


# ---------------------------------------------------------------- anti-overreach pins

def test_leather_origin_tied_to_hide_skin():
    assert "leather/hide/skin" in FIELD_GUIDE_FOOTWEAR


def test_reach_compliance_requires_explicit_pass():
    assert "PASS" in FIELD_GUIDE_FOOTWEAR
    assert "RSL" in FIELD_GUIDE_FOOTWEAR


# ---------------------------------------------------------------- publish->public + empty-doc contracts

class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []
        self.rowcount = 1

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, handler):
        self._handler = handler
        self.statements: list[tuple[str, object]] = []

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return await self._handler(sql, params)

    def transaction(self):
        from tests.test_button_audit import _Ctx

        return _Ctx(self)


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        from tests.test_button_audit import _Ctx

        return _Ctx(self._conn)


@pytest.mark.asyncio
async def test_public_unpublished_slug_404(monkeypatch):
    async def _handler(sql, params):
        return _FakeCursor(row=None)

    async def _gp():
        return _FakePool(_FakeConn(_handler))

    monkeypatch.setattr(public_mod, "get_pool", _gp)
    with pytest.raises(HTTPException) as e:
        await public_mod.public_passport("no-such-slug")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_public_published_no_values_empty_fields(monkeypatch):
    async def _handler(sql, params):
        if "FROM products" in sql:
            return _FakeCursor(row=("pid-1", "Boot", "SKU-1", "Acme", "footwear", None))
        return _FakeCursor(rows=[])

    async def _gp():
        return _FakePool(_FakeConn(_handler))

    monkeypatch.setattr(public_mod, "get_pool", _gp)
    res = await public_mod.public_passport("live-slug")
    assert res == {
        "product_name": "Boot",
        "sku": "SKU-1",
        "manufacturer_name": "Acme",
        "category": "footwear",
        "published_at": None,
        "fields": [],
    }


@pytest.mark.asyncio
async def test_empty_reference_doc_marks_done_zero_not_failed(monkeypatch):
    """RSL-like doc: model returns [] -> done,0 (correct empty), never failed."""
    conn = _FakeConn(_category_handler)

    async def _gp():
        return _FakePool(conn)

    async def _defs(c, category):
        return [{"field_key": "sku", "label": "SKU"}]

    async def _extract(table, defs):
        return []  # reference standard: nothing to map

    monkeypatch.setattr(products_mod, "get_pool", _gp)
    monkeypatch.setattr(products_mod, "fetch_field_definitions", _defs)
    monkeypatch.setattr(products_mod, "extract_table_mapped", _extract)
    await products_mod._run_extraction("doc-1", "prod-1", ".csv", b"a,b\n1,2\n", "ws-1")
    updates = [p for s, p in conn.statements if "UPDATE documents" in s]
    assert updates, "document row outcome was never recorded"
    status, count, _err, truncated, _doc = updates[0]
    assert (status, count) == ("done", 0)
    assert truncated is False
    assert not any("INSERT INTO field_values" in s for s, _ in conn.statements)


@pytest.mark.asyncio
async def test_invented_location_inserted_with_null_location(monkeypatch):
    conn = _FakeConn(_category_handler)

    async def _gp():
        return _FakePool(conn)

    async def _defs(c, category):
        return [{"field_key": "sku", "label": "SKU"}]

    async def _extract(table, defs):
        return [
            {"field_key": "sku", "value": "1", "unit": None, "location": "CSV / Row 2 / a"},
            {"field_key": "sku", "value": "X", "unit": None, "location": "Sheet 9 / Z99"},
        ]

    monkeypatch.setattr(products_mod, "get_pool", _gp)
    monkeypatch.setattr(products_mod, "fetch_field_definitions", _defs)
    monkeypatch.setattr(products_mod, "extract_table_mapped", _extract)
    await products_mod._run_extraction("doc-1", "prod-1", ".csv", b"a,b\n1,2\n", "ws-1")
    inserts = [p for s, p in conn.statements if "INSERT INTO field_values" in s]
    assert len(inserts) == 2
    assert inserts[0][6] == "CSV / Row 2 / a"  # real pin survives
    assert inserts[1][6] is None  # invented pin nulled, value kept


async def _category_handler(sql, params):
    if "SELECT category FROM products" in sql:
        return _FakeCursor(row=("footwear",))
    return _FakeCursor()
