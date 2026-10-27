# TracFlow Per-Button Audit Checklist (production sign-off)

Generated 2026-10-07. Every interactive element in `web/` mapped to its backend handler.
Status legend: `[ ]` todo · `[x]` verified · `[!] ` known defect (must fix before pilot).
How to use: run each row manually against a seeded company DB, tick + record actual result.

## A. Auth / session / account

| # | UI location | Button/action | Expected API + result | Audit status / defect |
|---|---|---|---|---|
| A1 | `app/page.tsx:19,35` | "Reviewer sign in" / "Open reviewer console" | Navigate `/login` | `[ ]` verify both land on `/login` |
| A2 | `app/login/page.tsx:103-117` | Tab Sign in / Create account toggle | Local state only, clears error | `[ ]` |
| A3 | `app/login/page.tsx:142-145` | Submit Sign in | `supabase.auth.signInWithPassword` → `push(/products)` + `refresh()` | `[ ]` wrong password shows inline brick error |
| A4 | `app/login/page.tsx` | Submit Create account (email + password + ONE company code) | `supabase.auth.signUp` → auto-`POST /api/invites/redeem` → lands in `/products` as member | `[x]` verified live 2026-10-07: single-step, no 403s. No-session signup shows “check email”, never pushes. |
| A5 | `app/login/page.tsx` | Fallback redeem box (signed in, not yet member) | `POST /api/invites/redeem {code}` → friendly message + hint (never raw) | `[x]` primary path is auto-redeem at signup (A4); box kept only for edge cases. Seed `TF-133C98` revoked (migration 014). |
| A6 | `components/Sidebar.tsx:106-111` | Sign out | `supabase.auth.signOut()` → `push(/login)` | `[!]` no resume persistence — last stage/drafts lost (see H). Must preserve `localStorage` resume keys across sign-out |
| A7 | `app/(console)/account/page.tsx:79-85` | Set new password | `supabase.auth.updateUser({password})`, client min-8 + match check | `[ ]` verify mismatch/short/too-weak messages; no forgot-password flow (intentional pilot scope) |
| A8 | `app/(console)/account/page.tsx:127-129` | Save signature | `localStorage tf_email_signature` only (no API) | `[ ]` only localStorage use in app — confirm per-browser, private-mode swallow OK |

## B. Dashboard (`/products`)

| # | Button/action | Expected API + result | Audit status / defect |
|---|---|---|---|
| B1 | Page load | `GET /api/products` → dossier list + readiness + next_action | `[ ]` verify empty → "No dossiers yet", error → brick box |
| B2 | "New dossier" toggle `products/page.tsx:105-110` | Local show/hide form | `[ ]` |
| B3 | "Create & open dossier" `products/page.tsx:138-140` | `POST /api/products {sku,name,manufacturer_name,category:footwear}` → `window.location.href=/products/{id}` | `[!]` no empty-string guard (backend `schemas.py:6-10` allows `""`); duplicate SKU is warning-banner only `products/page.tsx:115-126`, backend allows dupes |
| B4 | Dossier row click `products/page.tsx:172` | `Link /products/{id}` | `[ ]` |
| B5 | Verification mix `Donut.tsx:12` | Pure render of `agg.counts` | `[!]` `\|\|1` shows "1 fields" when empty — misleading |

## C. Dossier (`/products/[id]`) — core review surface

