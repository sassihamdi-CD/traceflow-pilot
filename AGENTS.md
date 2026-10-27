# TraceFlow — session handoff / agent notes

_Last updated: 2026-10-26._

## Where things are

- **Active repo:** `/home/sassi/Desktop/traceflow-pilot`
  - `origin` = `https://github.com/MindScript-IT/traceflow-pilot.git` (**READ-only** for this account)
  - `fork` = `git@github.com:sassihamdi-CD/traceflow-pilot.git` (**write**; `master` is current)
- **Old repo:** `/home/sassi/Desktop/tracflow` — do not edit; it is an unrelated earlier snapshot.
- **User repo (all work pushed):** https://github.com/sassihamdi-CD/traceflow-pilot
  - `master` = `b752f43` (plus any later commits)
- **Organization PR:** https://github.com/MindScript-IT/traceflow-pilot/pull/2
  - Contains 3 commits: review fixes, gated demo mode, README summary.
  - Direct push to the org repo is blocked (`permissions.push=false`); use the fork + PR.
- **Latest commits:**
  - `b752f43` Document the pilot hardening fixes in the README
  - `3d706e1` Add gated local demo mode for full-flow testing
  - `a0630a6` Fix pilot extraction/passport integrity and deployment health
  - base: `f805160`

## What was delivered

A senior review of passport mode + document upload/extraction, with fixes:

- Removed Next.js direct-DB routes (founder/invites/auth); all writes go through FastAPI.
- Founder page uses server-only `X-Founder-Key`.
- Seat-safe invite redemption; uniqueness races return `409 conflict`.
- Exactly one verified value per product/field, enforced in routes and by
  `migrations/019_one_verified_per_field.sql`.
- Public passport exposes verified evidence only; verified `product_name`/`sku`
  override product columns.
- PDF page refs normalized; CSV encodings (UTF-8/Windows-1252) and delimiters
  (`;`, tab, pipe); integral XLSX floats; dedupe/caps.
- Full per-document transcription for PDFs and spreadsheets.
- Retry failed documents; worker stale-job recovery + partial-proposal cleanup;
  storage/queue errors map to 502/503; orphan upload cleanup.
- Anthropic 429/5xx/network retries; `temperature=0`.
- SSR uses `API_URL_INTERNAL`; localized passport metadata and 404.
- Web/worker healthchecks fixed; app-role grants + append-only audit triggers.
- `.env.example` fixed (seeded workspace UUID, clean values, Company B vars).

Full findings + residual risks: `docs/PILOT_REVIEW.md`.
Local browser testing: `docs/LOCAL_TESTING.md`.
README has a **"Pilot hardening: what was fixed"** section.

## Local runtime (demo mode, currently used for browser testing)

- API `http://localhost:8010`, web `http://localhost:3010`
  (ports 8000/3000 were taken by older stacks).
- Services: `api`, `db`, `web`, `worker`, one-shot `migrate`.
- Start/check:
  ```bash
  docker compose up -d --build
  docker compose ps
  curl -s localhost:8010/ready
  ```
- The local `.env` (git-ignored) has:
  - `DEMO_MODE=true`
  - `DEMO_STORAGE_DIR=/srv/app/uploads`
  - `NEXT_PUBLIC_DEMO_MODE=true`
  - placeholder Supabase/Anthropic/R2 keys
- Demo mode bypasses only the adapters: Supabase auth -> fixed `demo-reviewer`,
  R2 -> shared `uploads` volume, Anthropic -> deterministic label/value
  extraction. The real flow (upload -> queue -> worker -> field_values ->
  review -> publish -> passport) is unchanged.
- **Never enable demo mode in production.** To run with real keys:
  1. Set `DEMO_MODE=false` and `NEXT_PUBLIC_DEMO_MODE=false`.
  2. Fill `SUPABASE_*`, `ANTHROPIC_API_KEY`, `R2_*`, `DB_APP_PASSWORD`,
     `FOUNDER_KEY`.
  3. Rebuild web (`docker compose up -d --build`) — `NEXT_PUBLIC_*` is baked.

### Test data created locally

