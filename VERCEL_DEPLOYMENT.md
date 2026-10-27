# Vercel Deployment Guide

## Architecture Overview

**Frontend (Vercel)**: Next.js 14 app in `/web`
- Static/SSR pages, API routes for auth callbacks
- Calls backend via `NEXT_PUBLIC_API_URL`

**Backend (Separate)**: FastAPI in `/app` 
- Deploy to Railway, Render, or Fly.io
- Needs PostgreSQL, R2, Anthropic, Supabase

---

## 1. Deploy Backend First (Railway/Render/Fly.io)

### Required Environment Variables for Backend

```bash
# Database (Supabase or managed Postgres)
DATABASE_URL=postgresql://user:pass@host:5432/dbname

# Supabase Auth
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=eyJ...

# Workspace (per-company)
WORKSPACE_ID=uuid-from-founder-mint

# Cloudflare R2 Storage
R2_ENDPOINT=https://xxx.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=xxx
R2_SECRET_ACCESS_KEY=xxx
R2_BUCKET=traceflow-docs

# Anthropic
ANTHROPIC_API_KEY=sk-ant-usr-...
ANTHROPIC_MODEL=claude-opus-5-5

# Frontend URL (for CORS)
FRONTEND_URL=https://your-app.vercel.app

# Founder key (server-only)
FOUNDER_KEY=TF-FOUNDER-...

# Port
PORT=8000
```

### Deploy to Railway (Recommended)
```bash
# Install Railway CLI
npm i -g @railway/cli

# Login and deploy
railway login
railway init
railway up

# Set env vars in Railway dashboard
```

### Or Render
```yaml
# render.yaml
services:
  - type: web
    name: tracflow-api
    env: docker
    dockerfilePath: ./Dockerfile
    envVars:
      - key: DATABASE_URL
        sync: false
      # ... add all vars above
```

---

## 2. Deploy Frontend to Vercel

### Option A: Vercel CLI
```bash
cd web
npm i -g vercel
vercel login
vercel --prod
```

### Option B: GitHub Integration
1. Push to GitHub
2. Import in Vercel dashboard
3. Set root directory to `web`
4. Add environment variables:
   - `NEXT_PUBLIC_API_URL` = `https://your-api.railway.app` (or render/fly URL)
   - `NEXT_PUBLIC_SUPABASE_URL` = from Supabase
   - `NEXT_PUBLIC_SUPABASE_ANON_KEY` = from Supabase
   - `NEXT_PUBLIC_PILOT_INVITE_CODE` = from founder mint

---

## 3. Configure Supabase

### Auth Settings
- **Site URL**: `https://your-app.vercel.app`
- **Redirect URLs**: 
  - `https://your-app.vercel.app/login`
  - `https://your-app.vercel.app/**`

### Email Auth
- Disable "Confirm email" for pilot (instant signup)
- Or configure SMTP for production

---

## 3.5 Apply migration 020 (universal ingestion)

```bash
psql "$DATABASE_URL" -f migrations/020_universal_ingestion.sql
```

Adds document metadata (`mime_type`, `file_size`, `content_sha256`,
`document_type`, `document_summary`, `document_language`,
`extracted_text_chars`, `is_scanned`, `extraction_version`), evidence on
`document_items`/`field_values`, and queue indexes. Re-runnable.

## 4. Configure CORS on Backend

In `app/main.py`, ensure `FRONTEND_URL` includes your Vercel domain:
```python
FRONTEND_URL = "https://your-app.vercel.app,http://localhost:3001"
```

---

## 5. Test Production Flow

1. **Founder mints company code** → `https://your-app.vercel.app/founder`
2. **Manufacturer signs up** → `https://your-app.vercel.app/login` with code
3. **Upload document** → extraction runs on backend
4. **Review queue** → accept/reject/correct values
4. **Publish passport** → public URL at `/passport/{slug}`

---

## 6. Per-Company Production Setup

For each manufacturer:
```bash
# 1. Founder mints code at /founder
# 2. Create separate backend deployment with:
#    - Unique DATABASE_URL (per-company DB)
#    - Unique WORKSPACE_ID
#    - Unique R2 prefix (handled by WORKSPACE_ID)
#    - Unique INTAKE_SECRET
# 3. Update Vercel env with new NEXT_PUBLIC_API_URL
```

---

## Current Status

✅ Frontend builds successfully (Next.js 14)
✅ All 118 backend tests pass
✅ Extraction deterministic (temperature=0)
✅ Italian prompts for footwear
✅ Per-company isolation architecture ready

**Next**: Deploy backend to Railway/Render, then frontend to Vercel.
---

## Serverless pilot notes (Vercel, 2026-10-21)

The live pilot runs **both** the Next.js frontend and the FastAPI backend on
Vercel (Hobby). Three platform constraints shape the setup:

1. **Database connections.** Use Supabase's *transaction* pooler (port `6543`),
   not the session pooler (`5432`). The app rewrites a `*.pooler.supabase.com:5432`
   `DATABASE_URL` to `:6543`, keeps `max_size=2`, and disables prepared
   statements. Session mode is capped at the project's pool size and Vercel's
   warm instances exhaust it.
2. **Request body limit (4.5 MB).** Vercel rejects larger bodies before the
   function runs, with a `413` that has no CORS headers (Safari shows
   "Load failed"). The web uploader now switches to a presigned R2 PUT above
   4 MiB: `POST /api/products/{id}/documents/presign` returns a signed URL,
   the browser PUTs the bytes straight to R2, then
   `POST .../documents/complete` verifies the object and queues extraction.
   Multipart remains the fallback when R2 presigning is unavailable.
   **Configure a CORS rule on the R2 bucket** allowing `PUT` from the web
   origin (`https://traceflow.mindscript.tech`) with the `Content-Type`
   header; without it the browser blocks the direct upload.
3. **No long-lived worker.** `scripts/worker.py` cannot run as a Vercel
   function. `POST /api/worker/tick` (header `X-Worker-Secret`) drains
   `extraction_jobs` for up to 40 seconds. Schedule it with Supabase
   `pg_cron` + `pg_net` every minute:

   ```sql
   CREATE EXTENSION IF NOT EXISTS pg_cron;
   CREATE EXTENSION IF NOT EXISTS pg_net;
   SELECT cron.schedule('traceflow-worker', '* * * * *', $$
     SELECT net.http_post(
       url := 'https://<backend>.vercel.app/api/worker/tick',
       headers := jsonb_build_object(
         'Content-Type', 'application/json',
         'X-Worker-Secret', '<WORKER_SECRET>'),
       body := '{}'::jsonb,
       timeout_milliseconds := 55000)
   $$);
   ```

   Any external scheduler (GitHub Actions, cron-job.org) can call the same
   endpoint. Replace this with a real worker process when moving off Hobby.

   **Recommended now:** run `python scripts/worker.py --concurrency 4` on
   Railway or Fly.io (1 vCPU / 1 GB), keep Supabase + R2, and disable the
   pg_cron job. The tick is light mode: it can mark a PDF done before full
   transcription, and it cannot process large documents within 60 seconds.
   See `docs/HOSTING_OPTIONS.md` and `docs/STRESS_TESTING.md`.

Also verify that a `workspaces` row exists for the configured `WORKSPACE_ID`.
Migration `000_pilot_bootstrap.sql` seeds `4db22bd4-77a6-4013-91c7-7e8936680909`;
if `WORKSPACE_ID` differs, insert the matching row before minting invites:

```sql
INSERT INTO workspaces (id, name) VALUES ('<WORKSPACE_ID>', 'Pilot manufacturer')
ON CONFLICT (id) DO NOTHING;
```
