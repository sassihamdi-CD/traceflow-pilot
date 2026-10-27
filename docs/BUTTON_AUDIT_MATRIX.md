# TracFlow Button Audit — Handler Matrix (code-reading, 2026-10-07)

Source: `docs/BUTTON_AUDIT_CHECKLIST.md` × current working tree (includes the
in-flight hardening workstream: `uuid_or_404`, pydantic non-empty validators,
followup 422 guards, `require_member`, `ensure_workspace_exists` 409,
`apiUploadAuthed`, `copyText` fallback, resume lib, D22 status button,
server-side signup-gate, publish gate v2).

Conventions: `API=$NEXT_PUBLIC_API_URL`, `T=$(supabase session access_token)`.
`422` = FastAPI validation or explicit guard. `404` on bad UUID comes from
`app/schemas.py:9 uuid_or_404` (runs before any DB touch).

## A. Auth / session / account

| # | Click path | Backend handler | Statuses | Manual verify (curl + click) |
|---|---|---|---|---|
| A1 | `/` → "Reviewer sign in" / "Open reviewer console" (`web/app/page.tsx:19,35`) | none (Next `push(/login)`) | — | Click both land on `/login`. |
| A2 | `/login` tab toggle (`web/app/login/page.tsx`) | none (local state) | — | Toggle clears error text. |
| A3 | `/login` Submit Sign in | Supabase `signInWithPassword` → `push(resume‖/products)` + `refresh()` (`web/app/login/page.tsx:84+`) | supabase inline | Wrong password → inline brick error; login → lands last spot. |
| A4 | `/login` Submit Create account | `POST /api/invites/signup-gate` (`app/routes/invites.py:37`) then `supabase.auth.signUp` | 200 ok / 403 bad code / gate open when env empty | `curl -XPOST $API/api/invites/signup-gate -H'Content-Type: application/json' -d'{"code":"wrong"}'` → 403. Click: bad code blocked before signUp; resume redirect after. |
| A5 | `/login` Redeem invite | `POST /api/invites/redeem` (`app/routes/invites.py:52`) | 200 / 404 bad / 409 used / 410 expired | `curl -XPOST $API/api/invites/redeem -H"Authorization: Bearer $T" -d'{"code":"TF-133C98"}'` → 404 (compromised seed removed, migration 014). |
| A6 | Sidebar Sign out (`web/components/Sidebar.tsx:48`) | Supabase `signOut()` → `/login`; keys `tf_last_location` + `tf_draft:*` KEPT | — | Sign out mid-stage → sign in → lands back on same stage with drafts. |
| A7 | `/account` Set new password | Supabase `updateUser({password})`, client min-8 + match | supabase inline | Mismatch/short/weak messages shown. No forgot-password (pilot scope). |
| A8 | `/account` Save signature | none (`localStorage tf_email_signature`) | — | Per-browser only; private-mode swallow OK. |

## B. Dashboard (`/products`)

| # | Click path | Backend handler | Statuses | Manual verify |
|---|---|---|---|---|
| B1 | Page load | `GET /api/products` (`app/routes/products.py:27`, `require_member`) | 200 / 401 / 403 non-member | `curl $API/api/products -H"Authorization: Bearer $T"` → list; empty → "No dossiers yet". |
| B2 | "New dossier" toggle (`products/page.tsx:105-110`) | none (local) | — | Show/hide form. |
| B3 | "Create & open dossier" | `POST /api/products` (`app/routes/products.py:34`) + `ProductCreate` validators (`app/schemas.py:28+`) | 200 / 422 empty sku/name/mfr / 401/403 / 409 unprovisioned ws | `curl -XPOST $API/api/products -H"Authorization: Bearer $T" -H'Content-Type: application/json' -d'{"sku":"","name":"x","manufacturer_name":"y"}'` → 422. Dup SKU → allowed (warning banner only, backend has no unique check). |
| B4 | Dossier row click | `Link /products/{id}` | — | Lands on dossier. |
| B5 | Verification mix (`Donut.tsx:12`) | none (render; `total = … \|\| 1` div-by-zero guard) | — | Empty dossier: confirm ring shows 0/empty, not "1 fields". |

