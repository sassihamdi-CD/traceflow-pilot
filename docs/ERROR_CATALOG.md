# TracFlow Error Catalog — what each message means and what to do

Every API failure returns the same shape (see `app/errors.py`):

```json
{"detail": {"code": "publish_blocked", "message": "…", "hint": "…", "data": {}}}
```

- **message** — what happened, in plain words. Never a class name or number.
- **hint** — the next step. Follow it before contacting support.
- The screen always shows `message` + `hint`, never raw codes or JSON.

## Sign-in and access

| code | You see | What to do |
|---|---|---|
| `session_missing` | You are signed out. | Sign in again — drafts in this browser are kept. |
| `session_invalid` | Your session has expired. | Sign in again — drafts in this browser are kept. |
| `auth_unavailable` | We could not reach the sign-in service. | Check your connection, retry in a moment. Nothing was changed. |
| `auth_unconfigured` | Sign-in is not set up on this server. | Contact the pilot admin (server setting). |
| `membership_unavailable` | We could not check your workspace access. | Retry in a moment. Nothing was changed. |
| `not_member` | Your account is not a member of this workspace yet. | Redeem your invite code on the login page, then retry. |
| `forbidden_role` | Your account could not be authorised for this action. | Pilot = one role: every member can do everything (incl. publishing). Sign out/in; if it persists, ask the pilot admin to check your membership. |
| `workspace_missing` | This workspace is not set up on the server yet. | Contact the pilot admin (provisioning). |

## Passport publishing

| code | You see | What to do |
|---|---|---|
| `publish_blocked` | The passport cannot be published yet — N still need verification: *names…*; … | Accept or correct a verified value for each listed field (re-upload fresh documents where expired), then publish again. Field **names** are shown, never internal codes. |

## Reviewing values

| code | You see | What to do |
|---|---|---|
| `already_decided` | This value was already accepted/rejected… | Refresh the page to see its current state. |
| `not_candidate` | That proposal is no longer open for decision. | Refresh — it was already decided or replaced. |
| `not_found` | That product / document / … could not be found. | It may be removed or in another workspace. Refresh the list. |

## Uploading documents

| code | You see | What to do |
|---|---|---|
| `file_too_large` | This file is too big — the limit is N MB. | Compress, or split into smaller documents, and upload again. |
| `unsupported_type` | We cannot read “name” — lists the supported formats… | Convert to PDF, Word (.docx), Excel, CSV, OpenDocument, PowerPoint, email, text or an image. |
| `not_a_pdf` | …named .pdf but is not a real PDF file. | Re-export as PDF and upload again. |
| `not_a_workbook` | …named .xlsx but is not a readable Excel workbook. | Re-save from Excel as .xlsx and upload again. |
| `csv_binary` / `csv_unreadable` | …named .csv but is not readable text. | Re-export as CSV (UTF-8 text) and upload again. |
| `csv_no_table` | …named .csv but contains no table data. | Check for a header row plus data rows, then upload again. |
| `missing_field` | A required piece of information is missing: … | Fill it in and try again. |
| `too_long` | That … is too long (maximum …). | Shorten it and try again. |
| `nothing_to_update` | There is nothing to save — every field was left empty. | Change at least one field, then save. |

## Invites

| code | You see | What to do |
|---|---|---|
| `bad_pilot_code` | That pilot code is not correct. | Ask the pilot admin for the current code. |
| `bad_invite` | That invite code is not valid. | Check for typos, or ask for a fresh code. |
| `invite_expired` | This invite code has expired. | Ask the pilot admin for a fresh code. |
| `invite_used` | This invite code has already been fully used. | Ask the pilot admin for a fresh code. |

## AI services (extraction, Q&A, intake)

| code | You see | What to do |
|---|---|---|
| `ai_unconfigured` | The AI service is not connected. | The pilot admin must add the AI key. Manual review still works. |
| `ai_unavailable` | The AI service did not answer. | Your work is safe — retry shortly. |
| `intake_failed` | We could not read that email automatically. | Retry, or create the request manually from Requests. |
| `intake_unconfigured` | Automatic email intake is not set up. | Paste the email manually on Notifications, or contact the admin. |
| `validation` | Some information is missing or invalid (field). | Check the highlighted fields and try again. |

## Requests

| code | You see | What to do |
|---|---|---|
| `bad_status` | That status is not allowed — use “open” or “responded”. | Pick one of the two and retry. |
| `bad_request_body` | That request arrived in a form we do not understand. | Reload and retry; contact the admin if it persists. |

## Founder area (`/founder` — never shown to manufacturers)

| code | You see | What to do |
|---|---|---|
| `bad_founder_key` | That founder key is not correct. | Ask whoever set up the pilot for the founder key. Manufacturers never need it. |
| `founder_disabled` | The founder area is not set up on this server. | Ask whoever set up the pilot to add the founder key. |
| `bad_intake_secret` / `intake_unconfigured` | The email sender was not recognised / intake is not set up. | Contact the pilot admin (mail integration). |
