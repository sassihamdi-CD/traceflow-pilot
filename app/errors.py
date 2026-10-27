"""Central human-readable error catalog.

Every user-facing API error goes through :func:`err` so the frontend (and
manufacturers) always receive the same shape::

    {"code": "publish_blocked", "message": "...", "hint": "...", "data": {...}}

- ``code``    — stable snake_case id, documented in docs/ERROR_CATALOG.md.
- ``message`` — plain-language sentence a non-engineer can understand.
                Never includes class names, tracebacks, or raw values.
- ``hint``    — what to do next.
- ``data``    — optional machine-readable extras (e.g. the blocking fields).

Rule: messages explain the *situation*, hints give the *next action*.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def err(status: int, code: str, message: str, hint: str = "", data: dict[str, Any] | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message, "hint": hint}
    if data is not None:
        detail["data"] = data
    return HTTPException(status_code=status, detail=detail)


# ---------------------------------------------------------------------------
# Auth / session / membership
# ---------------------------------------------------------------------------
def session_missing() -> HTTPException:
    return err(401, "session_missing",
               "You are signed out.",
               "Sign in again to continue — your drafts are kept in this browser.")


def session_invalid() -> HTTPException:
    return err(401, "session_invalid",
               "Your session has expired.",
               "Sign in again to continue — your drafts are kept in this browser.")


def auth_unconfigured() -> HTTPException:
    return err(500, "auth_unconfigured",
               "Sign-in is not set up on this server.",
               "Contact the pilot admin — the server is missing its sign-in settings.")


def auth_unavailable() -> HTTPException:
    return err(503, "auth_unavailable",
               "We could not reach the sign-in service.",
               "Check your internet connection and try again in a moment. Nothing was changed.")


def membership_unavailable() -> HTTPException:
    return err(503, "membership_unavailable",
               "We could not check your workspace access.",
               "Try again in a moment. Nothing was changed and no rights were granted.")


def not_member() -> HTTPException:
    return err(403, "not_member",
               "Your account is not a member of this workspace yet.",
               "Redeem your invite code on the login page, then try again.")


def forbidden_role() -> HTTPException:
    # PILOT: single role — every member can do everything. This is a safety
    # net for unexpected role values only and should not trigger in the pilot.
    return err(403, "forbidden_role",
               "Your account could not be authorised for this action.",
               "Sign out and sign in again. If it persists, ask the pilot admin to check your membership.")


def workspace_missing() -> HTTPException:
    return err(409, "workspace_missing",
               "This workspace is not set up on the server yet.",
               "Contact the pilot admin — the workspace needs to be provisioned first.")


# ---------------------------------------------------------------------------
# Generic lookup / validation
# ---------------------------------------------------------------------------
def not_found(what: str = "That item") -> HTTPException:
    return err(404, "not_found",
               f"{what} could not be found.",
               "It may have been removed, or it belongs to another workspace. Refresh the list and try again.")


def missing_field(name: str) -> HTTPException:
    return err(422, "missing_field",
               f"A required piece of information is missing: {name}.",
               "Fill it in and try again.")


def too_long(name: str, limit: str) -> HTTPException:
    return err(422, "too_long",
               f"That {name} is too long (maximum {limit}).",
               "Shorten it and try again.")


def nothing_to_update() -> HTTPException:
    return err(422, "nothing_to_update",
               "There is nothing to save — every field was left empty.",
               "Change at least one field, then save again.")


# ---------------------------------------------------------------------------
# Passport publishing
# ---------------------------------------------------------------------------
def publish_blocked(labels: list[str], pending_review: list[str] | None = None,
                    out_of_date: list[str] | None = None,
                    rejected: list[str] | None = None) -> HTTPException:
    parts: list[str] = []
    if labels:
        parts.append(f"{len(labels)} still need verification: {', '.join(labels)}")
    if pending_review:
        parts.append(f"{len(pending_review)} have new evidence to review: {', '.join(pending_review)}")
    if out_of_date:
        parts.append(f"{len(out_of_date)} rely on expired documents: {', '.join(out_of_date)}")
    if rejected:
        parts.append(f"{len(rejected)} were rejected with no replacement: {', '.join(rejected)}")
    return err(422, "publish_blocked",
               "The passport cannot be published yet — " + "; ".join(parts) + ".",
               "Accept or correct a verified value for each listed field (re-upload fresh documents where expired), then publish again.",
               data={"unverified": labels, "pending_review": pending_review or [],
                     "out_of_date": out_of_date or [], "rejected": rejected or []})


# ---------------------------------------------------------------------------
# Review decisions
# ---------------------------------------------------------------------------
def already_decided(action: str, status: str) -> HTTPException:
    return err(409, "already_decided",
               f"This value was already {status} by a reviewer, so it cannot be {action} again.",
               "Refresh the page to see its current state.")


def unique_conflict() -> HTTPException:
    """Generic 409 for a DB uniqueness race (e.g. two reviewers accepting the
    same field, or a rare invite-code collision). The transaction is rolled
    back by the connection context before the handler runs."""
    return err(409, "conflict",
               "That change conflicts with another one that was just saved.",
               "Refresh the page and try again.")


def not_candidate() -> HTTPException:
    return err(409, "not_candidate",
               "That proposal is no longer open for decision.",
               "Refresh the page — it was already accepted, rejected, or replaced by a newer proposal.")


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
def file_too_large(limit_mb: int) -> HTTPException:
    return err(413, "file_too_large",
               f"This file is too big — the limit is {limit_mb} MB.",
               "Compress the file, or split it into smaller documents, and upload again.")


def unsupported_type(filename: str) -> HTTPException:
    return err(415, "unsupported_type",
               f"We cannot read “{filename}”. Supported formats: PDF, Word, Excel, CSV, OpenDocument, PowerPoint, email, text and images.",
               "Upload PDF, Word (.docx), Excel (.xlsx/.xls), CSV, OpenDocument, "
               "PowerPoint, email (.eml), text, or an image. Legacy .doc files "
               "must be re-saved as .docx or PDF.")


def file_content_problem(kind: str) -> HTTPException:
    explanations = {
        "not_a_pdf": ("It is named .pdf but is not a real PDF file.",
                      "Re-export the document as PDF and upload again."),
        "not_an_image": ("It is named as an image but the bytes are not a supported image.",
                         "Re-save it as PNG or JPG and upload again."),
        "not_a_workbook": ("It is named as a spreadsheet but is not a readable workbook.",
                           "Re-save it from Excel (or LibreOffice) as .xlsx or CSV and upload again."),
        "not_a_docx": ("It is named as a Word document but is not a readable .docx file.",
                       "Open it in Word/LibreOffice and save as .docx or PDF, then upload again."),
        "not_a_pptx": ("It is named as a presentation but is not a readable .pptx file.",
                       "Re-save it as .pptx or PDF and upload again."),
        "not_an_opendocument": ("It is named as an OpenDocument file but is not a valid one.",
                                "Re-save it as .docx/.xlsx/.pdf and upload again."),
        "not_rtf": ("It is named .rtf but is not a real RTF file.",
                    "Re-save it as .txt, .docx or PDF and upload again."),
        "not_an_email": ("It is named .eml but is not a readable email.",
                         "Save the email as .eml again, or paste its text."),
        "text_unreadable": ("It is named as text but cannot be decoded.",
                            "Re-save it with UTF-8 encoding and upload again."),
        "corrupt_container": ("The file is named as an Office/OpenDocument file but its container is corrupt.",
                              "Re-save it from the original application and upload again."),
        "legacy_doc": ("Legacy Word (.doc) files cannot be read directly.",
                       "Open it in Word/LibreOffice and save as .docx or PDF, then upload again."),
        "csv_binary": ("It is named .csv but looks like a binary file, not text.",
                       "Export the sheet as CSV (comma-separated text) and upload again."),
        "csv_unreadable": ("It is named .csv but cannot be read as text.",
                           "Re-export it as CSV with UTF-8 encoding and upload again."),
        "csv_no_table": ("It is named .csv but contains no table data.",
                         "Check the file has a header row plus data rows, then upload again."),
        "zip_bomb": ("The Office/OpenDocument file expands to an unsafe size.",
                     "Re-save it from the original application and upload again."),
        "zip_too_large": ("The Office/OpenDocument file expands to an unsafe size.",
                          "Re-save it from the original application and upload again."),
        "zip_too_many_files": ("The Office/OpenDocument file contains too many internal files.",
                               "Re-save it from the original application and upload again."),
    }
    message, hint = explanations.get(kind, ("The file content does not match its type.",
                                            "Re-export the document in the stated format and upload again."))
    return err(415, kind, f"We cannot read this file. {message}", hint)


def upload_incomplete() -> HTTPException:
    return err(409, "upload_incomplete",
               "That file did not finish uploading.",
               "Wait for the upload to finish, then try again. Nothing was saved.")


def presign_unavailable() -> HTTPException:
    return err(409, "presign_unavailable",
               "Direct large-file upload is not configured on this server.",
               "The app will fall back to the standard upload path.")


def storage_unavailable() -> HTTPException:
    return err(502, "storage_unavailable",
               "The file could not be stored.",
               "Your file was not saved. Check the connection and upload it again in a moment.")


def queue_unavailable() -> HTTPException:
    return err(503, "queue_unavailable",
               "The document was stored but could not be queued for extraction.",
               "Nothing was saved. Upload it again in a moment; if it persists, contact the pilot admin.")


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------
def bad_pilot_code() -> HTTPException:
    return err(403, "bad_pilot_code",
               "That pilot code is not correct.",
               "Ask the pilot admin for the current pilot code and try again.")


def bad_invite() -> HTTPException:
    return err(404, "bad_invite",
               "That invite code is not valid.",
               "Check for typos, or ask the pilot admin for a fresh invite code.")


def invite_expired() -> HTTPException:
    return err(410, "invite_expired",
               "This invite code has expired.",
               "Ask the pilot admin for a fresh invite code.")


def invite_used() -> HTTPException:
    return err(409, "invite_used",
               "This invite code has already been fully used.",
               "Ask the pilot admin for a fresh invite code.")


def bad_founder_key() -> HTTPException:
    return err(403, "bad_founder_key",
               "That founder key is not correct.",
               "Ask the person who set up the pilot for the founder key. Manufacturers never need it — it is only for creating company codes.")


def founder_disabled() -> HTTPException:
    return err(503, "founder_disabled",
               "The founder area is not set up on this server.",
               "Ask the person who set up the pilot to add the founder key, then try again.")


# ---------------------------------------------------------------------------
# AI services
# ---------------------------------------------------------------------------
def ai_unconfigured() -> HTTPException:
    return err(503, "ai_unconfigured",
               "The AI service is not connected.",
               "The pilot admin needs to add the AI key. You can keep reviewing manually in the meantime.")


def ai_unavailable() -> HTTPException:
    return err(502, "ai_unavailable",
               "The AI service did not answer.",
               "Your work is safe — wait a little while and try again.")


def intake_failed() -> HTTPException:
    return err(502, "intake_failed",
               "We could not read that email automatically.",
               "Try again in a moment, or create the request manually from the Requests page.")


def bad_status() -> HTTPException:
    return err(422, "bad_status",
               "That status is not allowed — use “open” or “responded”.",
               "Pick one of the two statuses and try again.")


# ---------------------------------------------------------------------------
# Intake webhook / misc operator errors
# ---------------------------------------------------------------------------
def intake_unconfigured() -> HTTPException:
    return err(503, "intake_unconfigured",
               "Automatic email intake is not set up.",
               "Contact the pilot admin, or paste the email manually on the Notifications page.")


def bad_intake_secret() -> HTTPException:
    return err(403, "bad_intake_secret",
               "The email sender was not recognised.",
               "Contact the pilot admin — the mail integration secret does not match.")


def bad_request_body() -> HTTPException:
    return err(422, "bad_request_body",
               "That request arrived in a form we do not understand.",
               "Reload the page and try again. If it persists, contact the pilot admin.")
