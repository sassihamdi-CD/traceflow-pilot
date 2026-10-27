"""Reviewer actions (spec section 4). Every action + audit row in one transaction."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from app import errors

from app.auth import Caller, ensure_workspace_exists, get_caller, require_reviewer
from app.db import get_pool
from app.schemas import CorrectBody, ResolveConflictBody, uuid_or_404

router = APIRouter()


async def _audit(conn, ws: str, actor: str, action: str, entity_id: str, detail: str = "{}"):
    await conn.execute(
        "INSERT INTO audit_log (workspace_id, actor, action, entity_type, entity_id, detail)"
        " VALUES (%s,%s,%s,'field_value',%s,%s::jsonb)",
        (ws, actor, action, entity_id, detail),
    )




async def _reject_verified(conn, ws: str, product_id: str, field_key: str, actor: str) -> list[str]:
    """Reject the currently verified row(s) for a field.

    The product invariant is one accepted/corrected value per field. Historic
    data could contain duplicates; this collapses them whenever a reviewer
    makes a new decision, so the public passport cannot emit duplicate labels.
    """
    cur = await conn.execute(
        "SELECT id FROM field_values WHERE product_id = %s AND workspace_id = %s"
        " AND field_key = %s AND status IN ('accepted','corrected') FOR UPDATE",
        (product_id, ws, field_key),
    )
    ids = [str(r[0]) for r in await cur.fetchall()]
    for old_id in ids:
        await conn.execute(
            "UPDATE field_values SET status='rejected', reviewed_by=%s, reviewed_at=now()"
            " WHERE id = %s",
            (actor, old_id),
        )
    return ids

@router.post("/api/fields/{field_value_id}/accept")
async def accept_field(field_value_id: str, caller: Caller = Depends(require_reviewer)):
    uuid_or_404(field_value_id)
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT product_id, field_key, status FROM field_values"
                " WHERE id = %s AND workspace_id = %s FOR UPDATE",
                (field_value_id, caller.workspace_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise errors.not_found("That value")
            product_id, field_key, status = row[0], row[1], row[2]
            if status != "proposed":
                raise errors.already_decided("accepted", status)
            superseded = await _reject_verified(
                conn, caller.workspace_id, product_id, field_key, caller.user_id
            )
            await conn.execute(
                "UPDATE field_values SET status='accepted', reviewed_by=%s, reviewed_at=now(),"
                " supersedes_id=%s WHERE id = %s",
                (caller.user_id, superseded[0] if superseded else None, field_value_id),
            )
            await _audit(conn, caller.workspace_id, caller.user_id, "field.accepted", field_value_id,
                         json.dumps({"superseded": superseded}))
    return {"ok": True}


@router.post("/api/fields/{field_value_id}/reject")
async def reject_field(field_value_id: str, caller: Caller = Depends(require_reviewer)):
    uuid_or_404(field_value_id)
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT status FROM field_values WHERE id = %s AND workspace_id = %s",
                (field_value_id, caller.workspace_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise errors.not_found("That value")
            if row[0] != "proposed":
                raise errors.already_decided("rejected", row[0])
            await conn.execute(
                "UPDATE field_values SET status='rejected', reviewed_by=%s, reviewed_at=now()"
                " WHERE id = %s",
                (caller.user_id, field_value_id),
            )
            await _audit(conn, caller.workspace_id, caller.user_id, "field.rejected", field_value_id)
    return {"ok": True}


@router.post("/api/fields/{field_value_id}/correct")
async def correct_field(field_value_id: str, body: CorrectBody, caller: Caller = Depends(require_reviewer)):
    uuid_or_404(field_value_id)
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT product_id, field_key, status, document_id FROM field_values"
                " WHERE id = %s AND workspace_id = %s",
                (field_value_id, caller.workspace_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise errors.not_found("That value")
            product_id, field_key, status, document_id = row[0], row[1], row[2], row[3]
            if status != "proposed":
                raise errors.already_decided("corrected", status)
            superseded = await _reject_verified(
                conn, caller.workspace_id, product_id, field_key, caller.user_id
            )
            cur = await conn.execute(
                "INSERT INTO field_values (workspace_id, product_id, field_key, value, unit,"
                " status, document_id, extracted_by, reviewed_by, reviewed_at, supersedes_id)"
                " VALUES (%s,%s,%s,%s,%s,'corrected',%s,%s,%s,now(),%s) RETURNING id",
                (caller.workspace_id, product_id, field_key, body.value, body.unit,
                 document_id, caller.user_id, caller.user_id,
                 superseded[0] if superseded else field_value_id),
            )
            new_id = str((await cur.fetchone())[0])
            await conn.execute(
                "UPDATE field_values SET status='rejected', reviewed_by=%s, reviewed_at=now() WHERE id = %s",
                (caller.user_id, field_value_id),
            )
            await _audit(conn, caller.workspace_id, caller.user_id, "field.corrected", new_id,
                         json.dumps({"supersedes": field_value_id, "replaced_verified": superseded}))
    return {"ok": True, "id": new_id}


@router.post("/api/products/{product_id}/fields/{field_key}/resolve-conflict")
async def resolve_conflict(
    product_id: str, field_key: str, body: ResolveConflictBody, caller: Caller = Depends(require_reviewer)
):
    uuid_or_404(product_id)
    uuid_or_404(body.chosen_field_value_id, "That proposal")
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id FROM field_values WHERE id = %s AND product_id = %s"
                " AND field_key = %s AND workspace_id = %s AND status = 'proposed'",
                (body.chosen_field_value_id, product_id, field_key, caller.workspace_id),
            )
            if await cur.fetchone() is None:
                raise errors.not_candidate()
            cur = await conn.execute(
                "SELECT id FROM field_values WHERE product_id = %s AND field_key = %s"
                " AND workspace_id = %s AND status = 'proposed' AND id != %s",
                (product_id, field_key, caller.workspace_id, body.chosen_field_value_id),
            )
            others = [r[0] for r in await cur.fetchall()]
            superseded = await _reject_verified(
                conn, caller.workspace_id, product_id, field_key, caller.user_id
            )
            await conn.execute(
                "UPDATE field_values SET status='accepted', reviewed_by=%s, reviewed_at=now(),"
                " supersedes_id=%s WHERE id = %s",
                (caller.user_id, superseded[0] if superseded else None, body.chosen_field_value_id),
            )
            for oid in others:
                await conn.execute(
                    "UPDATE field_values SET status='rejected', reviewed_by=%s, reviewed_at=now() WHERE id = %s",
                    (caller.user_id, str(oid)),
                )
            await _audit(
                conn, caller.workspace_id, caller.user_id, "field.conflict_resolved",
                body.chosen_field_value_id,
                json.dumps({"superseded": [str(o) for o in others] + superseded}),
            )
    return {"ok": True, "superseded": [str(o) for o in others]}


@router.post("/api/products/{product_id}/accept-all")
async def accept_all(product_id: str, caller: Caller = Depends(require_reviewer)):
    """Bulk accept: every proposed row on the product becomes accepted.

    Same transitions as single Accept (proposed -> accepted + reviewed_by/at +
    one audit row each), all in one transaction. Conflicting fields are NOT
    auto-resolved — those still need an explicit human choice per field.
    """
    uuid_or_404(product_id)
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, field_key FROM field_values WHERE product_id = %s"
                " AND workspace_id = %s AND status = 'proposed'",
                (product_id, caller.workspace_id),
            )
            cands = [(str(r[0]), r[1]) for r in await cur.fetchall()]
            # Skip fields with >1 candidate: conflict needs explicit choice.
            per_field: dict[str, list[str]] = {}
            for vid, fk in cands:
                per_field.setdefault(fk, []).append(vid)
            accepted = []
            for fk, ids in per_field.items():
                if len(ids) != 1:
                    continue
                superseded = await _reject_verified(
                    conn, caller.workspace_id, product_id, fk, caller.user_id
                )
                await conn.execute(
                    "UPDATE field_values SET status='accepted', reviewed_by=%s,"
                    " reviewed_at=now(), supersedes_id=%s WHERE id = %s",
                    (caller.user_id, superseded[0] if superseded else None, ids[0]),
                )
                await _audit(conn, caller.workspace_id, caller.user_id, "field.accepted", ids[0],
                             json.dumps({"bulk": True, "superseded": superseded}))
                accepted.append(ids[0])
            skipped = sum(len(ids) for ids in per_field.values()) - len(accepted)
    return {"ok": True, "accepted": accepted, "skipped_conflicts": skipped}


@router.post("/api/products/{product_id}/fields/{field_key}/manual")
async def manual_value(
    product_id: str, field_key: str, body: CorrectBody,
    caller: Caller = Depends(require_reviewer),
):
    """Add a reviewer-entered value for a field that no document covers.

    This is the missing-value path before publish: the value is accepted
    immediately (the reviewer is the source), and the one-verified-per-field
    index still guarantees the passport cannot show a duplicate label.
    """
    uuid_or_404(product_id)
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT category FROM products WHERE id = %s AND workspace_id = %s FOR UPDATE",
                (product_id, caller.workspace_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise errors.not_found("That item")
            category = row[0]
            cur = await conn.execute(
                "SELECT 1 FROM field_definitions WHERE category = %s AND field_key = %s",
                (category, field_key),
            )
            if await cur.fetchone() is None:
                raise errors.err(
                    422, "unknown_field",
                    "That field does not belong to this product category.",
                    "Refresh the page and choose one of the fields shown.",
                )
            cur = await conn.execute(
                "SELECT id FROM field_values WHERE product_id = %s AND workspace_id = %s"
                " AND field_key = %s AND status IN ('accepted','corrected') FOR UPDATE",
                (product_id, caller.workspace_id, field_key),
            )
            if await cur.fetchone() is not None:
                raise errors.err(
                    409, "already_verified",
                    "This field already has a verified value.",
                    "Edit or replace the existing value instead of adding a second one.",
                )
            cur = await conn.execute(
                "INSERT INTO field_values (workspace_id, product_id, field_key, value, unit,"
                " status, document_id, location, extracted_by, reviewed_by, reviewed_at)"
                " VALUES (%s,%s,%s,%s,%s,'accepted',NULL,NULL,'manual',%s,now()) RETURNING id",
                (caller.workspace_id, product_id, field_key, body.value, body.unit,
                 caller.user_id),
            )
            new_id = str((await cur.fetchone())[0])
            await _audit(conn, caller.workspace_id, caller.user_id, "field.manual", new_id,
                         json.dumps({"field_key": field_key}))
    return {"ok": True, "id": new_id}


@router.post("/api/products/{product_id}/fields/{field_key}/replace")
async def replace_verified(
    product_id: str, field_key: str, body: CorrectBody,
    caller: Caller = Depends(require_reviewer),
):
    """Replace an already-verified value with a reviewer-entered correction.

    Accepting a proposal or entering a value manually is not always the last
    word: before publish the reviewer must be able to fix a wrong value. The
    old row is rejected (history kept) and a new corrected row becomes the one
    verified value, so the public passport and the one-verified index agree.
    """
    uuid_or_404(product_id)
    pool = await get_pool()
    async with pool.connection() as conn:
        await ensure_workspace_exists(conn, caller.workspace_id)
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT category FROM products WHERE id = %s AND workspace_id = %s FOR UPDATE",
                (product_id, caller.workspace_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise errors.not_found("That item")
            category = row[0]
            cur = await conn.execute(
                "SELECT 1 FROM field_definitions WHERE category = %s AND field_key = %s",
                (category, field_key),
            )
            if await cur.fetchone() is None:
                raise errors.err(
                    422, "unknown_field",
                    "That field does not belong to this product category.",
                    "Refresh the page and choose one of the fields shown.",
                )
            cur = await conn.execute(
                "SELECT id FROM field_values WHERE product_id = %s AND workspace_id = %s"
                " AND field_key = %s AND status IN ('accepted','corrected')"
                " ORDER BY reviewed_at DESC NULLS LAST, created_at DESC FOR UPDATE",
                (product_id, caller.workspace_id, field_key),
            )
            old_ids = [str(r[0]) for r in await cur.fetchall()]
            if not old_ids:
                raise errors.err(
                    409, "no_verified_value",
                    "This field has no verified value to replace.",
                    "Enter a new value instead.",
                )
            for old_id in old_ids:
                await conn.execute(
                    "UPDATE field_values SET status='rejected', reviewed_by=%s, reviewed_at=now()"
                    " WHERE id = %s",
                    (caller.user_id, old_id),
                )
            cur = await conn.execute(
                "INSERT INTO field_values (workspace_id, product_id, field_key, value, unit,"
                " status, document_id, location, extracted_by, reviewed_by, reviewed_at, supersedes_id)"
                " VALUES (%s,%s,%s,%s,%s,'corrected',NULL,NULL,'manual',%s,now(),%s) RETURNING id",
                (caller.workspace_id, product_id, field_key, body.value, body.unit,
                 caller.user_id, old_ids[0]),
            )
            new_id = str((await cur.fetchone())[0])
            await _audit(conn, caller.workspace_id, caller.user_id, "field.replaced", new_id,
                         json.dumps({"field_key": field_key, "replaced": old_ids}))
    return {"ok": True, "id": new_id}
