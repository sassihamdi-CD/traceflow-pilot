"""P0 blocker regression tests (no DB/network needed).

Covers: CORS PATCH, extraction workspace scoping, audit json.dumps,
upload size guard, binary/EXE classification, documents extracting status.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest
from fastapi import HTTPException

import app.main as main_mod
import app.routes.products as products_mod
from app.services import storage as storage_mod

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_SRC = (REPO_ROOT / "app" / "main.py").read_text()
PRODUCTS_SRC = (REPO_ROOT / "app" / "routes" / "products.py").read_text()
FIELDS_SRC = (REPO_ROOT / "app" / "routes" / "fields.py").read_text()
REQUESTS_SRC = (REPO_ROOT / "app" / "routes" / "requests.py").read_text()


def test_cors_allows_patch():
    # Source-level: PATCH/PUT/DELETE must be allowed for browser PATCH flows
    # (rename product, link document).
    for method in ("PATCH", "PUT", "DELETE"):
        assert method in MAIN_SRC, f"CORS allow_methods missing {method} in app/main.py"
    # Runtime-level: inspect registered CORSMiddleware options.
    found = False
    for mw in main_mod.app.user_middleware:
        kwargs = getattr(mw, "kwargs", {}) or {}
        opts = getattr(mw, "options", {}) or {}
        methods = kwargs.get("allow_methods") or opts.get("allow_methods") or ()
        flat = " ".join(str(m) for m in methods)
        if "PATCH" in flat:
            found = True
    assert found, "CORSMiddleware missing PATCH in registered middleware options"


def test_extraction_uses_workspace_param():
    sig = inspect.signature(products_mod._run_extraction)
    assert "workspace_id" in sig.parameters, (
        f"_run_extraction signature lacks workspace_id: {list(sig.parameters)}"
    )
    src = inspect.getsource(products_mod._run_extraction)
    assert "WORKSPACE_ID, product_id" not in src, (
        "_run_extraction still inserts with global WORKSPACE_ID"
    )
    assert "workspace_id" in src


def test_audit_uses_json_dumps():
    for name, src in (
        ("products.py", PRODUCTS_SRC),
        ("fields.py", FIELDS_SRC),
        ("requests.py", REQUESTS_SRC),
    ):
        assert "json.dumps" in src, f"{name} missing json.dumps for audit detail"
    for name, src in (
        ("products.py", PRODUCTS_SRC),
        ("fields.py", FIELDS_SRC),
        ("requests.py", REQUESTS_SRC),
    ):
        assert '{"filename": "%s"}' not in src, f"{name} has %-formatted filename audit JSON"
        assert '{"sku": "%s"}' not in src, f"{name} has %-formatted sku audit JSON"


def test_upload_size_limit_present():
    assert "100" in PRODUCTS_SRC, "products.py missing absolute size guard"
    assert "413" in PRODUCTS_SRC, "products.py missing 413 status for oversized upload"


def test_classify_rejects_binary_csv_and_exe():
    with pytest.raises(HTTPException) as e:
        storage_mod.classify_file("evil.exe", b"xx")
    assert e.value.status_code == 415
    with pytest.raises(HTTPException) as e2:
        storage_mod.classify_file("data.csv", b"\x00\x01binary")
    assert e2.value.status_code == 415
    assert storage_mod.classify_file("ok.csv", b"a,b\n1,2") == ".csv"


def test_documents_insert_sets_extracting():
    src = inspect.getsource(products_mod.upload_document)
    assert "extraction_status" in src, "upload path missing extraction_status column"
    assert "extracting" in src, "upload path missing 'extracting' status value"
