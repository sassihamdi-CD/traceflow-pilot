"""Standalone DB-backed extraction queue poller (Phase 3).

Polls extraction_jobs for queued rows with SELECT ... FOR UPDATE SKIP LOCKED,
marks running (attempts+1), parses any supported format with
`document_ingest.parse_document`, runs grounded two-phase extraction, stores
passport proposals + full transcription + document metadata, and on exception
marks failed with an error truncated to 500 chars.

Usage:
    DATABASE_URL=postgresql://... python scripts/worker.py [--once] [--concurrency N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from contextlib import contextmanager

# Repo-root bootstrap: when run as `python scripts/worker.py` (Docker CMD),
# sys.path[0] is scripts/, so `from app...` imports fail. Same pattern as
# scripts/backfill_parties.py. Without this every job fails with
# ModuleNotFoundError: No module named 'app'.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg

CLAIM_SQL = """
SELECT id, document_id, workspace_id
FROM extraction_jobs
WHERE status = 'queued'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED
"""


def get_conn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    # Transaction-mode poolers (Supabase :6543) do not support prepared
    # statements; disabling them also keeps direct connections harmless.
    return psycopg.connect(dsn, connect_timeout=15, prepare_threshold=None)


@contextmanager
def _heartbeat(job_id, interval: float = 60.0):
    """Keep a long job's updated_at fresh so another worker never requeues it.

    recover_stale() only looks at updated_at; without a heartbeat a full
    transcript that runs longer than the stale window is "recovered" while
    still alive and processed twice. The heartbeat uses its own autocommit
    connection so it never holds a transaction or a pool slot.
    """
    stop = threading.Event()

    def beat() -> None:
        # Only open a connection if the job is still running after one interval;
        # short jobs (the common demo case) pay no extra connection cost.
        if stop.wait(interval):
            return
        try:
            with psycopg.connect(
                os.environ["DATABASE_URL"], connect_timeout=15,
                autocommit=True, prepare_threshold=None,
            ) as hb:
                while True:
                    with hb.cursor() as cur:
                        cur.execute(
                            "UPDATE extraction_jobs SET updated_at = now()"
                            " WHERE id = %s AND status = 'running'",
                            (job_id,),
                        )
                    if stop.wait(interval):
                        return
        except Exception as exc:  # noqa: BLE001
            print(f"heartbeat stopped: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


def _fetch_document_bytes(storage_key: str) -> bytes:
    """Download document bytes (R2, or the shared local volume in demo mode)."""
    from app.services.storage import read_from_storage

    return read_from_storage(storage_key)


class _SkipFullItems(Exception):
    pass


def _run_real_extraction(conn, job_id, document_id, workspace_id, light: bool = False) -> None:
    """Fetch one document, parse any supported format, run grounded extraction,
    store passport proposals + full transcription + document metadata."""
    from app.services.document_ingest import DocumentParseError, parse_document
    from app.services.extraction import sanitize_items, validate_item_locations
    from app.services.grounded_extraction import (
        GroundedExtractionError,
        extract_document,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_id, storage_key, filename FROM documents WHERE id = %s",
            (document_id,),
        )
        doc = cur.fetchone()
        if doc is None:
            raise RuntimeError("document gone")
        product_id, storage_key, filename = doc

        cur.execute("SELECT category FROM products WHERE id = %s", (product_id,))
        prow = cur.fetchone()
        if prow is None:
            raise RuntimeError("product gone")
        category = prow[0]

        cur.execute(
            "SELECT field_key, label, required, display_order FROM field_definitions"
            " WHERE category = %s ORDER BY display_order",
            (category,),
        )
        defs = [
            {"field_key": r[0], "label": r[1], "required": bool(r[2]), "display_order": r[3]}
            for r in cur.fetchall()
        ]

    with conn.cursor() as cur:
        # Idempotency: a duplicate queue row must not insert a second set of
        # proposals. Only skip when the document actually finished.
        cur.execute(
            "SELECT extraction_status FROM documents WHERE id = %s",
            (document_id,),
        )
        drow = cur.fetchone()
        doc_status = drow[0] if drow else None
        cur.execute(
            "SELECT COUNT(*) FROM field_values WHERE document_id = %s",
            (document_id,),
        )
        has_values = (cur.fetchone() or [0])[0] > 0
        if doc_status == "done" and has_values:
            cur.execute(
                "UPDATE extraction_jobs SET status = 'done', error = NULL,"
                " updated_at = now() WHERE id = %s",
                (job_id,),
            )
            conn.commit()
            return
        cur.execute(
            "DELETE FROM field_values WHERE document_id = %s AND status = 'proposed'",
            (document_id,),
        )
        # Commit BEFORE downloading/parsing/LLM work. Otherwise the connection
        # sits "idle in transaction" for minutes, holding a DB slot and row
        # locks while the model runs — the first thing to break under load.
        conn.commit()

    try:
        body = _fetch_document_bytes(storage_key)
    except Exception as exc:
        raise RuntimeError(f"r2-fetch-failed: {exc}") from exc

    try:
        parsed = parse_document(filename or storage_key or "", body)
    except DocumentParseError as exc:
        detail = " ".join(x for x in (exc.message, exc.hint) if x)
        raise RuntimeError(f"{exc.code}: {detail}"[:500]) from exc

    try:
        result = asyncio.run(extract_document(parsed, defs, light=light))
    except GroundedExtractionError as exc:
        raise RuntimeError(f"extraction-failed: {exc}"[:500]) from exc

    allowed = {d["field_key"] for d in defs}
    clean = validate_item_locations(
        sanitize_items(result.fields, allowed),
        parsed.ext,
        parsed.tables if parsed.kind == "table" else None,
    )
    truncated = bool(result.truncated)
    parties = list(result.parties or [])

    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO field_values (workspace_id, product_id, field_key, value, unit,"
            " status, document_id, location, extracted_by)"
            " VALUES (%s,%s,%s,%s,%s,'proposed',%s,%s,'ai_system')",
            [
                (
                    workspace_id,
                    product_id,
                    it["field_key"],
                    it["value"],
                    it["unit"],
                    document_id,
                    it["location"],
                )
                for it in clean
            ],
        )
        # Full per-document transcription (every labelled value), separate
        # from the passport mapping. Failure here never discards the fields
        # already inserted.
        try:
            if result.items:
                from app.services.document_items import store_document_items

                stored = asyncio.run(
                    store_document_items(conn, workspace_id, document_id, result.items)
                )
                if getattr(stored, "truncated", False):
                    truncated = True
        except Exception as exc:  # noqa: BLE001
            print(f"document_items store failed: {type(exc).__name__}: {exc}")

        cur.execute(
            "UPDATE documents SET extraction_status = 'done', extracted_count = %s,"
            " extraction_error = NULL, extraction_truncated = %s,"
            " detected_parties = %s::jsonb, page_count = %s, mime_type = %s,"
            " file_size = %s, content_sha256 = %s, document_type = %s,"
            " document_summary = %s, document_language = %s,"
            " extracted_text_chars = %s, is_scanned = %s, extraction_version = %s"
            " WHERE id = %s",
            (
                len(clean),
                truncated,
                json.dumps(parties),
                parsed.page_count,
                parsed.mime,
                len(body),
                digest,
                result.document_type or parsed.kind,
                (result.summary or "")[:4000],
                result.language or "unknown",
                parsed.text_chars,
                parsed.scanned,
                "grounded-v1",
                document_id,
            ),
        )
        cur.execute(
            "UPDATE extraction_jobs SET status = 'done', error = NULL, "
            "updated_at = now() WHERE id = %s",
            (job_id,),
        )
        conn.commit()

def recover_stale(conn, stale_minutes: int = 15) -> None:
    """Requeue jobs whose worker died mid-run.

    Without this a crash (or a container stop) leaves a job 'running' forever
    and the document stuck in 'extracting', with no reviewer-visible error.
    After three attempts the job is failed instead of retried in a loop.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE extraction_jobs SET status = 'failed',"
            " error = COALESCE(error, 'worker stopped before finishing'),"
            " updated_at = now()"
            " WHERE status = 'running' AND attempts >= 3"
            " AND updated_at < now() - make_interval(mins => %s)"
            " RETURNING document_id",
            (stale_minutes,),
        )
        failed_docs = [r[0] for r in cur.fetchall()]
        for document_id in failed_docs:
            cur.execute(
                "UPDATE documents SET extraction_status = 'failed',"
                " extraction_error = 'worker stopped before finishing' WHERE id = %s",
                (document_id,),
            )
        cur.execute(
            "UPDATE extraction_jobs SET status = 'queued', updated_at = now()"
            " WHERE status = 'running'"
            " AND updated_at < now() - make_interval(mins => %s)"
            " RETURNING document_id",
            (stale_minutes,),
        )
        requeued_docs = [r[0] for r in cur.fetchall()]
        for document_id in requeued_docs:
            cur.execute(
                "UPDATE documents SET extraction_status = 'pending', extraction_error = NULL"
                " WHERE id = %s",
                (document_id,),
            )
        conn.commit()
        if failed_docs or requeued_docs:
            print(f"recovered stale jobs: requeued={len(requeued_docs)} failed={len(failed_docs)}")