## C. Dossier (`/products/[id]`)

| # | Click path | Backend handler | Statuses | Manual verify |
|---|---|---|---|---|
| C1 | Page load | `GET /api/products/{id}` (`app/routes/products.py:114`) + `uuid_or_404` | 200 / 404 (bad UUID or missing) | `curl $API/api/products/not-a-uuid -H"Authorization: Bearer $T"` → 404. Skeleton pulse, inline error. |
| C2 | Auto-poll while extracting (`page.tsx:90-95`) | same as C1, 4s interval | 200 | Type in edit box during extracting → poll must NOT wipe input (drafts in `product:{id}:edit` localStorage). |
| C3 | Rename toggle + Save | `PATCH /api/products/{id}` (`app/routes/products.py:54`) | 200 / 422 nothing-to-update / 404 | Empty submit just closes; `curl -XPATCH … -d'{}'` → 422. |
| C4 | "Accept all N proposals" | `POST /api/products/{id}/accept-all` (`app/routes/fields.py:149`) | 200 `{accepted, skipped_conflicts}` | Conflicts skipped, bulk audit rows in `/activity`. |
| C5 | Accept (per value) | `POST /api/fields/{id}/accept` (`app/routes/fields.py:24`) | 200 / 404 / 409 decided | Double-accept → 409. `curl -XPOST $API/api/fields/not-a-uuid/accept …` → 404. |
| C6 | Reject (per value) | `POST /api/fields/{id}/reject` (`app/routes/fields.py:49`) | 200 / 404 / 409 | Same as C5. |
| C7 | "Resolve conflict with this" | `POST /api/products/{id}/fields/{key}/resolve-conflict` (`app/routes/fields.py:109`) | 200 / 404 | Others → rejected; audit `field.conflict_resolved`. |
| C8 | "Edit & accept" | `POST /api/fields/{id}/correct` (`app/routes/fields.py:74`) + `CorrectBody` non-empty (`app/schemas.py:36+`) | 200 / 422 empty / 404 / 409 | `curl -XPOST $API/api/fields/<uuid>/correct … -d'{"value":""}'` → 422. |
| C9 | Supplier quick "Add supplier" | `POST /api/suppliers` (`app/routes/suppliers.py:13`) | 200 / 422 empty name | Empty → inline "Type the supplier name first"; `… -d'{"name":""}'` → 422. |
| C10 | Supplier select / Change | local state | — | Single-linked doc shows "To: X · auto". |
| C11 | "Request" (follow-up) | `POST /api/suppliers/{id}/followups` (`app/routes/suppliers.py:64`) | 200 / 422 missing product_id/field_key / 404 supplier-or-product | `…/followups -d'{"field_key":"weight"}'` → 422 (was 500). No-supplier → inline error. |
| C12 | "Write email" | local + `copyText` fallback (`web/lib/clipboard.ts:6`, used `EmailDraftModal.tsx:41`) | — | Copy over plain HTTP / denied permission → textarea fallback, no throw. |
| C13 | Email modal Copy + Close | same as C12 | — | Subject/body edits persist per open; Esc/backdrop closes. |
| C14 | Upload dropzone | `POST /api/products/{id}/documents` (`app/routes/products.py:198`) via `apiUploadAuthed` (`web/lib/api.ts:96`, 60s timeout + refresh) | 200 extracting / 404 / 413 oversize / 415 type / 422 bad UUID | `curl -XPOST $API/api/products/<uuid>/documents -H"Authorization: Bearer $T" -F file=@evil.exe` → 415; 26MB pdf → 413; per-file errors shown, no silent batch success. |
| C15 | Source doc `<details>` | `GET /api/documents/{id}/items` (`app/routes/documents.py:71`) | 200 / 404 | extracting/done/failed badge + `extracted_count`; failed shows `extraction_error`. |
| C16 | "Publish passport" | `POST /api/products/{id}/publish` ADMIN (`app/routes/publish.py:14`) | 200 / 403 reviewer / 404 / 409 unprovisioned / 422 gate | Gate blocks: unverified, verified+`has_new_proposal`, `valid_until` out-of-date, rejected-unreplaced. Reviewer token → 403 (promote via memberships). |
| C17 | "View public passport" | `GET /api/public/passport/{slug}` (`app/routes/public.py:18`, no auth) | 200 verified-only / 404 unpublished | Unpublished slug → 404; published → only `{label,value,unit}` of accepted/corrected. |

