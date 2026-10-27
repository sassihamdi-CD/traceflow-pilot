"""File-type gate + object storage (R2 in production, local volume in demo).

The pilot used to accept three extensions and cap PDFs at 10 MB because every
PDF was base64-encoded into a single model request. That is no longer true:
documents are parsed locally, chunked, and scanned pages are sent as small
vision slices. The gate therefore accepts the formats manufacturers actually
send and the size caps are much higher.

Two upload paths exist:

1. Multipart ``POST /api/products/{id}/documents`` — small files, used by
   local/demo and as a fallback.
2. Presigned R2 PUT + ``/documents/complete`` — large files. The browser
   uploads directly to R2, so Vercel's ~4.5 MB request-body limit no longer
   applies.

``verify_file_content`` validates the container cheaply (magic bytes + zip
layout) without fully parsing multi-megabyte documents on the request path.
The worker performs the real parse and records a readable error if it fails.
"""
from __future__ import annotations

import io
import os
import uuid
import zipfile

import boto3

from app import errors
from app.config import settings
from app.services.document_ingest import (
    MIME_BY_EXT,
    SUPPORTED_EXTENSIONS,
    DocumentParseError,
    check_zip_safety,
)

# Kept as a module-level name because other modules/tests import it.
ALLOWED_EXTENSIONS = SUPPORTED_EXTENSIONS

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Direct multipart ceiling (before the per-type cap). Presigned uploads are
# validated at complete-time against the same per-type caps.
ABSOLUTE_MAX_BYTES = 100 * 1024 * 1024