- `DEMO-001` / "Demo Trail Runner" — verified, unpublished.
- `DEMO-PDF` / "Demo PDF Sandal" — published; passport slug `5sbxdCsURdhO`.
- `DEMO-REVIEW` / "Demo Review Sneaker" — 7 proposed values in the review queue.
- `DEMO-UI` / "Demo UI Boot" — empty, for testing uploads.
- Sample uploads: `samples/demo-shoe.csv`, `.xlsx`, `.pdf`.
- Passport: http://localhost:3010/passport/5sbxdCsURdhO

## Verification

- `pytest -q` -> **127 passed, 2 skipped**.
- `docker compose ps` -> api/db/web healthy, worker up.
- CSV, XLSX and PDF uploads exercised end-to-end through the worker, review,
  publish and public passport.
- Not yet exercised: real Anthropic extraction, real R2 round-trip, real
  Supabase sign-in (local keys are placeholders).

## Next steps

1. Review/merge PR #2 (or grant write access for direct org pushes).
2. Run one real-key pass: Supabase login, R2 upload, Anthropic extraction.
3. Address residual risks listed in `docs/PILOT_REVIEW.md` (invite entropy,
   unpublish, rate limiting, PDF page-count validation, multi-worker leases,
   Supabase bootstrap ordering).

## Conventions / gotchas

- Python venv: `.venv`; tests: `.venv/bin/python -m pytest -q`.
- Do **not** commit `.env` or any secret; `.env.example` has placeholders only.
- Old port-clash stacks may still be running; use 8010/3010 for this project.
- `TRUNCATE audit_log` is blocked by design (append-only triggers).
- `docker compose up` uses the local `.env`; changing `NEXT_PUBLIC_*` requires
  a web rebuild.

## Live Vercel pilot fix (2026-10-21)

The founder's pilot is running both apps on Vercel (Hobby):
- Web: `https://traceflow.mindscript.tech` (project `traceflow-pilot-n5k6`)
- API: `https://traceflow-pilot-o8o7.vercel.app` (project `traceflow-pilot-o8o7`)

Fixed live:
- CORS allowlist now includes the custom domain (the original Safari
  "Load failed" on every authenticated call).
- Production Supabase was missing migrations 004-019; all applied.
- `WORKSPACE_ID` had no `workspaces` row; created it.
- Supabase session pooler exhausted (`EMAXCONNSESSION`); `app/db.py` now
  rewrites `:5432` to the transaction pooler `:6543`, uses `max_size=2`, and
  disables prepared statements.
- Anthropic SDK 1.17 rejects `temperature`; removed from ask/extraction.
- Added `POST /api/worker/tick` (header `X-Worker-Secret`) and scheduled it
  every minute with Supabase `pg_cron` + `pg_net`, because Vercel cannot run
  `scripts/worker.py`. Automatic CSV extraction verified in <60s.
- Web ask timeout raised to 90s; uploads >4.4 MB now show a clear message
  instead of Vercel's CORS-less 413 (true fix = presigned R2 uploads).
- End-to-end verified: CSV upload -> auto extraction -> accept -> publish ->
  public passport (API + SSR). `pytest -q` = 127 passed, 2 skipped.

Test data (`ZZ-*` products, test membership) was deleted. Test Supabase user
`tf-agent-test-1792612493@example.com` still exists but has no membership.
Temp `/api/ops/*` endpoints and `OPS_SECRET` are removed.

Uncommitted local changes: `app/db.py`, `app/config.py`, `app/main.py`,
`app/routes/worker.py`, `app/services/{ask,extraction,document_items}.py`,
`scripts/worker.py`, `scripts/__init__.py`, `vercel.json`, web ask/upload
timeouts, `web/vercel.json`, `.env.example`, `VERCEL_DEPLOYMENT.md`.

## 2026-10-22: real PDF extraction + publish UX

The founder's two PDFs were stored in R2 but stuck (`failed`/`pending`) with
0 passport values. Root cause: the full per-document transcription call
(12k output tokens) exceeded Vercel's 60s function limit; `recover_stale`
then requeued/failed the job after 15-minute windows.

Fixed live:
- `scripts/worker.py`: `process_one(conn, light=False, stale_minutes=15)`.
  The serverless tick calls `light=True` for PDFs, which skips the full
  transcription + parties calls and only maps passport fields. The standalone
  worker still does the full pipeline.
- `app/routes/worker.py`: one job per tick (a 26-page PDF can use the whole
  budget), stale window 2 minutes for serverless.