| # | Button/action | Expected API + result | Audit status / defect |
|---|---|---|---|
| C1 | Page load | `GET /api/products/{id}` → fields + documents + readiness | `[ ]` skeleton pulse, error inline |
| C2 | Auto-poll while extracting `products/[id]/page.tsx:90-95` | `setInterval(load,4000)` while any `pending\|extracting` | `[!]` reloads whole detail, clobbers in-progress `edit[]` inputs + notice/error; no backoff/visibility guard |
| C3 | Rename toggle + Save `page.tsx:261-282` | `PATCH /api/products/{id} {name,sku,manufacturer_name filtered non-empty}` → "Dossier renamed" | `[ ]` verify empty-submit just closes; `422 Nothing to update` path |
| C4 | "Accept all N proposals" `page.tsx:285-289` | `POST /api/products/{id}/accept-all` → `Accepted N + skipped_conflicts` | `[ ]` verify conflicts skipped, bulk audit rows written |
| C5 | Accept (per value) `page.tsx:400` | `POST /api/fields/{valueId}/accept` | `[ ]` 409 if already decided; race → two accepted = `conflicting` display only (no DB constraint) |
| C6 | Reject (per value) `page.tsx:401` | `POST /api/fields/{valueId}/reject` | `[ ]` same 409/race note |
| C7 | "Resolve conflict with this" `page.tsx:403-405` (only when `conflicting`) | `POST /api/products/{id}/fields/{key}/resolve-conflict {chosen_field_value_id}` | `[ ]` verify others → rejected, audit `field.conflict_resolved` |
| C8 | "Edit & accept" `page.tsx:415-418` | `POST /api/fields/{valueId}/correct {value}` (new `corrected` row, old → rejected, `supersedes_id`) | `[!]` empty input → inline "Type the corrected value first" (good); backend `CorrectBody.value=""` allowed — server must reject empty |
| C9 | Supplier quick "Add supplier" `page.tsx:336-342` | `POST /api/suppliers {name}` → refresh list → pre-select for field | `[ ]` empty name → "Type the supplier name first" |
| C10 | Supplier select / Change `page.tsx:350-363` | Local `askSupplier[fieldKey]` state | `[ ]` auto-supplier (single linked doc) shown as "To: X · auto" |
| C11 | "Request" (follow-up) `page.tsx:365-368` | `POST /api/suppliers/{id}/followups {product_id,field_key}` → "saved to follow-ups" + green saved box | `[ ]` no-supplier → "No supplier to ask yet" error |
| C12 | "Write email" `page.tsx:369-372` | Local `draftSupplierEmail()` + `EmailDraftModal` (no API, no send) | `[!]` `navigator.clipboard` unguarded — throws on HTTP/denied permission (same in `EmailDraftModal.tsx:36-40`, `StageGaps.tsx:69-80`, `StageRespond.tsx:48-49`) |
| C13 | Email modal Copy + Close `EmailDraftModal.tsx:53-78` | `clipboard.writeText(Subject+body)`, Esc/backdrop close | `[ ]` verify edit-subject/body persists per open; add clipboard fallback |
| C14 | Upload via dropzone `page.tsx:112-139` + `UploadDropzone.tsx:9-55` | `POST /api/products/{id}/documents` FormData per file (sequential), then `load()` | `[!]` BYPASSES `apiAuthed` — raw `getSession()` token, no refresh retry, no 15s abort, no SESSION_EXPIRED mapping; per-batch "X failed; Y stored" overwrites granular Network error; 25MB+ → expect 413, exe/fake-pdf → 415, binary CSV → 415 |
| C15 | Source doc `<details>` expand `page.tsx:434-445` | `GET /api/documents/{id}/items` via `DocumentItems.tsx:22` → Label\|Value\|Unit\|Location table | `[ ]` verify extracting/done/failed badge + `extracted_count`; failed shows `extraction_error` (currently class-only, low-signal) |
| C16 | "Publish passport" | `POST /api/products/{id}/publish` (PILOT: any member — single role) → human 422 while fields unverified | `[x]` verified live 2026-10-07: reviewer gets “cannot be published yet — 6 still need verification: Product name, SKU, …” + next action. No 403. Gate blocks unverified / pending-review / out-of-date / rejected-unreplaced. |
| C17 | "View public passport" `page.tsx:476` | `Link /passport/{public_slug}` (no auth) | `[ ]` verify unpublished slug → 404, published shows verified-only `{label,value,unit}` |

## D. Request workspace (`/requests`, `/requests/[id]/[stage]`)