_MB = 1024 * 1024
MAX_BYTES_BY_EXT = {
    ".pdf": 64 * _MB,
    ".xlsx": 32 * _MB,
    ".xlsm": 32 * _MB,
    ".xls": 32 * _MB,
    ".ods": 32 * _MB,
    ".csv": 32 * _MB,
    ".tsv": 32 * _MB,
    ".docx": 32 * _MB,
    ".doc": 32 * _MB,
    ".odt": 32 * _MB,
    ".pptx": 32 * _MB,
    ".odp": 32 * _MB,
    ".eml": 20 * _MB,
    ".png": 20 * _MB,
    ".jpg": 20 * _MB,
    ".jpeg": 20 * _MB,
    ".webp": 20 * _MB,
    ".gif": 20 * _MB,
    ".tif": 20 * _MB,
    ".tiff": 20 * _MB,
    ".bmp": 20 * _MB,
    ".txt": 16 * _MB,
    ".md": 16 * _MB,
    ".markdown": 16 * _MB,
    ".log": 16 * _MB,
    ".rtf": 16 * _MB,
    ".html": 16 * _MB,
    ".htm": 16 * _MB,
    ".xml": 16 * _MB,
    ".json": 16 * _MB,
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
ZIP_EXTENSIONS = {".xlsx", ".xlsm", ".docx", ".pptx", ".odt", ".ods", ".odp"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".rtf", ".html", ".htm", ".xml", ".json"}
TABLE_EXTENSIONS = {".csv", ".tsv"}


def ext_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def content_type_for(ext: str) -> str:
    return MIME_BY_EXT.get(ext, "application/octet-stream")


def check_size(ext: str, nbytes: int) -> str:
    """Per-type size gate. Raises 413 when the body exceeds the type's cap."""
    cap = MAX_BYTES_BY_EXT.get(ext, ABSOLUTE_MAX_BYTES)
    if nbytes > cap:
        raise errors.file_too_large(cap // _MB)
    return ext


def max_size_mb(ext: str) -> int:
    return MAX_BYTES_BY_EXT.get(ext, ABSOLUTE_MAX_BYTES) // _MB


def _zip_has(body: bytes, required: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            return required in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _zip_mimetype(body: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            return zf.read("mimetype").decode("ascii", errors="ignore")
    except Exception:
        return ""


def _image_magic_ok(ext: str, head: bytes) -> bool:
    checks = {
        ".png": head.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": head.startswith(b"\xff\xd8\xff"),
        ".jpeg": head.startswith(b"\xff\xd8\xff"),
        ".gif": head.startswith(b"GIF87a") or head.startswith(b"GIF89a"),
        ".webp": head[:4] == b"RIFF" and head[8:12] == b"WEBP",
        ".tif": head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"),
        ".tiff": head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"),
        ".bmp": head.startswith(b"BM"),
    }
    return checks.get(ext, True)


def _decode_ok(body: bytes) -> str | None:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            body.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return None


def verify_file_content(filename: str, ext: str, body: bytes) -> str:
    """Deep-ish content check on the full body (after the size gate).

    Validates the actual container so a renamed binary cannot reach the
    worker. Full parsing happens asynchronously, not here.
    """
    if ext in ZIP_EXTENSIONS:
        try:
            check_zip_safety(body, ext)
        except DocumentParseError as exc:
            raise errors.file_content_problem(exc.code) from exc

    if ext == ".pdf":
        if not body.startswith(PDF_MAGIC):
            raise errors.file_content_problem("not_a_pdf")
    elif ext in IMAGE_EXTENSIONS:
        if not _image_magic_ok(ext, body[:16]):
            raise errors.file_content_problem("not_an_image")
    elif ext in {".xlsx", ".xlsm"}:
        if not body.startswith(ZIP_MAGIC) or not _zip_has(body, "xl/workbook.xml"):
            raise errors.file_content_problem("not_a_workbook")
    elif ext == ".docx":
        if not body.startswith(ZIP_MAGIC) or not _zip_has(body, "word/document.xml"):
            raise errors.file_content_problem("not_a_docx")
    elif ext == ".pptx":
        if not body.startswith(ZIP_MAGIC) or not _zip_has(body, "ppt/presentation.xml"):
            raise errors.file_content_problem("not_a_pptx")
    elif ext in {".odt", ".ods", ".odp"}:
        mimetype = _zip_mimetype(body)
        expected = {
            ".odt": "opendocument.text",
            ".ods": "opendocument.spreadsheet",
            ".odp": "opendocument.presentation",
        }[ext]
        if not body.startswith(ZIP_MAGIC) or expected not in mimetype:
            raise errors.file_content_problem("not_an_opendocument")
    elif ext == ".xls":
        if not body.startswith(OLE_MAGIC):
            raise errors.file_content_problem("not_a_workbook")
    elif ext == ".doc":
        if not body.startswith(OLE_MAGIC):
            raise errors.file_content_problem("not_a_docx")
        raise errors.file_content_problem("legacy_doc")
    elif ext in TABLE_EXTENSIONS:
        if b"\x00" in body:
            raise errors.file_content_problem("csv_binary")
        if _decode_ok(body) is None:
            raise errors.file_content_problem("csv_unreadable")
        text = body.decode("utf-8-sig", errors="ignore")
        if ext == ".tsv":
            has_table = "\t" in text or "\n" in text
        else:
            has_table = any(tok in text for tok in (",", ";", "\t", "|", "\n", "\r"))
        if not has_table:
            raise errors.file_content_problem("csv_no_table")
    elif ext == ".rtf":
        if not body.lstrip().startswith(b"{\\rtf"):
            raise errors.file_content_problem("not_rtf")
    elif ext in TEXT_EXTENSIONS:
        if _decode_ok(body) is None:
            raise errors.file_content_problem("text_unreadable")
    elif ext == ".eml":
        if b"\x00" in body:
            raise errors.file_content_problem("not_an_email")
    return ext


def classify_file(filename: str, head: bytes) -> str:
    """Validate extension + cheap magic. Raises 415 on unknown/mismatch."""
    ext = ext_of(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise errors.unsupported_type(filename or "that file")
    if ext == ".pdf" and not head.startswith(PDF_MAGIC):
        raise errors.file_content_problem("not_a_pdf")
    if ext in {".xlsx", ".xlsm", ".docx", ".pptx", ".odt", ".ods", ".odp"} and not head.startswith(ZIP_MAGIC):
        raise errors.file_content_problem("corrupt_container")
    if ext in {".xls", ".doc"} and not head.startswith(OLE_MAGIC):
        raise errors.file_content_problem("not_a_workbook")
    if ext in IMAGE_EXTENSIONS and not _image_magic_ok(ext, head):
        raise errors.file_content_problem("not_an_image")
    if ext in TABLE_EXTENSIONS and (b"\x00" in head):
        raise errors.file_content_problem("csv_binary")
    return ext


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint or None,
        aws_access_key_id=settings.r2_access_key_id or None,
        aws_secret_access_key=settings.r2_secret_access_key or None,
    )


def storage_configured() -> bool:
    return bool(
        not settings.demo_mode
        and settings.r2_endpoint
        and settings.r2_access_key_id
        and settings.r2_secret_access_key
    )


def _local_path(key: str) -> str:
    """Resolve a storage key inside DEMO_STORAGE_DIR (no traversal)."""
    root = os.path.abspath(settings.demo_storage_dir or "/srv/app/uploads")
    path = os.path.abspath(os.path.join(root, key))
    if path != root and not path.startswith(root + os.sep):
        raise ValueError("storage key escapes the demo storage directory")
    return path


def upload_to_storage(key: str, body: bytes, content_type: str) -> None:
    """R2 in production; a shared local directory in demo mode."""
    if settings.demo_mode:
        path = _local_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(body)
        return
    client = _r2_client()
    client.put_object(Bucket=settings.r2_bucket, Key=key, Body=body, ContentType=content_type)


def presign_put(key: str, content_type: str, expires: int = 900) -> str:
    """Presigned PUT so the browser uploads large files straight to R2."""
    client = _r2_client()
    return client.generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.r2_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )


def head_storage(key: str) -> dict:
    """Object metadata (size/content-type). Raises on missing/unreachable."""
    if settings.demo_mode:
        path = _local_path(key)
        stat = os.stat(path)
        return {"ContentLength": stat.st_size, "ContentType": None}
    client = _r2_client()
    return client.head_object(Bucket=settings.r2_bucket, Key=key)


def read_storage_head(key: str, nbytes: int = 16) -> bytes:
    """Read the first bytes of a stored object for magic validation."""
    if settings.demo_mode:
        with open(_local_path(key), "rb") as fh:
            return fh.read(nbytes)
    client = _r2_client()
    obj = client.get_object(Bucket=settings.r2_bucket, Key=key, Range=f"bytes=0-{nbytes - 1}")
    return obj["Body"].read(nbytes)


def read_from_storage(key: str) -> bytes:
    """Read document bytes back (demo local volume or R2)."""
    if settings.demo_mode:
        with open(_local_path(key), "rb") as fh:
            return fh.read()
    client = _r2_client()
    obj = client.get_object(Bucket=settings.r2_bucket, Key=key)
    return obj["Body"].read()


def delete_from_storage(key: str) -> None:
    """Compensating delete when a DB write after upload fails."""
    if settings.demo_mode:
        try:
            os.unlink(_local_path(key))
        except FileNotFoundError:
            pass
        return
    client = _r2_client()
    client.delete_object(Bucket=settings.r2_bucket, Key=key)


def new_storage_key(workspace_id: str, product_id: str, filename: str) -> str:
    """Per-company/object layout: {workspace}/{product}/{uuid}-{basename}.

    basename() blocks "../" escapes. The workspace prefix lets the complete
    endpoint prove a key belongs to the caller's workspace.
    """
    safe = os.path.basename(filename or "upload")
    return f"{workspace_id}/{product_id}/{uuid.uuid4()}-{safe}"


def key_belongs_to(key: str, workspace_id: str, product_id: str) -> bool:
    return key.startswith(f"{workspace_id}/{product_id}/") and ".." not in key