- Verified: 26-page Burberry PDF -> `done` in 6.2s; 4-page HIPREN PDF ->
  `done` with 2 passport values. Both founder documents are `done`.
- PDFs processed by the serverless tick have no `document_items`; the product
  page now says "Passport fields only" instead of "All extracted values".
- New `POST /api/products/{product_id}/fields/{field_key}/manual` adds a
  reviewer-entered accepted value for a missing field (one-verified-per-field
  still enforced; audit `field.manual`).
- New `POST /api/products/{product_id}/fields/{field_key}/replace` replaces an
  already-verified value: old rows are rejected (history kept), a new
  `corrected` row becomes the one verified value (audit `field.replaced`).
  The product page exposes it as "Edit verified value" on every verified field.
- Product page now shows a "N required fields still need a value" banner with
  anchors, inline manual entry per missing field, a "Publish blocked · N
  missing" label, a retry button for failed documents, and a "Manual entry"
  source label. `document_items` count comes from `product_detail`.
- `GET /api/worker/status` (same `X-Worker-Secret`) returns recent documents,
  latest jobs/attempts/errors, and the pg_cron job state.
- `pytest -q` = 127 passed, 2 skipped. Web + API deployed to production.

Founder product `c8278fe4-e838-4ccb-87f5-34e8f9fcf796` (SKU `VR-2026-RT`,
slug `NBrd_wHJnKC9`) now has 7 verified values, but five look like UI test
entries: `upper_material`=33, `upper_composition`=125, `leather_origin`=12,
`sole_material`=155, and `product_name`="HIPREN" accepted from the AI proposal.
"Hipren" is likely the supplier from the declaration, not the product name.
SKU was corrected from "12" to `VR-2026-RT` through the new replace flow as a
live check. Do not publish until the founder replaces the test values with
real ones using "Edit verified value"; no re-upload is needed.

Still true: no long-running worker host, so full PDF transcription is off on
Vercel. A real worker (Railway/Render/Fly) restores it. Diagnostic reviewer
`tf-diag-1792663074@example.com` is still a member for testing; clean it up
when no longer needed.

Uncommitted local changes now also include `app/routes/fields.py`,
`app/services/detail.py`, `web/app/(console)/products/[id]/page.tsx`, and
`web/messages/{en,it}.json`.

## 2026-10-26: universal ingestion + grounded extraction

Founder demo exposed two failures: unfamiliar documents produced invented
passport values, and only PDF/XLSX/CSV were accepted. Fixed locally (not yet
committed or deployed):

- **Universal parser** `app/services/document_ingest.py`: PDF, DOCX, XLSX/
  XLSM/XLS, CSV/TSV, ODS/ODT/ODP, PPTX, EML, TXT/MD/RTF/HTML/XML/JSON,
  PNG/JPG/WEBP/GIF/TIFF/BMP. Legacy `.doc` is rejected with a "save as .docx
  or PDF" hint. Scanned PDFs are sliced for vision (≤12 pages).
- **Grounded extractor** `app/services/grounded_extraction.py`: phase 1
  transcribes every labelled value/row with `evidence`; phase 2 maps fields
  only from transcribed item ids. Text-document items are checked against the
  parsed source and dropped if the value/evidence is absent. Chunked
  (24k chars, ≤80 chunks, concurrency 4, ≤8k items), version `grounded-v1`.
- **Large uploads**: `POST /api/products/{id}/documents/presign` → browser
  PUT to R2 → `.../documents/complete`. Multipart fallback. Caps: 64 MB PDF,
  32 MB Office/CSV, 20 MB images, 16 MB text, 100 MB ceiling. Requires R2
  bucket CORS for `PUT` + `Content-Type` from the web origin.
- **Worker**: parses any format, stores document metadata/evidence, supports
  `--concurrency N` / `WORKER_CONCURRENCY` (max 16) with
  `FOR UPDATE SKIP LOCKED`. Serverless tick stays one job/tick and light mode.
- **DB**: `migrations/020_universal_ingestion.sql` (document metadata,
  `document_items.evidence/page_number`, `field_values.evidence`, indexes);
  add to `scripts/migrate.sh` (done). Apply to production Supabase before
  deploying the new worker/API.
- **Deps**: `python-docx`, `python-pptx`, `xlrd`, `odfpy`, `Pillow` added to
  `requirements.txt`.
