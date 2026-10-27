#!/usr/bin/env python3
"""Stress the grounded extraction pipeline without an Anthropic bill.

This exercises the real code paths that the end-to-end demo skips:

- 24k-char chunking, 80-chunk ceiling, 300-item chunk cap, 8k item cap
- bounded concurrency (4 chunks per document)
- text grounding against a 4 MB source
- field mapping over 8k items / 50k table cells
- per-document memory and wall-clock cost

The model is faked in-process (`gx._call_json`) with deterministic responses
and optional latency/failure injection. It is not a unit test; run it on a
laptop or in CI with a generous timeout.

Examples:
    .venv/bin/python scripts/stress_pipeline.py --docs 12 --latency 0.02
    .venv/bin/python scripts/stress_pipeline.py --docs 30 --latency 0.05 --fail-every 17
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import resource
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import grounded_extraction as gx
from app.services.document_ingest import ParsedDocument, ParsedPage
from app.services.extraction_prompts import FIELD_GUIDE_FOOTWEAR

FIELD_LINES = [
    ("Product name", "Universal Runner"),
    ("SKU", "PIPE-2026-001"),
    ("Upper material", "Recycled polyester mesh"),
    ("Upper composition", "100% recycled polyester"),
    ("Leather origin", "Italy"),
    ("Sole material", "Rubber"),
    ("REACH compliance", "Compliant with Regulation (EC) 1907/2006"),
]
FIELD_DEFS = [
    {"field_key": key, "label": label}
    for key, label in [
        ("product_name", "Product name"),
        ("sku", "SKU"),
        ("upper_material", "Upper material"),
        ("upper_composition", "Upper composition"),
        ("leather_origin", "Leather origin"),
        ("sole_material", "Sole material"),
        ("reach_compliance", "REACH compliance"),
    ]
]
FEET = {d["field_key"] for d in FIELD_DEFS}


@dataclass
class DocSpec:
    name: str
    parsed: ParsedDocument


def make_text_doc(name: str, pages: int, lines_per_page: int, rng: random.Random) -> DocSpec:
    parsed_pages: list[ParsedPage] = []
    lines: list[str] = []
    for p in range(pages):
        if p == 0:
            page_lines = [f"{label}: {value}" for label, value in FIELD_LINES]
            page_lines += [
                f"Substance {p:04d}-{i:05d}: {rng.randint(1, 999)}.{rng.randint(0, 99)} mg/kg"
                for i in range(max(lines_per_page - len(page_lines), 0))
            ]
        else:
            page_lines = [
                f"Substance {p:04d}-{i:05d}: {rng.randint(1, 999)}.{rng.randint(0, 99)} mg/kg"
                for i in range(lines_per_page)
            ]
        text = "\n".join(page_lines)
        parsed_pages.append(ParsedPage(f"Page {p + 1}", text, p + 1))
        lines.append(text)
    source = "\n".join(lines)
    return DocSpec(name, ParsedDocument(
        filename=f"{name}.txt", ext=".txt", kind="text", mime="text/plain",
        pages=parsed_pages, page_count=pages, text_chars=len(source),
    ))


def make_table_doc(name: str, cells: int, rng: random.Random) -> DocSpec:
    table = []
    for i in range(cells):
        table.append({
            "sheet": "Sheet1",
            "cell": f"A{i + 1}",
            "value": f"cell-{i:06d}-{rng.randint(0, 9999)}",
        })
    for i, (label, value) in enumerate(FIELD_LINES):
        table[i] = {"sheet": "Sheet1", "cell": f"A{i + 1}", "value": label}
        table[cells // 2 + i] = {
            "sheet": "Sheet1", "cell": f"B{i + 1}", "value": value,
        }
    return DocSpec(name, ParsedDocument(
        filename=f"{name}.xlsx", ext=".xlsx", kind="table",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        pages=[], tables=table, page_count=None,
        text_chars=sum(len(x["value"]) for x in table),
    ))


def _items_from_source(source: str) -> list[dict]:
    items = []
    for line in source.splitlines():
        line = line.strip()
        if not line or line.startswith("[Page"):
            continue
        match = re.match(r"^(.{1,120}?):\s*(.+)$", line)
        if match:
            label, value = match.group(1).strip(), match.group(2).strip()
        else:
            label, value = line[:120], line
        items.append({
            "label": label, "value": value, "unit": None,
            "location": "Page 1", "evidence": line,
        })
        if len(items) >= gx.MAX_ITEMS_PER_CHUNK:
            break
    return items


def _field_for_label(label: str) -> str | None:
    from app.services.demo_extraction import _match_field

    return _match_field(label, FIELD_DEFS)


def _fake_transcribe(content) -> dict:
    text = ""
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
    source = text.split("SOURCE:", 1)[-1] if "SOURCE:" in text else ""
    items = _items_from_source(source) if source else []
    # A plausible hallucination that is NOT in the source. Grounding must drop it.
    items.append({
        "label": "Invented", "value": "Unicorn leather",
        "unit": None, "location": "Page 99", "evidence": "unicorn leather",
    })
    return {
        "document_type": "supplier declaration",
        "language": "en",
        "summary": "Synthetic declaration for pipeline stress.",
        "items": items,
    }


def _fake_map(content) -> dict:
    text = ""
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
    start = text.find('{"requested_fields"')
    if start < 0:
        return {"fields": []}
    payload = json.loads(text[start:])
    requested = payload.get("requested_fields") or []
    allowed = {r.get("field_key") for r in requested if r.get("field_key")}
    found = []
    if "items" in payload:
        for item in payload["items"]:
            label = str(item.get("label") or "")
            key = _field_for_label(label)
            if key in allowed and key in FEET:
                found.append({
                    "field_key": key,
                    "id": item.get("_id"),
                    "value": item.get("value"),
                    "unit": item.get("unit"),
                    "location": item.get("location"),
                })
                allowed.discard(key)
    else:
        cells = payload.get("cells") or []
        by_location = {f"{c.get('sheet')} / {c.get('cell')}": c for c in cells}
        for i, cell in enumerate(cells):
            key = _field_for_label(str(cell.get("value") or ""))
            if key in allowed and key in FEET and i + 1 < len(cells):
                nxt = cells[i + 1]
                found.append({
                    "field_key": key,
                    "value": nxt.get("value"),
                    "unit": None,
                    "location": f"{nxt.get('sheet')} / {nxt.get('cell')}",
                })
                allowed.discard(key)
    return {"fields": found}


class FakeModel:
    def __init__(self, latency: float, fail_every: int):
        self.latency = latency
        self.fail_every = fail_every
        self.calls = 0
        self.failures = 0
        self.lock = asyncio.Lock()

    async def __call__(self, system, content, max_tokens):
        async with self.lock:
            self.calls += 1
            call_no = self.calls
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.fail_every and call_no % self.fail_every == 0:
            self.failures += 1
            raise RuntimeError("simulated upstream 500")
        if system == gx.TRANSCRIBE_SYSTEM:
            return _fake_transcribe(content)
        if system == gx.FIELD_MAP_SYSTEM:
            return _fake_map(content)
        return {"items": [], "fields": []}


async def run_one(spec: DocSpec, model: FakeModel, timeout: float) -> dict:
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            gx.extract_document(spec.parsed, FIELD_DEFS, light=False),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "name": spec.name, "ok": False, "error": f"{type(exc).__name__}: {exc}",
            "seconds": round(time.monotonic() - started, 3),
        }
    invented = [i for i in result.items if i.get("value") == "Unicorn leather"]
    return {
        "name": spec.name,
        "ok": not invented,
        "seconds": round(time.monotonic() - started, 3),
        "items": len(result.items),
        "fields": len(result.fields),
        "truncated": result.truncated,
        "warnings": len(result.warnings),
        "invented": len(invented),
        "text_chars": spec.parsed.text_chars,
        "cells": len(spec.parsed.tables),
    }


async def main_async(args) -> int:
    rng = random.Random(20261026)
    specs: list[DocSpec] = []
    for i in range(args.docs):
        if args.kind == "table":
            specs.append(make_table_doc(f"table-{i}", args.cells, rng))
        elif args.kind == "text":
            specs.append(make_text_doc(f"text-{i}", args.pages, args.lines, rng))
        else:
            if i % 3 == 0:
                specs.append(make_table_doc(f"mixed-table-{i}", args.cells, rng))
            else:
                specs.append(make_text_doc(f"mixed-text-{i}", args.pages, args.lines, rng))

    model = FakeModel(args.latency, args.fail_every)
    gx._call_json = model  # type: ignore[assignment]

    from types import SimpleNamespace

    gx.settings = SimpleNamespace(demo_mode=False, anthropic_api_key="test", anthropic_model="fake")
    started = time.monotonic()
    sem = asyncio.Semaphore(args.docs_concurrency)

    async def one(spec: DocSpec):
        async with sem:
            return await run_one(spec, model, args.timeout)

    results = await asyncio.gather(*(one(s) for s in specs))
    elapsed = time.monotonic() - started
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    failures = [r for r in results if not r.get("ok")]
    print(f"documents        {len(specs)}")
    print(f"kind             {args.kind}")
    print(f"fake calls       {model.calls} (failures={model.failures})")
    print(f"wall time        {elapsed:.2f}s")
    print(f"peak RSS         {rss_kb / 1024:.1f} MiB")
    print(f"items total      {sum(r.get('items', 0) for r in results)}")
    print(f"fields total     {sum(r.get('fields', 0) for r in results)}")
    print(f"truncated docs   {sum(1 for r in results if r.get('truncated'))}")
    print(f"bad docs         {len(failures)}")
    for r in failures[:20]:
        print(f"  - {r}")
    if args.json:
        print(json.dumps(results, indent=2))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=12)
    parser.add_argument("--kind", choices=["text", "table", "mixed"], default="mixed")
    parser.add_argument("--pages", type=int, default=80, help="text pages per document")
    parser.add_argument("--lines", type=int, default=160, help="lines per text page")
    parser.add_argument("--cells", type=int, default=50_000, help="table cells")
    parser.add_argument("--latency", type=float, default=0.02, help="fake model latency (s/call)")
    parser.add_argument("--fail-every", type=int, default=0, help="inject a failure every N calls")
    parser.add_argument("--timeout", type=float, default=300.0, help="per-document timeout")
    parser.add_argument("--docs-concurrency", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