## D. Request workspace

| # | Click path | Backend handler | Statuses | Manual verify |
|---|---|---|---|---|
| D1 | `/requests` load | `GET /api/requests` (`app/routes/requests.py:74`) + products list | 200 / 401/403 | Both lists render; dossier select populated. |
| D2 | "New request" toggle + tabs | local | — | showForm / new‖existing modes. |
| D3 | "Open request" submit | `POST /api/requests` (`app/routes/requests.py:16`) | 200 / 404 product / 422 missing fields | `… -d'{"requester_name":"a","subject":"s"}'` (no product/new_product) → 422 surfaced inline. |
| D4 | Request row click | `Link /requests/{id}/request` | — | Lands on stage 01. |
| D5 | Stage tabs 01-06 (`RequestWorkspace.tsx:77-93`) | `GET /api/requests/{id}` (`app/routes/requests.py:103`) | 200 / 404 | Back-nav keeps real verification state (positional `done` = open defect — verify). |
| D6 | Stage 01 modal | local | — | Open/close. |
| D7 | Stage 02 upload (`StageStructure.tsx:24-45`) | same upload as C14 via `apiUploadAuthed` (`StageStructure.tsx:35`) + optional `supplier_id` | 200 / 413 / 415 / 404 | Failures itemized per file; list refreshes (no silent `ok==0` success). |
| D8 | doc→supplier link select | `PATCH /api/documents/{docId}` (`app/routes/products.py:83`) | 200 / 404 supplier-or-doc | Link + unlink (empty→null); cross-workspace supplier → 404. |
| D9 | "Add party as supplier" pill | suppliers.py:13 then products.py:83 | 200 / 422 / 404 | Known names static; unknown render add-button. |
| D10 | "See the evidence…" | `Link evidence` | — | — |
| D11 | Stage 03 read-only table | via dossier detail | — | `—`/`Not provided` + StateBadge correct. |
| D12 | Stage 04 Accept/Reject/Edit/Resolve | same as C5–C8 | 200 / 404 / 409 / 422 | Same guards as dossier. |
| D13 | Stage 04 "Request clarification" | same followup as C11 | 200 / 422 / 404 | No supplier → inline error; omission → 422 (was 500). |
| D14 | Stage 05 gap toggle | local `picked[]` (+ `tf_draft` persist `StageGaps.tsx:41`) | — | Multi-select survives refresh. |
| D15 | Stage 05 "Save follow-up draft" | followup endpoint with `{field_key: picked[0], items: picked[]}` (`StageGaps.tsx:68-79`) | 200 / 422 / 404 | Saved row renders ALL items (`f.items` list) — single call, full array stored. |
| D16 | Stage 05 "Copy" | `copyText` (`StageGaps.tsx:101`) | — | Works over HTTP (fallback). |
| D17 | Stage 06 "Copy response" | `copyText(letterText())` (`StageRespond.tsx:53`) | — | Letter lists verified + `Not provided` + outstanding. |
| D18 | Stage 06 "Download .txt" | local Blob `response-{sku}.txt` | — | File downloads. |
| D19 | Stage 06 passport preview | `apiPublicPassport` (no auth) + `dppError` state (`StageRespond.tsx:83-89`) | null→not-published / 404 null / non-OK throws | Kill API → error state must differ from "Not published yet". |
| D20 | Stage 06 "Publish passport" | same as C16 | 200 / 403 / 422 | Same gate + admin-only. |
| D21 | Stage 06 "Open public passport" | `Link /passport/{slug}` | — | — |
| D22 | "Mark responded / Reopen" (`RequestWorkspace.tsx:167`) | `POST /api/requests/{id}/status` (`app/routes/requests.py:114`) | 200 / 422 bad value / 404 | `…/status -d'{"status":"bogus"}'` → 422; button flips header `○ draft` ↔ `● responded`; audit `request.status`. |

