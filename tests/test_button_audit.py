"""Button-audit integration tests (checklist docs/BUTTON_AUDIT_CHECKLIST.md).

Runs WITHOUT keys, DB, or network:
- Supabase auth is bypassed via FastAPI dependency_overrides for
  get_caller / require_reviewer / require_admin (role=admin so both
  reviewer and admin-only routes are reachable).
- DB access is faked with a minimal async FakePool per route module
  (monkeypatched onto app.routes.<mod>.get_pool). Tests that need real
  Postgres semantics (row races, FK constraints, RLS) are marked SKIP
  with an explicit reason instead of faked.
- File-gate tests (415/413) hit the real classify_file + real size guard
  in upload_document; they raise before any pool/storage use.

Expected state: all pass against the hardened tree (uuid_or_404 path guards,
CorrectBody/ProductCreate non-empty validators, followup 422 guards). Any
failure below is a P0 regression of the button audit.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.routes.fields as fields_mod
import app.routes.products as products_mod
import app.routes.publish as publish_mod
import app.routes.requests as requests_mod
import app.routes.suppliers as suppliers_mod
from app.auth import Caller, get_caller, require_admin, require_member, require_reviewer
from app.main import app
from app.services.storage import classify_file


# ---------------------------------------------------------------- fake pool

class FakeCursor:
    def __init__(self, row=None, rows=None, rowcount=0):
        self._row = row
        self._rows = rows if rows is not None else []
        self.rowcount = rowcount

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _Ctx:
    """Wraps an object as an async context manager (pool.connection(),
    conn.transaction())."""

    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, handler):
        self._handler = handler

    async def execute(self, sql, params=None):
        return await self._handler(sql, params)

    def transaction(self):
        return _Ctx(self)


class FakePool:
    def __init__(self, handler):
        self._handler = handler

    def connection(self):
        return _Ctx(FakeConn(self._handler))


async def _null_handler(sql, params):
    """Default handler: every SELECT finds nothing, every UPDATE hits 0 rows."""
    return FakeCursor(row=None, rows=[], rowcount=0)


def _patch_pool(monkeypatch, module, handler=_null_handler):
    async def _get_pool():
        return FakePool(handler)

    monkeypatch.setattr(module, "get_pool", _get_pool)


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def client():
    """TestClient with auth overridden (admin role), no lifespan (no DB warmup)."""
    admin = Caller(user_id="audit-tester", workspace_id="ws-audit", role="admin")

    async def _caller():
        return admin

    app.dependency_overrides[get_caller] = _caller
    app.dependency_overrides[require_member] = _caller
    app.dependency_overrides[require_reviewer] = _caller
    app.dependency_overrides[require_admin] = _caller
    c = TestClient(app, raise_server_exceptions=False)
    yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- A/C: invalid UUID

def test_invalid_uuid_product_returns_404_or_422(client, monkeypatch):
    """C1: GET /api/products/{id} with garbage id must not 500."""
    _patch_pool(monkeypatch, products_mod)

    async def _no_detail(conn, ws, pid):
        return None

    monkeypatch.setattr(products_mod, "product_detail", _no_detail)
    r = client.get("/api/products/not-a-uuid")
    assert r.status_code in (404, 422), r.text


def test_invalid_uuid_field_accept_returns_404_or_422(client, monkeypatch):
    """C5: POST /api/fields/{id}/accept with garbage id must not 500."""
    _patch_pool(monkeypatch, fields_mod)  # SELECT finds nothing -> 404
    r = client.post("/api/fields/not-a-uuid/accept")
    assert r.status_code in (404, 422), r.text


# ---------------------------------------------------------------- C8: empty correct value -> 422 (P0, currently FAILS)

def test_correct_empty_value_rejected_422(client, monkeypatch):
    """C8: POST /api/fields/{id}/correct {value:""} must be 422.

    Guard: app/schemas.py CorrectBody._value_non_empty (was P0 C8: empty
    string passed validation and inserted an empty 'corrected' value).
    Frontend also trims client-side only.
    """
    async def _handler(sql, params):
        if sql.strip().upper().startswith("SELECT PRODUCT_ID"):
            return FakeCursor(row=("prod-1", "weight", "proposed", "doc-1"))
        if "RETURNING id" in sql:
            return FakeCursor(row=("new-id-1",))
        return FakeCursor(rowcount=1)

    _patch_pool(monkeypatch, fields_mod, _handler)
    r = client.post("/api/fields/11111111-1111-1111-1111-111111111111/correct",
                    json={"value": ""})
    assert r.status_code == 422, f"C8 P0: empty correct value not rejected: {r.status_code} {r.text}"


# ---------------------------------------------------------------- C11/D13: followup missing product_id -> 422 (P0, FAILS)

def test_followup_missing_product_id_rejected_422(client, monkeypatch):
    """D13/C11: POST /api/suppliers/{id}/followups without product_id must be 422.

    Guard: app/routes/suppliers.py create_followup rejects missing/blank
    product_id + field_key with 422 before the INSERT (was P0 D13: None went
    into a NOT NULL column -> 500 with a live DB, simulated here).
    """
    async def _handler(sql, params):
        if "FROM suppliers" in sql:
            return FakeCursor(row=("sup-1",))  # supplier exists
        raise Exception('null value in column "product_id" violates not-null constraint')

    _patch_pool(monkeypatch, suppliers_mod, _handler)
    r = client.post("/api/suppliers/22222222-2222-2222-2222-222222222222/followups",
                    json={"field_key": "weight"})
    assert r.status_code == 422, f"D13 P0: missing product_id not rejected: {r.status_code} {r.text}"


# ---------------------------------------------------------------- C16/D20: publish blocked -> 422

def test_publish_missing_fields_rejected_422(client, monkeypatch):
    """C16/D20: publish with unverified required fields must be 422.

    Gate: app/routes/publish.py publish_product blocks unverified required
    fields, verified+has_new_proposal, out-of-date evidence, and
    rejected-without-replacement (was P0 C16: gate ignored out_of_date /
    rejected_unreplaced and treated verified+has_new_proposal as publishable).
    """
    async def _handler(sql, params):
        if "FROM workspaces" in sql:
            return FakeCursor(row=(1,))  # workspace provisioned
        return FakeCursor(row=None, rows=[], rowcount=0)

    _patch_pool(monkeypatch, publish_mod, _handler)

    async def _detail(conn, ws, pid):
        return {"fields": [
            {"field_key": "weight", "required": True, "state": "proposed"},
            {"field_key": "origin", "required": True, "state": "verified"},
        ]}

    monkeypatch.setattr(publish_mod, "product_detail", _detail)
    r = client.post("/api/products/33333333-3333-3333-3333-333333333333/publish")
    assert r.status_code == 422, r.text
    assert "weight" in r.text


# ---------------------------------------------------------------- C14/F: file gates

def test_unknown_file_type_rejected_415_unit():
    """F/C14: classify_file rejects unknown types without any DB/pool."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        classify_file("evil.exe", b"MZ...")
    assert e.value.status_code == 415