| # | Button/action | Expected API + result | Audit status / defect |
|---|---|---|---|
| D1 | Requests load `requests/page.tsx:26-27` | `GET /api/requests` + `GET /api/products` (dossier select) | `[ ]` |
| D2 | "New request" toggle + mode tabs `requests/page.tsx:63-85` | Local `showForm`, `mode=new\|existing` | `[ ]` |
| D3 | "Open request" submit `requests/page.tsx:137-140` | `POST /api/requests {requester_name,org,subject,message + product_id\|new_product}` → `href=/requests/{id}/request` | `[!]` backend has no length/category caps; missing `product_id`+`new_product` → 422 must surface |
| D4 | Request row click `requests/page.tsx:148` | `Link /requests/{id}/request` | `[ ]` |
| D5 | Stage tabs 01-06 `RequestWorkspace.tsx:77-93` | `Link /requests/{id}/{request,structure,evidence,verify,gaps,respond}` | `[!]` progress is positional (`done=i<activeIdx`) — back-nav marks later stages undone regardless of verification |
| D6 | Stage 01 "Open the original message" `StageRequest.tsx:28-33` | Local modal open/close | `[ ]` |
| D7 | Stage 02 upload `StageStructure.tsx:24-45` | Same upload endpoint + optional `supplier_id` FormData | `[!]` same raw-token bypass as C14; failures SILENT (`ok++` only), notice claims success even when `ok==0`; no auto-refresh (unlike dossier) |
| D8 | Stage 02 doc→supplier link select `StageStructure.tsx:128-132` | `PATCH /api/documents/{docId} {supplier_id\|null}` | `[ ]` verify link + unlink (empty → null) paths, 404 on cross-workspace supplier |
| D9 | Stage 02 "Add party as supplier" pill `StageStructure.tsx:144-150` | `POST /api/suppliers {name}` then `PATCH /api/documents/{docId} {supplier_id}` | `[ ]` known-name pills render static (no button); unknown render add-button |
| D10 | Stage 02 "See the evidence…" `StageStructure.tsx:194-197` | `Link /requests/{id}/evidence` | `[ ]` |
| D11 | Stage 03 "Review the proposals" `StageEvidence.tsx:66-69` | `Link /requests/{id}/verify` | `[ ]` read-only table — verify `—`/`Not provided` for missing, `StateBadge` correct |
| D12 | Stage 04 Accept/Reject/Edit/Resolve `StageVerify.tsx:81-103` | Same field endpoints as C5-C8 | `[ ]` same 409/race notes; "Type the corrected value first" inline |
| D13 | Stage 04 "Request clarification" `StageVerify.tsx:111-114` | `POST /api/suppliers/{id}/followups {product_id,field_key}` | `[!]` no-supplier → "Pick the supplier…" error (good); backend `supplier_followups.product_id/field_key NOT NULL` but route allows omission → 500 path must be closed |
| D14 | Stage 05 gap item toggle `StageGaps.tsx:135-150` | Local `picked[]` multi-select | `[ ]` |
| D15 | Stage 05 "Save follow-up draft" `StageGaps.tsx:153-156` | `POST /api/suppliers/{id}/followups {product_id,field_key:picked[0],recipient,subject,items:picked[]}` | `[!]` COLLAPSES multi-pick: only `picked[0]` in `field_key`, rest in `items[]` — confirm backend stores all `items` (single call per field vs one call with items array) |
| D16 | Stage 05 "Copy" prepared follow-up `StageGaps.tsx:169-172` | `clipboard.writeText(To/Subject/items)` | `[!]` unguarded clipboard (see C12) |
| D17 | Stage 06 "Copy response" `StageRespond.tsx:128-131` | Local `letterText()` → clipboard | `[!]` unguarded clipboard; verify letter lists verified + `Not provided` + outstanding |
| D18 | Stage 06 "Download .txt" `StageRespond.tsx:132-135` | Local Blob download `response-{sku}.txt` | `[ ]` |
| D19 | Stage 06 "Load public passport preview" `StageRespond.tsx:143-146` | `GET {API}/api/public/passport/{slug}` (no auth) | `[!]` `catch{setDpp(null)}` shows "Not published yet" even on network/500 — indistinguishable states |
| D20 | Stage 06 "Publish passport" `StageRespond.tsx:161-164` | Same `POST publish` as C16 | `[!]` same admin-only 403 + gate gaps |
| D21 | Stage 06 "Open public passport" `StageRespond.tsx:166-169` | `Link /passport/{slug}` | `[ ]` |
| D22 | **MISSING BUTTON — request status** | `POST /api/requests/{request_id}/status {open\|responded}` exists in `app/routes/requests.py:109` but NO UI calls it | `[!]` request can never become `responded`; workspace header `RequestWorkspace.tsx:138` stuck on "○ draft". Must add "Mark responded / Reopen" button with audit `request.status` |

## E. Review queue / Ask / Notifications / Suppliers / Activity

