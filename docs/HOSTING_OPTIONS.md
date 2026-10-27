# Hosting options after the pilot stress test

Status: recommendation only; nothing has been deployed by this change.

## Short version

**Keep the Next.js web app on Vercel. Run the extraction worker on a
long-lived container host (Railway or Fly.io), connected to Supabase and R2.
Move the FastAPI app there too if Ask or uploads start hitting Vercel's
request/function limits; otherwise it can stay on Vercel during the pilot.**

The serverless `POST /api/worker/tick` is a stopgap. It processes one light job
per minute, cannot do full PDF transcription, and adds a pg_cron/pg_net
dependency. It should not be the manufacturer-scale path.

## Options

| Option | Web | API | Worker | Verdict |
| --- | --- | --- | --- | --- |
| Current: all Vercel + pg_cron tick | Vercel | Vercel | serverless tick | OK for demo; not for full ingestion |
| **Web Vercel + worker Railway/Fly** | Vercel | Vercel | container | **Recommended next step** |
| API + worker on Railway/Fly | Vercel | container | container | Best once Ask/large endpoints exceed Vercel limits |
| Render free web/worker | Vercel | Render | Render | Free worker spins down; use paid |
| AWS ECS/Fargate | Vercel/CloudFront | ECS | ECS | Works, but more setup than this pilot needs |
| Cloudflare Workers/Containers | CF | CF | CF | Python parsing stack (pypdf/openpyxl/docx) is a poor fit |

## Why not Vercel for the worker

- Hobby request/function limits (roughly 4.5 MB body and 60 s per invocation;
  confirm current limits for your plan) are below a 26-200 page PDF or a
  multi-chunk grounded extraction. The web upload timeout was already raised to
  90 s.
- There is no always-on process, so `scripts/worker.py --concurrency N` cannot
  run there.
- The tick's light mode skips full transcription and only maps passport fields.
- A full worker and the tick can race on the same PDF; the light tick may mark
  it done before full transcription.

## Recommended topology

- **Vercel**: web (`traceflow.mindscript.tech`), SSR, auth callbacks.
- **Railway or Fly.io**: one worker service built from the repo `Dockerfile`,
  command `python scripts/worker.py --concurrency 4`. Start with 1 vCPU / 1 GB.
  Add a second instance or raise to 2 GB if more than ~4 large jobs run at once.
- **Supabase Postgres**: use the transaction pooler (`:6543`) with
  `prepare_threshold=None`. API pool: 2 on serverless, 10-20 on a long-lived
  host (`DB_POOL_MAX_SIZE`). Worker concurrency 4 + 4 heartbeat connections is
  about 8-10 connections; keep total clients below the Supabase pooler limit.
- **Cloudflare R2**: unchanged. Set bucket CORS to allow `PUT` and
  `Content-Type` from the Vercel web origin before testing presigned uploads.
- **Scheduling**: disable the pg_cron `worker/tick` job when the real worker is
  running, or keep it only as a documented emergency fallback. Do not run both
  as the normal path.

## Deploy checklist

1. Apply `migrations/020_universal_ingestion.sql` to production Supabase.
2. Set R2 CORS for the web origin (`PUT`, `Content-Type`).
3. Deploy the worker image with `DATABASE_URL`, `SUPABASE_*`, `ANTHROPIC_API_KEY`,
   `R2_*`, `WORKSPACE_ID` and `WORKER_CONCURRENCY=4`.
4. Start one job (a small DOCX) and verify `extraction_version = 'grounded-v1'`,
   `document_items` > 0, and no `idle in transaction` row older than a few
   seconds in `pg_stat_activity`.
5. Upload a PDF longer than 20 pages and confirm full `document_items` and
   `document_summary` are stored.
6. Disable the pg_cron tick; verify the queue drains from the container worker
   only.
7. Run one real Anthropic/R2/Supabase pass; the local stress numbers are demo
   mode and do not measure model latency or cost.

## Cost / operations notes

- One small Railway/Fly worker is typically a few dollars a month and removes
  the pg_cron + serverless timeout class of failures.
- Keep one worker replica until duplicate-job behavior is observed in
  production. `FOR UPDATE SKIP LOCKED` prevents double-claiming; the heartbeat
  prevents stale requeues, but two replicas double the DB connection budget.
- Ask (`/api/ask`) is the most likely API endpoint to exceed Vercel's function
  time. If it does, move the FastAPI app to the same container host as the
  worker and point `NEXT_PUBLIC_API_URL` / `API_URL_INTERNAL` at it.