def test_unknown_file_type_upload_rejected_415(client):
    """C14: full upload path with .exe must be 415 (raised before DB/storage)."""
    r = client.post("/api/products/44444444-4444-4444-4444-444444444444/documents",
                    files={"file": ("evil.exe", b"MZ...", "application/octet-stream")})
    assert r.status_code == 415, r.text


def test_oversize_upload_rejected_413(client, monkeypatch):
    """C14/F: a body above the type cap must be 413 before DB/storage."""
    import app.services.storage as storage_mod

    monkeypatch.setitem(storage_mod.MAX_BYTES_BY_EXT, ".pdf", 1024)
    big = b"%PDF" + b"x" * 4096
    r = client.post("/api/products/44444444-4444-4444-4444-444444444444/documents",
                    files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413, r.text


# ---------------------------------------------------------------- D22: request status bad value -> 422

def test_request_status_bad_value_rejected_422(client):
    """D22: POST /api/requests/{id}/status {bogus} must be 422 (pre-DB guard)."""
    r = client.post("/api/requests/55555555-5555-5555-5555-555555555555/status",
                    json={"status": "bogus"})
    assert r.status_code == 422, r.text


def test_request_status_valid_values_reach_db(client, monkeypatch):
    """D22 companion: a valid status passes validation and reaches the DB layer
    (fake pool: workspace exists, no request row -> 404, proving validation
    did NOT reject it)."""
    async def _handler(sql, params):
        if "FROM workspaces" in sql:
            return FakeCursor(row=(1,))
        return FakeCursor(row=None, rows=[], rowcount=0)

    _patch_pool(monkeypatch, requests_mod, _handler)
    for ok in ("open", "responded"):
        r = client.post("/api/requests/55555555-5555-5555-5555-555555555555/status",
                        json={"status": ok})
        assert r.status_code == 404, f"status={ok}: {r.status_code} {r.text}"


# ---------------------------------------------------------------- skipped: need live Postgres semantics

@pytest.mark.skip(reason="Needs live Postgres: accept/reject 409 race needs two "
                         "concurrent transactions on one row (fields.py:35,58).")
def test_accept_race_returns_409():
    raise NotImplementedError


@pytest.mark.skip(reason="Needs live Postgres + R2/S3: full upload->extracted "
                         "round-trip with background worker (products.py:186).")
def test_upload_extract_roundtrip():
    raise NotImplementedError
