# Stress testing (2026-10-26)

Two harnesses cover different failure modes:

| Harness | What it exercises | Needs |
| --- | --- | --- |
| `scripts/stress_ingestion.py` | Real HTTP upload, storage, queue, worker, parsing, demo extraction, DB writes | Local Docker stack (`api`, `db`, `worker`) |
| `scripts/stress_pipeline.py` | Real chunking/grounding/mapping/concurrency, with a fake model (no Anthropic bill) | Python venv only |

Both are **local** tools. Do not point them at production.

## Run

```bash
# back-end E2E (demo mode)
.venv/bin/python scripts/stress_ingestion.py --profile smoke
.venv/bin/python scripts/stress_ingestion.py --profile load      --concurrency 6
.venv/bin/python scripts/stress_ingestion.py --profile extreme   --concurrency 6
.venv/bin/python scripts/stress_ingestion.py --profile large     --concurrency 3
.venv/bin/python scripts/stress_ingestion.py --profile boundary  --concurrency 3

# grounded pipeline with a fake model
.venv/bin/python scripts/stress_pipeline.py --docs 12 --kind mixed \
  --pages 80 --lines 160 --cells 50000 --latency 0.02
.venv/bin/python scripts/stress_pipeline.py --docs 8 --kind text \
  --pages 80 --lines 160 --fail-every 17
```

`stress_ingestion.py` keeps the generated files and product for inspection unless
you clean them up. Products use the `STRESS-*` SKU prefix.

## Local results (Docker, demo extraction)

Host: 14.98 GiB RAM, API :8010, web :3010, Postgres 16, worker concurrency 1-4.

| Profile | Docs | Items | Proposals | Wall / drain | Worker peak |
| --- | ---: | ---: | ---: | --- | ---: |
| smoke | 10 | 10,888 | 63 | 6-49 s* | ~140 MiB |
| load @1 | 29 | 40,530 | 196 | 28.3 s | ~137 MiB |
| load @4 | 29 | 40,530 | 196 | 20.8 s | ~170 MiB |
| extreme @1 | 34 | 50,530 | 231 | 45.9 s | ~179 MiB |
| extreme @4 | 34 | 50,530 | 231 | 38.6 s | ~249 MiB |
| large @3 | 3 | 6,000 | 21 | 14.4 s | ~214 MiB |
| boundary @3 | 13 | 10,888 | 63 | 2.1 s | ~150 MiB |

\*The test host also runs several unrelated local stacks. The same smoke run
took 6 s when idle and 49 s at load average ~10; treat absolute timings as
shape, not a benchmark.

`large` = ~12 MB TXT (4 MB read cap), ~30 MB / 3.6M-cell CSV, 1M-cell XLSX.
`boundary` = 65.5 MB total; the 65 MB PDF and zip bomb are rejected at upload
(413/415), and a corrupt PDF fails cleanly in the worker. 10/13 processed.

Pipeline harness (fake model):

- 12 mixed docs (8 text + 4 tables of 50k cells), 572 model calls, 12.2 s,
  211 MiB peak, 56k items, 0 invented values.
- 8 text docs with a simulated upstream failure every 17 calls: 18 failures,
  0 failed documents, partial results marked `truncated`.

Limitations: demo extraction is deterministic and CPU-bound, so these times are
**not** real Anthropic/R2/Supabase latency. The pipeline harness measures our
own chunking/grounding memory and wall time, not model cost. A real-key pass is
still required.

## Findings fixed during this run

1. **CSV memory blow-up**: a 30 MB / 3.6M-cell CSV reached ~541 MiB because
   pandas materialised the whole file. `csv_to_table` now streams with the
   stdlib `csv` reader and stops at the 500-cell budget; the same workload with
   three large documents peaked at ~214 MiB. `pandas` was removed from
   `requirements.txt`.
2. **Silent mapping failures**: field-mapping exceptions were warnings only and
   did not set `truncated`, so a document could look complete with missing
   fields. Any mapping warning now marks the document truncated for retry.
3. **Per-row insert overhead**: `document_items` (and worker `field_values`)
   inserted one round trip per row. Both now use `executemany`; a 507-item
   store fell from 1.34 s to 0.55 s locally.
4. **Stale-job duplication**: `recover_stale()` could requeue a live job whose
   full transcript took longer than the 15-minute window. The worker now sends a
   heartbeat every 60 s on an autocommit connection while a job runs.
5. **DB transaction held during model work** (already fixed): the worker commits
   the claim/cleanup before downloading/parsing/LLM work.
6. **Zip bombs / text bombs** (already fixed): zip safety and per-format text
   budgets reject or truncate hostile inputs before parsing.

## Production prerequisites

Apply migration 020, set R2 CORS (`PUT`, `Content-Type` from the web origin),
and run a real worker. Do not leave the serverless light-mode tick and a full
worker fighting over PDFs: the light tick can mark a PDF done before the full
worker transcribes it. Disable the pg_cron tick when the full worker is live.