## E. Review / Ask / Notifications / Suppliers / Activity

| # | Click path | Backend handler | Statuses | Manual verify |
|---|---|---|---|---|
| E1 | Review load | `GET /api/review-queue` (`app/routes/ops.py:17`) FIFO | 200 | Load-test 500+ proposals (no pagination — watch latency). |
| E2 | Review Accept/Reject | same as C5/C6 per row | 200 / 409 | `busy=id` disables row. |
| E3 | Review "Accept all N unambiguous" | `POST /api/review-queue/accept-all` (`app/routes/ops.py:46`) | 200 `{accepted_count, skipped_conflicts}` | Label says `items.length` but conflicts skipped → verify "Nothing unambiguous" path. |
| E4 | Review "Open dossier" | `Link /products/{id}` | — | — |
| E5/E6 | Ask chips + submit | `POST /api/ask` (`app/routes/ask.py:14`, `require_member`) | 200 / 422 missing/>2000 / 503 no key / 502 provider down | `… -d'{"question":""}'` → 422; history lost on refresh (resume scope). |
| E7 | Notifications load | `GET /api/notifications` (`app/routes/notifications.py:13`) | 200 | `?unread_only=true&limit=1` bell poll + list render. |
| E8 | "New intake" toggle | local | — | — |
| E9 | "Create intake" | `POST /api/intake` (`app/routes/intake.py:33`) | 200 / 422 length / 502 extraction | `subject>500‖body>50000` → 422; always-new `INTAKE-xxx` product (spam-dedupe risk). |
| E10 | "Mark all read" | `POST /api/notifications/read-all` (`app/routes/notifications.py:71`) | 200 `{marked}` | Only rendered when `unreadCount>0`. |
| E11 | Per-row "Mark read" | `POST /api/notifications/{id}/read` (`app/routes/notifications.py:51`) | 200 (already-read → ok) / 404 | — |
| E12 | Bell click | `Link /notifications` | — | Bell vs sidebar queue badge counts: sidebar refetches only on path change → may diverge. |
| E13 | Suppliers add + save | suppliers.py:13, list `:37` | 200 / 422 | Health % + open_requests cards; empty-list copy. |
| E14 | Activity load | `GET /api/activity` (`app/routes/ops.py:81`) | 200 | New accept/correct/upload/publish rows newest-first. |
| E15 | `/` + `/passport/[slug]` | public.py:18 / landing | 200 / 404→notFound / non-OK→500 (no boundary) | Add error boundary for non-404 passport failures. |

## F. Cross-cutting (every row)

- Auth: `SESSION_EXPIRED` → `redirectToLoginOnSessionExpired` (`web/lib/api.ts:160`) wired in dossier page, RequestWorkspace, StageGaps/Respond/Structure. NOT yet in review/notifications/suppliers/ask/account pages → raw error text there (residual P1).
- Uploads: PDF/Word/Excel/CSV/OpenDocument/PowerPoint/email/text/image (`document_ingest.py`); 100MB absolute + per-type caps (`storage.py`); presigned R2 for >4MiB; `e.target.value=""` reselect (verify per dropzone).
- Timeouts: `apiAuthed` 15s abort; uploads 60s (`apiUploadAuthed` default). Worker path (`scripts/worker.py`) is sole extractor; `BackgroundTasks` removed from upload.
- Errors: brick box truncated 300ch; moss success; pulse/skeleton loading.
- Multi-company: per-company DB; `ensure_workspace_exists` 409 fail-closed (`auth.py:95`); cross-workspace ids → 404 via `uuid_or_404` + workspace-scoped SQL; membership miss → role `none` → 403 (`auth.py:28-93`); DB outage → 503 (never mints rights).
- History/resume: `tf_last_location` + `tf_draft:*` survive sign-out (`resume.ts`, Sidebar, login `resumeOrProducts`); dossier edit drafts + gaps drafts persist; polling no longer wipes edits.
- Passport core: Burberry PRSL → done,0; BV Page N / Sheet-Cell; HIPREN verbatim; publish gate = 7 required verified (see C16).