| # | Button/action | Expected API + result | Audit status / defect |
|---|---|---|---|
| E1 | Review load `review/page.tsx:21` | `GET /api/review-queue` FIFO | `[ ]` unbounded (no pagination) — load-test with 500+ proposals |
| E2 | Review Accept/Reject `review/page.tsx:124,133` | `POST /api/fields/{id}/accept\|reject` per row, `busy=id` disabled | `[ ]` |
| E3 | Review "Accept all N unambiguous" `review/page.tsx:75-78` | `POST /api/review-queue/accept-all` → skips conflicts | `[!]` button label says `items.length` but API skips conflicts — verify "Nothing unambiguous" error path `review/page.tsx:49-51` |
| E4 | Review "Open dossier" `review/page.tsx:106-111` | `Link /products/{id}` | `[ ]` |
| E5 | Ask suggestion chips `ask/page.tsx:57-62` | `POST /api/ask {question}` (chip text) | `[ ]` |
| E6 | Ask submit `ask/page.tsx:108-111` | `POST /api/ask {question}` → appended `{role:ai,answer}`; `maxLength=2000` client | `[ ]` verify `503 AI not configured`, `502 AI unavailable`, `422 Missing/>2000`; chat history NOT persisted (lost on refresh — in resume scope) |
| E7 | Notifications load `notifications/page.tsx:24-29` | `GET /api/notifications?unread_only=false&limit=100` + `unread_count` | `[ ]` |
| E8 | "New intake" toggle `notifications/page.tsx:82-87` | Local show/hide | `[ ]` |
| E9 | "Create intake" `notifications/page.tsx:122-124` | `POST /api/intake {subject,body_text,from_email?}` → "Intake created — N requirements" + Open request link | `[ ]` verify `422 subject>500\|body>50000`, `502 Intake extraction failed`; intake always creates new `INTAKE-xxx` product (spam-duplicate risk, no dedupe) |
| E10 | "Mark all read" `notifications/page.tsx:89-95` | `POST /api/notifications/read-all` → `marked` count | `[ ]` only rendered when `unreadCount>0` |
| E11 | Per-row "Mark read" `notifications/page.tsx:161-167` | `POST /api/notifications/{id}/read` | `[ ]` already-read → `ok` (not 404) per `notifications.py:59-66` |
| E12 | Bell click `NotificationsBell.tsx:27-39` | `Link /notifications`; polls `unread_only=true&limit=1` every 30s | `[!]` sidebar queue badge `Sidebar.tsx:44` fetches only on `path` change — bell vs queue counts diverge |
| E13 | Suppliers "Add supplier" + Save `suppliers/page.tsx:49-66` | `POST /api/suppliers {name,external_code\|null}` → reload | `[ ]` verify health `%` + open_requests cards; empty-list copy |
| E14 | Activity load `activity/page.tsx:25` | `GET /api/activity?limit=100` insert-only trail | `[ ]` verify new accept/correct/upload/publish rows appear newest-first |
| E15 | Landing `/` + passport `/passport/[slug]` | Public, no auth; passport `notFound()` on 404 | `[ ]` non-404 non-OK throws to 500 (no error boundary) — add boundary |

## F. Cross-cutting checks (every row above)

- [ ] Auth: expired token → refresh-once → retry; else `SESSION_EXPIRED` must REDIRECT to `/login` (today only `login/page.tsx:83-86` does; elsewhere raw text).
- [ ] Uploads: PDF/Word/Excel/CSV/OpenDocument/PowerPoint/email/text/image; per-type `413`; fake magic `415`; binary CSV `415`; >4MiB presigned R2 path; re-select same file works (`e.target.value=""`).
- [ ] Timeouts: `apiAuthed` 15s abort → "Request timed out after 15s"; uploads have NO timeout (must add, large PDFs hang).
- [ ] Errors: brick box shows truncated 300ch body; success: moss box; loading: pulse/skeleton.
- [ ] Multi-company (separate DB per company): repeat A–E per company DB; Company A token must 401/404 on Company B URLs; `WORKSPACE_ID` boot check fail-closed.
- [ ] History/resume: refresh mid-edit keeps drafts (today polling C2 wipes them); logout/login and close/reopen land on last `/requests/{id}/{stage}` with drafts restored.
- [ ] Passport core: Burberry PRSL (26pp) → `done,0` (correct empty); BV report → expected fields with real `Page N` / `Sheet / Cell`; HIPREN IT → verbatim, no translate; publish blocked until all 7 required `verified`; public page shows verified-only.

## H. Invite codes — founder page (`/invites`) [verified live 2026-10-07]

| # | Button/action | Expected API + result | Audit status |
|---|---|---|---|
| H1 | Page load | `GET /api/invites` → metadata list (used/max, expiry). Plaintext never stored/shown. | `[x]` shows 3 issued codes with live seat counts, zero failed requests |
| H2 | "Generate code" (company + seats) | `POST /api/invites {prefix,seats,days}` → BIG code + copy message + “shown once” warning | `[x]` `TESTCO-TDZ5QV` minted, 5 seats / 90 days, list refreshed |
| H3 | "Copy message to send" | Clipboard pre-written WhatsApp-style message with code + seats + validity | `[x]` clipboard helper with manual-copy fallback |

Founder answers: (1) new-account creation verified healthy end-to-end (fresh
`mfr-74716@example.com` signup → auto-joined → dashboard with data, no 403s).
(2) The founder NEVER runs scripts: codes are minted on the Invite codes page.
One code per company, shared with everyone there (seats = headcount). Per-company
DB deployments each mint their own code in their own console.

## G. Backend endpoints with NO UI (confirm intentional or add button)

- `POST /api/requests/{id}/status` — NO UI (see D22, must add).
- `POST /api/intake/email` (webhook, `X-Intake-Secret`) — no UI by design; needs signature/replay/dedupe before prod.
- `GET /api/documents/{id}` (single doc meta) — UI uses `/items` only; fine.
- `GET /health`, `/ready` — ops only; wire to deploy healthchecks.
