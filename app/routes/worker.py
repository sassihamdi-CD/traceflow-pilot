"""Pull-based extraction worker endpoint.

Vercel cannot run a long-lived worker process. This secret-gated endpoint lets
an external scheduler (Supabase pg_cron + pg_net, GitHub Actions, cron-job.org)
drain the extraction_jobs queue inside the normal 60s function budget.

It is intentionally boring: claim one job per tick with the same code path as
scripts/worker.py, run in light mode (first chunk only, for both text and
vision), mark the document truncated so the reviewer knows to retry with a
real worker, and let recover_stale requeue anything a killed invocation left
behind.
"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import runtime_dsn

router = APIRouter()


def _authorized(value: str | None) -> bool:
    if not settings.worker_secret or not value:
        return False
    return secrets.compare_digest(value, settings.worker_secret)


@router.post("/api/worker/tick")
async def worker_tick(x_worker_secret: str | None = Header(default=None)):
    if not _authorized(x_worker_secret):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})

    # scripts.worker reads DATABASE_URL; rewrite it to the transaction pooler
    # and disable prepared statements for serverless use.
    os.environ["DATABASE_URL"] = runtime_dsn()
    from scripts.worker import get_conn, process_one

    conn = None
    processed = 0
    try:
        conn = get_conn()
        # One document per tick: a 26-page PDF can still take most of the
        # 60s function budget, and starting a second job would get killed.
        processed = 1 if await run_in_threadpool(process_one, conn, True, 2) else 0
        return {"ok": True, "processed": processed}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={
            "error": f"{type(exc).__name__}: {exc}"[:600]
        })
    finally:
        if conn is not None:
            conn.close()


@router.get("/api/worker/status")
async def worker_status(x_worker_secret: str | None = Header(default=None)):
    """Read-only queue/document diagnostics for the configured workspace."""
    if not _authorized(x_worker_secret):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    from app.db import get_pool

    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT d.id::text, d.filename, d.extraction_status, d.uploaded_at,"
                " j.id::text, j.status, j.attempts, j.error, j.updated_at"
                " FROM documents d"
                " LEFT JOIN LATERAL ("
                "   SELECT id, status, attempts, error, updated_at"
                "   FROM extraction_jobs WHERE document_id = d.id"
                "   ORDER BY created_at DESC LIMIT 1) j ON TRUE"
                " WHERE d.workspace_id = %s ORDER BY d.uploaded_at DESC LIMIT 15",
                (settings.workspace_id,),
            )
            docs = [{
                "document_id": r[0], "filename": r[1], "status": r[2],
                "uploaded_at": r[3].isoformat() if r[3] else None,
                "job_id": r[4], "job_status": r[5], "attempts": r[6],
                "job_error": r[7], "job_updated_at": r[8].isoformat() if r[8] else None,
            } for r in await cur.fetchall()]
            cron = []
            try:
                cur = await conn.execute(
                    "SELECT jobid, schedule, active FROM cron.job WHERE jobname='traceflow-worker'"
                )
                cron = [{"jobid": r[0], "schedule": r[1], "active": r[2]} for r in await cur.fetchall()]
            except Exception as exc:  # noqa: BLE001
                cron = [{"error": f"{type(exc).__name__}: {exc}"[:200]}]
                await conn.rollback()
        return {"documents": docs, "cron": cron}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={
            "error": f"{type(exc).__name__}: {exc}"[:600]
        })