def process_one(conn, light: bool = False, stale_minutes: int = 15) -> bool:
    """Claim a single queued job and transition it. Returns True if a job was handled."""
    recover_stale(conn, stale_minutes)
    with conn.cursor() as cur:
        cur.execute(CLAIM_SQL)
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return False
        job_id, document_id, workspace_id = row
        cur.execute(
            "UPDATE extraction_jobs SET status = 'running', attempts = attempts + 1, "
            "updated_at = now() WHERE id = %s",
            (job_id,),
        )
        conn.commit()
        try:
            with _heartbeat(job_id):
                _run_real_extraction(conn, job_id, document_id, workspace_id, light=light)
        except Exception as exc:  # noqa: BLE001
            # The failed statement left the transaction aborted; without this
            # rollback the UPDATE below itself fails (InFailedSqlTransaction)
            # and the job/document stay 'running'/'extracting' forever.
            conn.rollback()
            # Class + message (truncated): class-only errors are low-signal
            # for reviewers (audit C15). The r2-fetch prefix survives inside
            # the message instead of being collapsed to a bare token.
            err = f"{type(exc).__name__}: {exc}"[:500]
            with conn.cursor() as cur2:
                cur2.execute(
                    "UPDATE extraction_jobs SET status = 'failed', error = %s, "
                    "updated_at = now() WHERE id = %s",
                    (err, job_id),
                )
                try:
                    cur2.execute(
                        "UPDATE documents SET extraction_status = 'failed',"
                        " extraction_error = %s WHERE id = %s",
                        (err, document_id),
                    )
                except Exception:
                    pass
            conn.commit()
        return True


def _run_one_worker(once: bool, poll_interval: float) -> None:
    with get_conn() as conn:
        while True:
            handled = process_one(conn)
            if once:
                return
            if not handled:
                time.sleep(poll_interval)


def run_loop(once: bool = False, poll_interval: float = 2.0, concurrency: int = 1) -> None:
    """Run one or more queue consumers.

    Each consumer owns its own DB connection, so ``FOR UPDATE SKIP LOCKED``
    lets manufacturers process several documents in parallel without
    double-claiming a job. Embeddings/Anthropic calls inside a job are already
    bounded by the extractor's own semaphore.
    """
    concurrency = max(1, min(int(concurrency or 1), 16))
    if concurrency == 1:
        _run_one_worker(once, poll_interval)
        return
    import threading

    threads = [
        threading.Thread(target=_run_one_worker, args=(once, poll_interval), daemon=True)
        for _ in range(concurrency)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TraceFlow extraction queue worker")
    parser.add_argument("--once", action="store_true", help="Process at most one job then exit (tests/CI)")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("WORKER_CONCURRENCY", "1")),
        help="Parallel queue consumers (default WORKER_CONCURRENCY or 1)",
    )
    args = parser.parse_args(argv)
    run_loop(once=args.once, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