- **Ask**: snapshot now includes documents + up to 400 document items, so Ask
  can answer about arbitrary files; system prompt forbids inventing values.
- **UI**: upload accepts all formats + large-file presign; product page shows
  document type, pages, size, scan flag, language and summary; en/it copy
  updated.
- **Tests**: new `tests/test_universal_ingest.py`; old cap/format tests
  updated. `.venv/bin/python -m pytest -q` = **135 passed, 2 skipped**.
  `npx tsc --noEmit` (web) passes.

Local Docker was rebuilt with migration 020 applied. A DOCX upload to DEMO-UI
flowed through the worker (`grounded-v1`, 8 items, 7 proposals); an unrelated
invoice DOCX produced 0 passport proposals. Test rows/files were removed.

Still to do: apply migration 020 + set R2 CORS on production; deploy API/web/
worker; run one real-key format pass (DOCX/ODT/scan) against the live pilot.

## 2026-10-26 (later): hard stress test + hosting decision

Added reusable local harnesses:

- `scripts/stress_ingestion.py` — real API/storage/queue/worker/DB run.
  Profiles `smoke|load|extreme|large|boundary`; products use `STRESS-*`.
- `scripts/stress_pipeline.py` — real grounded chunking/grounding/mapping with
  a fake model, optional latency and injected 500s.

Results (local Docker, demo extraction, 15 GiB host):

| Profile | Docs | Items | Drain | Worker peak |
| --- | ---: | ---: | ---: | ---: |
| smoke | 10 | 10,888 | 6-49 s* | ~140 MiB |
| load @4 | 29 | 40,530 | 20.8 s | ~170 MiB |
| extreme @4 | 34 | 50,530 | 38.6 s | ~249 MiB |
| large @3 (12 MB TXT + 30 MB CSV + 1M-cell XLSX) | 3 | 6,000 | 14.4 s | ~214 MiB |
| boundary @3 (65.5 MB total) | 13 | 10,888 | 2.1 s | ~150 MiB |

\*The host also runs several unrelated local stacks; identical smoke runs
took 6 s when idle and 49 s at load average ~10. Use the item/failure/memory
numbers, not absolute timings, as the benchmark.

Boundary rechecked: 65 MB PDF and zip bomb rejected at upload (413/415),
corrupt PDF fails cleanly in the worker, 10/13 processed. Pipeline harness: 12
mixed docs / 572 fake calls / 12.2 s / 211 MiB; 8 docs with 18 injected upstream
failures had 0 failed documents.

Fixes made because of the run:

- CSV parsing is now streamed with the stdlib `csv` reader instead of pandas.
  The 30 MB / 3.6M-cell case fell from ~541 MiB to ~214 MiB for three
  concurrent large documents; `pandas` removed from `requirements.txt`.
- Field-mapping failures now set `truncated=True` instead of being a silent
  warning, so missing fields trigger a retry.
- `document_items` / `field_values` inserts are batched with `executemany`
  (one round trip per document instead of up to 2,000). A 507-item store fell
  from 1.34 s to 0.55 s locally; this matters under DB latency.
- Long jobs send a 60 s heartbeat on an autocommit connection; without it
  `recover_stale()` could requeue a live full transcript and process it twice.
- Existing fixes re-verified: zip-bomb guard, text/PDF budgets, commit before
  LLM work, `FOR UPDATE SKIP LOCKED` concurrency.

Tests: `.venv/bin/python -m pytest -q` = **140 passed, 2 skipped**;
`npx tsc --noEmit` (web) passes. Local stress products/files were deleted; the
standard one-worker compose stack was restored.

Hosting decision (details in `docs/HOSTING_OPTIONS.md`): keep web on Vercel;
run the full worker on Railway or Fly.io with Supabase + R2, and move the
FastAPI app there too if Ask/upload endpoints hit Vercel limits. Disable the
pg_cron tick once the real worker is live — the light tick can mark a PDF done
before full transcription. Apply migration 020 and set R2 CORS first.

Stress details, caveats and production prerequisites: `docs/STRESS_TESTING.md`.

Still not measured: real Anthropic/R2/Supabase latency, cost, and long-PDF
behaviour under a real key. Nothing was committed, pushed, migrated or deployed
in production during the stress work.
