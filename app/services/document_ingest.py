"""Universal document ingestion.

One parser for every format a manufacturer actually sends us. The old pilot
only understood PDF/XLSX/CSV and sent PDFs straight to the model, which meant
a DOCX, a scanned PDF, a photo of a declaration, an ODS, or a legacy XLS simply
failed (or worse: the model was still asked for the seven footwear fields and
invented plausible-looking values).

This module converts bytes into a `ParsedDocument`:

- text-bearing formats -> labelled pages/sections with exact locations
- spreadsheets        -> cell coordinates (sheet / A1)
- scanned PDFs/images -> raw bytes kept for the vision extractor
- unknown/corrupt     -> a typed DocumentParseError with an actionable hint

Nothing here calls the network. Parsing is deterministic and unit-testable.
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser

PDF_EXT = ".pdf"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".rtf"}
MARKUP_EXTENSIONS = {".html", ".htm", ".xml", ".json"}
SPREADSHEET_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".ods"}
DOCUMENT_EXTENSIONS = {".docx", ".doc", ".odt", ".pptx", ".odp", ".eml"}

SUPPORTED_EXTENSIONS = (
    {PDF_EXT}
    | IMAGE_EXTENSIONS
    | TEXT_EXTENSIONS
    | MARKUP_EXTENSIONS
    | SPREADSHEET_EXTENSIONS
    | DOCUMENT_EXTENSIONS
)

MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".log": "text/plain",
    ".rtf": "application/rtf",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".eml": "message/rfc822",
}

# A page with less text than this is treated as a scan/vision page.
SCANNED_PAGE_MIN_CHARS = 20
MAX_PAGES = 2000
MAX_CELLS = 50_000
MAX_TEXT_CHARS = 4_000_000
MAX_PAGE_CHARS = 250_000
MAX_CELL_CHARS = 4_000
MAX_TABLE_TEXT_CHARS = 8_000_000

# Zip-based containers can be crafted to expand from a few KB into gigabytes
# (zip bombs). These limits are deliberately high for real manufacturer packs
# (embedded images, many sheets) but stop decompression attacks before the
# Office/ODF libraries try to open the archive.
ZIP_EXTENSIONS = {".docx", ".xlsx", ".xlsm", ".pptx", ".odt", ".ods", ".odp"}
MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_UNCOMPRESSED = 512 * 1024 * 1024
MAX_ZIP_RATIO = 200


class DocumentParseError(Exception):
    """A file reached the parser but its contents cannot be read.

    `code` is stable for logs/tests; `message`/`hint` are user-facing and are
    copied onto the document row so the reviewer sees what to do next.
    """

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass
class ParsedPage:
    label: str
    text: str
    index: int = 1


@dataclass
class ParsedDocument:
    filename: str
    ext: str
    kind: str
    mime: str
    pages: list[ParsedPage] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    body: bytes = b""
    page_count: int | None = None
    text_chars: int = 0
    scanned: bool = False
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def source_text(self) -> str:
        """Text used for grounding checks (no location labels)."""
        if self.tables and not self.pages:
            return " ".join(str(c.get("value", "")) for c in self.tables)[:MAX_TABLE_TEXT_CHARS]
        return "\n".join(p.text for p in self.pages if p.text)

    @property
    def has_text(self) -> bool:
        return bool(self.source_text.strip())

    @property
    def needs_vision(self) -> bool:
        return self.kind in {"image", "pdf_scanned"} and bool(self.body)


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _decode_text(body: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _cell_text(value: object) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)[:MAX_CELL_CHARS]


def _normalize_page_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _take_text(text: str, used: int) -> tuple[str, int, bool]:
    """Cap one page/section against the per-page and document text budgets."""
    truncated = False
    if len(text) > MAX_PAGE_CHARS:
        text = text[:MAX_PAGE_CHARS]
        truncated = True
    remaining = MAX_TEXT_CHARS - used
    if remaining <= 0:
        return "", used, True
    if len(text) > remaining:
        return text[:remaining], MAX_TEXT_CHARS, True
    return text, used + len(text), truncated


def check_zip_safety(body: bytes, ext: str) -> None:
    """Reject zip-based Office/ODF files that would decompress dangerously.

    Called before any library opens the container. A real .docx/.xlsx pack is
    usually below ~10:1; a zip bomb is thousands to one and can exhaust the
    worker's memory. Limits allow large genuine documents with images.
    """
    if ext not in ZIP_EXTENSIONS:
        return
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise DocumentParseError(
                    "zip_too_many_files",
                    "This Office/OpenDocument file contains too many internal files.",
                    "Re-save it from the original application and upload again.",
                )
            total = compressed = 0
            for info in infos:
                size = max(int(info.file_size or 0), 0)
                total += size
                compressed += max(int(info.compress_size or 0), 0)
                if total > MAX_ZIP_UNCOMPRESSED:
                    raise DocumentParseError(
                        "zip_too_large",
                        "This Office/OpenDocument file expands to an unsafe size.",
                        "Re-save it from the original application and upload again.",
                    )
            if compressed and total > 8 * 1024 * 1024 and total > compressed * MAX_ZIP_RATIO:
                raise DocumentParseError(
                    "zip_bomb",
                    "This Office/OpenDocument file looks like a decompression bomb.",
                    "Re-save it from the original application and upload again.",
                )
    except zipfile.BadZipFile as exc:
        raise DocumentParseError(
            "corrupt_container",
            "This file is not a valid Office/OpenDocument file.",
            "Re-save it from the original application and upload again.",
        ) from exc
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(
            "corrupt_container",
            "This file is not a valid Office/OpenDocument file.",
            "Re-save it from the original application and upload again.",
        ) from exc


def _zip_kind(body: bytes, ext: str) -> str:
    """Inspect a zip container to distinguish Office/ODF formats."""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = set(zf.namelist())
            if "word/document.xml" in names:
                return "docx"
            if "xl/workbook.xml" in names:
                return "xlsx"
            if "ppt/presentation.xml" in names:
                return "pptx"
            if "mimetype" in names:
                try:
                    mime = zf.read("mimetype").decode("ascii", errors="ignore")
                except Exception:
                    mime = ""
                if "opendocument.text" in mime:
                    return "odt"
                if "opendocument.spreadsheet" in mime:
                    return "ods"
                if "opendocument.presentation" in mime:
                    return "odp"
    except zipfile.BadZipFile:
        pass
    return {
        ".docx": "docx",
        ".xlsx": "xlsx",
        ".xlsm": "xlsx",
        ".pptx": "pptx",
        ".odt": "odt",
        ".ods": "ods",
        ".odp": "odp",
    }.get(ext, "unknown")


def _parse_pdf(body: bytes) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DocumentParseError(
            "pdf_reader_missing",
            "PDF reading is not available on this server.",
            "Contact the pilot admin.",
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception as exc:
                raise DocumentParseError(
                    "pdf_encrypted",
                    "This PDF is password-protected, so its contents cannot be read.",
                    "Remove the password (Save as a new PDF) and upload again.",
                ) from exc
        pages: list[ParsedPage] = []
        page_count = len(reader.pages)
        truncated = page_count > MAX_PAGES
        text_chars = 0
        for i in range(min(page_count, MAX_PAGES)):
            page = reader.pages[i]
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            text, text_chars, page_truncated = _take_text(text, text_chars)
            text = _normalize_page_text(text)
            truncated = truncated or page_truncated
            if text:
                pages.append(ParsedPage(f"Page {i + 1}", text, i + 1))
            if text_chars >= MAX_TEXT_CHARS:
                truncated = True
                break
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(
            "pdf_corrupt",
            "This PDF could not be opened.",
            "Re-export it as a PDF and upload again.",
        ) from exc

    scanned = page_count > 0 and text_chars < max(SCANNED_PAGE_MIN_CHARS, page_count * 15)
    return ParsedDocument(
        filename="", ext=PDF_EXT, kind="pdf_scanned" if scanned else "pdf",
        mime=MIME_BY_EXT[PDF_EXT], pages=pages, body=body,
        page_count=page_count, text_chars=text_chars, scanned=scanned,
        truncated=truncated,
    )


def _parse_docx(body: bytes) -> ParsedDocument:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError(
            "docx_reader_missing", "Word documents are not supported on this server.",
            "Contact the pilot admin.",
        ) from exc
    try:
        doc = Document(io.BytesIO(body))
    except Exception as exc:
        raise DocumentParseError(
            "docx_corrupt", "This Word document could not be opened.",
            "Re-save it as .docx or PDF and upload again.",
        ) from exc
    pages: list[ParsedPage] = []
    text_chars = 0
    truncated = False
    for i, para in enumerate(doc.paragraphs, start=1):
        text, text_chars, cut = _take_text(para.text.strip(), text_chars)
        truncated = truncated or cut
        if text:
            pages.append(ParsedPage(f"Paragraph {i}", text, i))
        if text_chars >= MAX_TEXT_CHARS:
            break
    if text_chars < MAX_TEXT_CHARS:
        for t_idx, table in enumerate(doc.tables, start=1):
            lines = []
            for row in table.rows:
                cells = [_normalize_page_text(c.text.strip()) for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
            if lines:
                text, text_chars, cut = _take_text("\n".join(lines), text_chars)
                truncated = truncated or cut
                if text:
                    pages.append(ParsedPage(f"Table {t_idx}", text, len(pages) + 1))
            if text_chars >= MAX_TEXT_CHARS:
                break
    return ParsedDocument(
        filename="", ext=".docx", kind="docx", mime=MIME_BY_EXT[".docx"],
        pages=pages, body=body, page_count=len(pages),
        text_chars=text_chars, truncated=truncated,
    )


def _parse_pptx(body: bytes) -> ParsedDocument:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError(
            "pptx_reader_missing", "PowerPoint files are not supported on this server.",
            "Contact the pilot admin.",
        ) from exc
    try:
        prs = Presentation(io.BytesIO(body))
    except Exception as exc:
        raise DocumentParseError(
            "pptx_corrupt", "This PowerPoint file could not be opened.",
            "Re-save it as .pptx or PDF and upload again.",
        ) from exc
    pages: list[ParsedPage] = []
    text_chars = 0
    truncated = False
    for i, slide in enumerate(prs.slides, start=1):
        lines = []
        for shape in slide.shapes:
            text = getattr(shape, "text", "") or ""
            if text.strip():
                lines.append(text.strip())
        text, text_chars, cut = _take_text(_normalize_page_text("\n".join(lines)), text_chars)
        truncated = truncated or cut
        if text:
            pages.append(ParsedPage(f"Slide {i}", text, i))
        if text_chars >= MAX_TEXT_CHARS:
            truncated = True
            break
    return ParsedDocument(
        filename="", ext=".pptx", kind="pptx", mime=MIME_BY_EXT[".pptx"],
        pages=pages, body=body, page_count=len(pages),
        text_chars=text_chars, truncated=truncated,
    )


def _column_letter(idx: int) -> str:
    """1-based column index -> spreadsheet letter (1 -> A, 27 -> AA)."""
    out = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _xlsx_cells(body: bytes) -> tuple[list[dict], bool]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError(
            "xlsx_reader_missing", "Excel files are not supported on this server.",
            "Contact the pilot admin.",
        ) from exc
    try:
        wb = load_workbook(filename=io.BytesIO(body), data_only=True, read_only=True)
    except Exception as exc:
        raise DocumentParseError(
            "not_a_workbook", "This file is named .xlsx but is not a readable Excel workbook.",
            "Re-save it from Excel or LibreOffice as .xlsx and upload again.",
        ) from exc
    cells: list[dict] = []
    truncated = False
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=False):
                for cell in row:
                    if cell.value is None:
                        continue
                    if isinstance(cell.value, str) and not cell.value.strip():
                        continue
                    if len(cells) >= MAX_CELLS:
                        truncated = True
                        return cells, truncated
                    raw = _cell_text(cell.value)
                    if len(raw) >= MAX_CELL_CHARS:
                        truncated = True
                    cells.append({"sheet": ws.title, "cell": cell.coordinate,
                                  "value": raw})
    finally:
        wb.close()
    return cells, truncated


def _xls_cells(body: bytes) -> tuple[list[dict], bool]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError(
            "xls_reader_missing", "Legacy Excel (.xls) files are not supported on this server.",
            "Save it as .xlsx or CSV and upload again.",
        ) from exc
    try:
        book = xlrd.open_workbook(file_contents=body)
    except Exception as exc:
        raise DocumentParseError(
            "xls_corrupt", "This legacy Excel file could not be opened.",
            "Save it as .xlsx or CSV and upload again.",
        ) from exc
    cells: list[dict] = []
    cells_truncated = False
    for sheet in book.sheets():
        for r in range(sheet.nrows):
            for c in range(sheet.ncols):
                value = sheet.cell_value(r, c)
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                if len(cells) >= MAX_CELLS:
                    return cells, True
                raw = _cell_text(value)
                if len(raw) >= MAX_CELL_CHARS:
                    cells_truncated = True
                cells.append({
                    "sheet": sheet.name,
                    "cell": f"{_column_letter(c + 1)}{r + 1}",
                    "value": raw,
                })
    return cells, cells_truncated


def _ods_cells(body: bytes) -> tuple[list[dict], bool]:
    try:
        from odf import teletype
        from odf.opendocument import load
        from odf.table import Table, TableCell, TableRow
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError(
            "ods_reader_missing", "OpenDocument spreadsheets are not supported on this server.",
            "Save the file as .xlsx or CSV and upload again.",
        ) from exc
    try:
        doc = load(io.BytesIO(body))
    except Exception as exc:
        raise DocumentParseError(
            "ods_corrupt", "This OpenDocument spreadsheet could not be opened.",
            "Save the file as .xlsx or CSV and upload again.",
        ) from exc
    cells: list[dict] = []
    ods_truncated = False
    for table in doc.getElementsByType(Table):
        sheet_name = table.getAttribute("name") or "Sheet"
        row_idx = 0
        for row in table.getElementsByType(TableRow):
            row_idx += 1
            col_idx = 0
            for cell in row.getElementsByType(TableCell):
                repeat = int(cell.getAttribute("numbercolumnsrepeated") or 1)
                text = teletype.extractText(cell).strip()
                value = cell.getAttribute("value") or text
                for _ in range(repeat):
                    col_idx += 1
                    if value is None or str(value).strip() == "":
                        continue
                    if len(cells) >= MAX_CELLS:
                        return cells, True
                    raw = str(value).strip()[:MAX_CELL_CHARS]
                    if len(str(value).strip()) > MAX_CELL_CHARS:
                        ods_truncated = True
                    cells.append({
                        "sheet": sheet_name,
                        "cell": f"{_column_letter(col_idx)}{row_idx}",
                        "value": raw,
                    })
    return cells, ods_truncated


def _table_text(cells: list[dict]) -> tuple[str, bool]:
    lines: list[str] = []
    size = 0
    truncated = False
    for c in cells:
        line = f"{c['sheet']} / {c['cell']}: {c['value']}"
        if size + len(line) > MAX_TABLE_TEXT_CHARS:
            truncated = True
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines), truncated


def _spreadsheet_document(filename: str, ext: str, body: bytes) -> ParsedDocument:
    truncated = False
    if ext in {".csv", ".tsv"}:
        from app.services.extraction import csv_to_table

        try:
            cells = list(csv_to_table(body, max_rows=MAX_CELLS))
        except Exception as exc:
            raise DocumentParseError(
                "csv_unreadable", "This CSV could not be parsed as a table.",
                "Re-export it as CSV (UTF-8) with a header row and upload again.",
            ) from exc
        truncated = bool(getattr(cells, "truncated", False))
    elif ext in {".xlsx", ".xlsm"}:
        cells, truncated = _xlsx_cells(body)
    elif ext == ".xls":
        cells, truncated = _xls_cells(body)
    elif ext == ".ods":
        cells, truncated = _ods_cells(body)
    else:
        raise DocumentParseError("unsupported_type", "Unsupported spreadsheet format.")
    text, text_truncated = _table_text(cells) if cells else ("", False)
    pages = [ParsedPage("Table", text, 1)] if text else []
    return ParsedDocument(
        filename="", ext=ext, kind="table", mime=MIME_BY_EXT.get(ext, "application/octet-stream"),
        pages=pages, tables=cells, body=body, page_count=None,
        text_chars=len(text), truncated=truncated or text_truncated,
    )


class _TextCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "td", "th"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _html_text(text: str) -> str:
    parser = _TextCollector()
    parser.feed(text)
    return _normalize_page_text(" ".join(parser.parts))


def _rtf_text(text: str) -> str:
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\(par|line)\b", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = re.sub(r"[{}]", "", text)
    return _normalize_page_text(text)


def _parse_eml(body: bytes) -> ParsedDocument:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(body)
    except Exception as exc:
        raise DocumentParseError(
            "eml_corrupt", "This email file could not be opened.",
            "Save the email as .eml or paste its text and upload again.",
        ) from exc
    lines = [f"Subject: {msg.get('subject', '')}", f"From: {msg.get('from', '')}",
             f"To: {msg.get('to', '')}", f"Date: {msg.get('date', '')}"]
    plain = None
    html = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = part.get_content()
        elif ctype == "text/html" and html is None:
            html = part.get_content()
    text = plain or (_html_text(html) if html else "")
    combined, text_chars, truncated = _take_text(
        _normalize_page_text("\n".join(lines) + "\n\n" + str(text)), 0
    )
    pages = [ParsedPage("Email", combined, 1)] if combined else []
    return ParsedDocument(
        filename="", ext=".eml", kind="email", mime=MIME_BY_EXT[".eml"],
        pages=pages, body=body, page_count=1,
        text_chars=text_chars, truncated=truncated,
    )


def _parse_textual(ext: str, body: bytes) -> ParsedDocument:
    raw = _decode_text(body)
    if ext in {".html", ".htm"}:
        text = _html_text(raw)
        kind = "html"
    elif ext == ".json":
        try:
            text = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            text = raw
        kind = "json"
    elif ext == ".rtf":
        text = _rtf_text(raw)
        kind = "rtf"
    elif ext == ".xml":
        text = raw
        kind = "xml"
    else:
        text = _normalize_page_text(raw)
        kind = "text"
    text = text[:MAX_TEXT_CHARS]
    pages = [ParsedPage("Document", text, 1)] if text.strip() else []
    return ParsedDocument(
        filename="", ext=ext, kind=kind, mime=MIME_BY_EXT.get(ext, "text/plain"),
        pages=pages, body=body, page_count=1,
        text_chars=len(text), truncated=len(raw) > MAX_TEXT_CHARS,
    )


def _image_mime(body: bytes, ext: str) -> str:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if body[:2] == b"BM":
        return "image/bmp"
    return MIME_BY_EXT.get(ext, "image/jpeg")


def _parse_image(body: bytes, ext: str) -> ParsedDocument:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(body)) as img:
            img.verify()
    except Exception as exc:  # noqa: BLE001 - any decoder failure is "not an image"
        raise DocumentParseError(
            "not_an_image",
            "This file is named as an image but cannot be opened as one.",
            "Export it as PNG, JPG or PDF and upload again.",
        ) from exc
    return ParsedDocument(
        filename="", ext=ext, kind="image", mime=_image_mime(body, ext),
        pages=[ParsedPage("Image 1", "", 1)], body=body, page_count=1,
        text_chars=0, scanned=True,
    )


def parse_document(filename: str, body: bytes) -> ParsedDocument:
    """Parse any supported upload. Raises DocumentParseError, never KeyError."""
    if not body:
        raise DocumentParseError(
            "empty_file", "That file is empty.", "Choose a file that contains data and upload again."
        )
    ext = _ext(filename)
    if ext not in SUPPORTED_EXTENSIONS:
        raise DocumentParseError(
            "unsupported_type",
            f"We cannot read “{filename}”.",
            "Upload PDF, Word (.docx), Excel (.xlsx/.xls), CSV, OpenDocument, "
            "PowerPoint, email (.eml), text, or an image.",
        )

    if ext == ".doc":
        raise DocumentParseError(
            "legacy_doc",
            "Legacy Word (.doc) files cannot be read directly.",
            "Open it in Word/LibreOffice and save as .docx or PDF, then upload again.",
        )

    if ext in ZIP_EXTENSIONS:
        check_zip_safety(body, ext)

    if ext == PDF_EXT:
        parsed = _parse_pdf(body)
    elif ext in IMAGE_EXTENSIONS:
        parsed = _parse_image(body, ext)
    elif ext in SPREADSHEET_EXTENSIONS:
        parsed = _spreadsheet_document(filename, ext, body)
    elif ext in ZIP_EXTENSIONS:
        kind = _zip_kind(body, ext)
        if kind == "docx":
            parsed = _parse_docx(body)
        elif kind in {"xlsx", "ods"}:
            parsed = _spreadsheet_document(filename, ext if kind == "xlsx" else ".ods", body)
        elif kind == "pptx":
            parsed = _parse_pptx(body)
        elif kind == "odt":
            parsed = _parse_odt(body)
        elif kind == "odp":
            parsed = _parse_odp(body)
        else:
            raise DocumentParseError(
                "corrupt_container",
                "This file is not a valid Office/OpenDocument file.",
                "Re-save it from its original application and upload again.",
            )
    elif ext == ".eml":
        parsed = _parse_eml(body)
    else:
        parsed = _parse_textual(ext, body)

    parsed.filename = filename
    return parsed


def _parse_odt(body: bytes) -> ParsedDocument:
    try:
        from odf import teletype
        from odf.opendocument import load
        from odf.text import P
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError(
            "odt_reader_missing", "OpenDocument text files are not supported on this server.",
            "Save it as .docx or PDF and upload again.",
        ) from exc
    try:
        doc = load(io.BytesIO(body))
    except Exception as exc:
        raise DocumentParseError(
            "odt_corrupt", "This OpenDocument text file could not be opened.",
            "Save it as .docx or PDF and upload again.",
        ) from exc
    pages: list[ParsedPage] = []
    text_chars = 0
    truncated = False
    for i, para in enumerate(doc.getElementsByType(P), start=1):
        text, text_chars, cut = _take_text(teletype.extractText(para).strip(), text_chars)
        truncated = truncated or cut
        if text:
            pages.append(ParsedPage(f"Paragraph {i}", text, i))
        if text_chars >= MAX_TEXT_CHARS:
            truncated = True
            break
    return ParsedDocument(
        filename="", ext=".odt", kind="odt", mime=MIME_BY_EXT[".odt"],
        pages=pages, body=body, page_count=len(pages),
        text_chars=text_chars, truncated=truncated,
    )


def _parse_odp(body: bytes) -> ParsedDocument:
    try:
        from odf import teletype
        from odf.draw import Page
        from odf.opendocument import load
    except ImportError as exc:  # pragma: no cover
        raise DocumentParseError(
            "odp_reader_missing", "OpenDocument presentations are not supported on this server.",
            "Save it as .pptx or PDF and upload again.",
        ) from exc
    try:
        doc = load(io.BytesIO(body))
    except Exception as exc:
        raise DocumentParseError(
            "odp_corrupt", "This OpenDocument presentation could not be opened.",
            "Save it as .pptx or PDF and upload again.",
        ) from exc
    pages: list[ParsedPage] = []
    text_chars = 0
    truncated = False
    for i, page in enumerate(doc.getElementsByType(Page), start=1):
        text, text_chars, cut = _take_text(
            _normalize_page_text(teletype.extractText(page).strip()), text_chars
        )
        truncated = truncated or cut
        if text:
            pages.append(ParsedPage(f"Slide {i}", text, i))
        if text_chars >= MAX_TEXT_CHARS:
            truncated = True
            break
    return ParsedDocument(
        filename="", ext=".odp", kind="odp", mime=MIME_BY_EXT[".odp"],
        pages=pages, body=body, page_count=len(pages),
        text_chars=text_chars, truncated=truncated,
    )


def split_pdf_for_vision(body: bytes, max_pages: int = 20) -> list[tuple[str, bytes]]:
    """Split a scanned PDF into <=max_pages native-PDF chunks.

    Anthropic accepts PDFs directly, but a 200-page scan blows the request
    size/time budget. Splitting keeps each call small and lets the worker
    report partial progress instead of failing the whole document.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:  # pragma: no cover
        return [("PDF", body)]
    try:
        reader = PdfReader(io.BytesIO(body))
        total = len(reader.pages)
        if total <= max_pages:
            return [("PDF", body)]
        chunks = []
        for start in range(0, total, max_pages):
            end = min(start + max_pages, total)
            writer = PdfWriter()
            for idx in range(start, end):
                writer.add_page(reader.pages[idx])
            buf = io.BytesIO()
            writer.write(buf)
            chunks.append((f"Pages {start + 1}-{end}", buf.getvalue()))
        return chunks
    except Exception:
        return [("PDF", body)]


def convert_image_for_vision(body: bytes, mime: str) -> tuple[bytes, str]:
    """Normalize exotic images to PNG so the vision model accepts them."""
    if mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        return body, mime
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(body))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception:
        return body, mime