## G. Backend endpoints with NO UI

- `POST /api/requests/{id}/status` — HAS UI now (D22 button). Closed.
- `POST /api/intake/email` webhook (`intake.py:54`, `X-Intake-Secret`) — no UI by design; still needs provider-signature verification + replay/dedupe before prod (P1).
- `GET /api/documents/{id}` (single meta) — UI uses `/items` only; fine.
- `GET /health` (`main.py:56`), `/ready` (`main.py:61`) — wire to deploy healthchecks; HSTS+CSP headers added (`main.py:76+`).

## Verdict — rows still FAILING (code reading, current tree)

**P0 (pilot blockers): none open.** All checklist `[!]` backend gaps are closed in-tree:
A4 server gate (`invites.py:37` + login calls it), C8 (`schemas.py:36+`),
D13/C11 (`suppliers.py:64-80`), C14/D7 (`api.ts:96` + call sites),
C16/D20 gate v2 (`publish.py:41-74`), D22 button (`RequestWorkspace.tsx:167`),
clipboard (`clipboard.ts` + 3 call sites), resume/A6/F-history (`resume.ts` +
Sidebar + login), compromised seed A5 (`009` unseeded + `014` cleanup),
fail-closed auth (`auth.py:28-93`, `require_member`).

**P1 (fix/verify before scale):**
1. F-auth residual: review / notifications / suppliers / ask / account pages don't call `redirectToLoginOnSessionExpired` → expired token shows raw text instead of `/login`. (cite: no matches in those pages for the helper)
2. `POST /api/intake/email`: stub auth only (`X-Intake-Secret`), TODO signature/attachments/dedupe (`intake.py:72`). Webhook abuse + duplicate products.
3. D5: stage progress still positional (`done=i<activeIdx` per checklist) — back-nav marks verified stages undone. Needs progress-from-verification.
4. E9: intake always mints new `INTAKE-xxx` product — spam/duplicate risk, no dedupe key.
5. B3: duplicate SKU still allowed backend-side (warning banner only, no unique constraint).

**P2 (polish/verify manually):**
6. B5 `Donut.tsx:12` `|| 1` — confirm empty state doesn't read "1 fields".
7. E3 label (`items.length`) vs skipped conflicts — confirm "Nothing unambiguous" path copy.
8. E12 bell vs sidebar queue badge divergence (sidebar refetch on path change only).
9. E15 passport non-404 failure → 500, no error boundary.
10. D19 preview error vs not-published states (now has `dppError` — verify copy differs).
11. E1 review queue unbounded (load-test 500+).

## Tests

`tests/test_button_audit.py` (11 tests, TestClient + `dependency_overrides` +
fake async pool — no keys/DB/network): invalid UUID → 404 (C1/C5), empty
correct → 422 (C8), followup omission → 422 (C11/D13), publish gate → 422
(C16/D20), unknown type → 415 + oversize → 413 (C14/F), status bad value →
422 + valid values reach DB (D22). 2 skips document live-Postgres-only paths
(accept/reject 409 race, upload→extract round-trip). Full suite: **90 passed,
2 skipped**. Also repaired 3 stale tests from the auth/seed renames
(`test_document_items`, `test_intake_notify`, `test_invites`) — tests only,
no prod changes.
