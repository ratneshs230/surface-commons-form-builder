"""Form Builder — Commons guild's open-source replacement for Typeform / Tally
/ Google Forms paid plans.

Endpoints (all under ``/api/v1/commons/form``):

- ``POST /create``                — author a new form (auth, 2 SC).
- ``GET  /{form_id}``             — public schema fetch (free, no auth).
- ``POST /{form_id}/submit``      — anyone can submit a response (free).
- ``GET  /{form_id}/responses``   — owner-only list (auth, 1 SC / page).
- ``DELETE /{form_id}``           — owner-only soft-delete (auth, free).
- ``GET  /health``                — public probe for the landing page.

A form's owner is the ``X-Agent-Id`` that POSTed /create. Only that owner
can read responses or delete the form. We soft-delete forms (set
``deleted_at``) so prior submissions stay queryable for audit; the
SQL CASCADE on form_responses still applies if a hard-delete ever ships.

SC pricing:
  create        = 2 SC   (debited from creator's wallet)
  submit        = free   (the responder is unauthenticated; we never charge them)
  responses     = 1 SC / page-fetch
  delete        = free   (housekeeping)

Public URL convention is a Surface-rendered viewer that lives at
``https://commons.agentnation.in/forms/{form_id}``. v1 ships the JSON
schema endpoint only — the static HTML viewer is v2 work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.service_auth import require_service_key_and_agent
from app.models.agent import Agent
from app.services.ledger import (
    InsufficientFundsError,
    record_transaction,
)


router = APIRouter(prefix="/api/v1/commons/form", tags=["commons-form"])


# ── SC pricing ────────────────────────────────────────────────────────────
SC_COST = {
    "create": float(os.environ.get("FORM_SC_CREATE", "2")),
    "responses_page": float(os.environ.get("FORM_SC_RESPONSES_PAGE", "1")),
}

PUBLIC_BASE = os.environ.get(
    "FORM_PUBLIC_BASE", "https://commons.agentnation.in/forms"
)
RESPONSES_DEFAULT_LIMIT = 50
RESPONSES_MAX_LIMIT = 500
MAX_FIELDS_PER_FORM = 100
MAX_TITLE_LEN = 240
MAX_LABEL_LEN = 500
MAX_OPTIONS_PER_FIELD = 100
MAX_ANSWER_BYTES = 32 * 1024  # per-answer payload cap


SUPPORTED_FIELD_TYPES = {
    "text",
    "textarea",
    "email",
    "number",
    "single_choice",
    "multi_choice",
    "date",
    # "file_upload" deliberately omitted in v1 — needs object storage wiring.
}
CHOICE_FIELDS = {"single_choice", "multi_choice"}

_EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── Pydantic models ───────────────────────────────────────────────────────


class FormFieldIn(BaseModel):
    type: Literal[
        "text", "textarea", "email", "number",
        "single_choice", "multi_choice", "date",
    ]
    label: str = Field(..., min_length=1, max_length=MAX_LABEL_LEN)
    required: bool = False
    options: list[str] | None = None

    @field_validator("options")
    @classmethod
    def _check_options(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) > MAX_OPTIONS_PER_FIELD:
            raise ValueError(f"options exceeds {MAX_OPTIONS_PER_FIELD}")
        cleaned = [o.strip() for o in v if o and o.strip()]
        if not cleaned:
            raise ValueError("options must contain at least one non-empty value")
        return cleaned


class FormCreateIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=MAX_TITLE_LEN)
    fields: list[FormFieldIn] = Field(..., min_length=1, max_length=MAX_FIELDS_PER_FORM)


class FormSubmitIn(BaseModel):
    answers: dict[str, Any]


# ── Schema validation ─────────────────────────────────────────────────────


def _normalize_fields(raw: list[FormFieldIn]) -> list[dict]:
    """Assign deterministic field ids (f1..fN) and validate choice fields have options."""
    out: list[dict] = []
    for i, f in enumerate(raw, start=1):
        if f.type in CHOICE_FIELDS and not f.options:
            raise HTTPException(
                status_code=400,
                detail=f"field {i} ({f.type}) requires non-empty options",
            )
        rec: dict[str, Any] = {
            "id": f"f{i}",
            "type": f.type,
            "label": f.label,
            "required": bool(f.required),
        }
        if f.type in CHOICE_FIELDS:
            rec["options"] = list(f.options or [])
        out.append(rec)
    return out


def _validate_answers(schema: dict, answers: dict[str, Any]) -> dict[str, Any]:
    """Coerce + validate per the form schema. Drops keys not in schema."""
    fields = {f["id"]: f for f in schema.get("fields", [])}
    cleaned: dict[str, Any] = {}

    for fid, fdef in fields.items():
        if fid not in answers:
            if fdef.get("required"):
                raise HTTPException(
                    status_code=400,
                    detail=f"missing required field: {fid} ({fdef['label']})",
                )
            continue

        v = answers[fid]
        ftype = fdef["type"]

        # Empty-value handling
        if v is None or (isinstance(v, str) and not v.strip()):
            if fdef.get("required"):
                raise HTTPException(
                    status_code=400,
                    detail=f"empty required field: {fid} ({fdef['label']})",
                )
            continue

        try:
            cleaned[fid] = _coerce(ftype, v, fdef)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"invalid value for {fid}: {type(e).__name__}",
            )

        # Per-answer payload size cap (post-coercion)
        if len(json.dumps(cleaned[fid], default=str).encode("utf-8")) > MAX_ANSWER_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"answer {fid} exceeds {MAX_ANSWER_BYTES} bytes",
            )

    return cleaned


def _coerce(ftype: str, v: Any, fdef: dict) -> Any:
    if ftype in {"text", "textarea"}:
        if not isinstance(v, str):
            raise ValueError("string expected")
        return v
    if ftype == "email":
        if not isinstance(v, str) or not _EMAIL_RX.match(v.strip()):
            raise HTTPException(status_code=400, detail=f"invalid email: {fdef['id']}")
        return v.strip()
    if ftype == "number":
        if isinstance(v, bool):
            raise ValueError("bool not numeric")
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            try:
                return float(v) if "." in v else int(v)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"invalid number: {fdef['id']}"
                )
        raise ValueError("number expected")
    if ftype == "single_choice":
        if not isinstance(v, str) or v not in fdef.get("options", []):
            raise HTTPException(
                status_code=400,
                detail=f"value not in options: {fdef['id']}",
            )
        return v
    if ftype == "multi_choice":
        if not isinstance(v, list):
            raise ValueError("list expected")
        opts = set(fdef.get("options", []))
        bad = [x for x in v if x not in opts]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"values not in options for {fdef['id']}: {bad[:5]}",
            )
        return list(v)
    if ftype == "date":
        if isinstance(v, str):
            try:
                date.fromisoformat(v)
                return v
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"invalid ISO date: {fdef['id']}"
                )
        raise ValueError("ISO date string expected")
    raise ValueError(f"unsupported type: {ftype}")


# ── Helpers ───────────────────────────────────────────────────────────────


def _public_url(form_id: str) -> str:
    return f"{PUBLIC_BASE}/{form_id}"


def _hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    salt = date.today().isoformat()
    return hashlib.sha256(f"{ip}|{salt}".encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


async def _charge(
    db: AsyncSession,
    agent: Agent,
    *,
    amount: float,
    tool: str,
    ref: str,
) -> float:
    if amount <= 0:
        return 0.0
    try:
        await record_transaction(
            db,
            agent_id=agent.id,
            delta=-amount,
            reason=f"commons.form.{tool}",
            ref_id=ref,
            note=f"Form Builder: {tool}",
        )
    except InsufficientFundsError as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_sc",
                "balance": e.balance,
                "needed": amount,
                "tool": f"commons.form.{tool}",
            },
        )
    return amount


async def _log_tool_call(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_name: str,
    input_summary: dict,
    output_len: int,
    sc_cost: float,
    latency_ms: int,
    error: str | None = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO tool_calls
                (id, agent_id, tool_name, input_json, output_len,
                 sc_cost, latency_ms, error, created_at)
            VALUES
                (:id, :agent_id, :tool_name, CAST(:input_json AS JSONB),
                 :output_len, :sc_cost, :latency_ms, :error, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tool_name": tool_name,
            "input_json": json.dumps(input_summary, default=str),
            "output_len": output_len,
            "sc_cost": sc_cost,
            "latency_ms": latency_ms,
            "error": error,
        },
    )


async def _load_form(db: AsyncSession, form_id: str, *, include_deleted: bool = False) -> dict:
    try:
        uuid.UUID(form_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="form_not_found")
    row = (
        await db.execute(
            text(
                """
                SELECT id, title, schema, created_by, created_at, deleted_at
                FROM forms WHERE id = :id
                """
            ),
            {"id": form_id},
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="form_not_found")
    if row.deleted_at and not include_deleted:
        raise HTTPException(status_code=410, detail="form_deleted")
    schema = row.schema if isinstance(row.schema, dict) else json.loads(row.schema)
    return {
        "id": str(row.id),
        "title": row.title,
        "schema": schema,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }


# ── /health (public) ──────────────────────────────────────────────────────


@router.get("/health")
async def health() -> dict:
    return {
        "service": "commons.form",
        "version": "1.0.0",
        "field_types": sorted(SUPPORTED_FIELD_TYPES),
        "sc_cost": SC_COST,
        "max_fields_per_form": MAX_FIELDS_PER_FORM,
        "responses_max_limit": RESPONSES_MAX_LIMIT,
        "public_base": PUBLIC_BASE,
    }


# ── POST /create ──────────────────────────────────────────────────────────


@router.post("/create")
async def create_form(
    body: Annotated[FormCreateIn, ...],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    fields = _normalize_fields(body.fields)
    schema = {"fields": fields}

    await _charge(db, agent, amount=SC_COST["create"], tool="create", ref=ref)
    try:
        form_id = str(uuid.uuid4())
        await db.execute(
            text(
                """
                INSERT INTO forms (id, title, schema, created_by)
                VALUES (:id, :title, CAST(:schema AS JSONB), :created_by)
                """
            ),
            {
                "id": form_id,
                "title": body.title,
                "schema": json.dumps(schema),
                "created_by": agent.id,
            },
        )
        out = {
            "form_id": form_id,
            "public_url": _public_url(form_id),
            "title": body.title,
            "field_count": len(fields),
            "sc_charged": SC_COST["create"],
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.form.create",
            input_summary={"title": body.title, "field_count": len(fields)},
            output_len=len(json.dumps(out)),
            sc_cost=SC_COST["create"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500, detail={"error": "create_failed", "msg": str(e)[:200]}
        )


# ── GET /{form_id} (public schema) ────────────────────────────────────────


@router.get("/{form_id}")
async def get_form(form_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    f = await _load_form(db, form_id)
    return {
        "form_id": f["id"],
        "title": f["title"],
        "fields": f["schema"].get("fields", []),
        "public_url": _public_url(f["id"]),
        "created_at": f["created_at"],
    }


# ── POST /{form_id}/submit (public) ───────────────────────────────────────


@router.post("/{form_id}/submit")
async def submit_response(
    form_id: str,
    body: Annotated[FormSubmitIn, ...],
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    f = await _load_form(db, form_id)
    cleaned = _validate_answers(f["schema"], body.answers or {})
    response_id = str(uuid.uuid4())
    ip_hash = _hash_ip(_client_ip(request))
    try:
        await db.execute(
            text(
                """
                INSERT INTO form_responses (id, form_id, answers, ip_hash)
                VALUES (:id, :form_id, CAST(:answers AS JSONB), :ip_hash)
                """
            ),
            {
                "id": response_id,
                "form_id": form_id,
                "answers": json.dumps(cleaned, default=str),
                "ip_hash": ip_hash,
            },
        )
        await db.commit()
        return {"response_id": response_id, "form_id": form_id, "accepted": True}
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500, detail={"error": "submit_failed", "msg": str(e)[:200]}
        )


# ── GET /{form_id}/responses (owner-only) ─────────────────────────────────


@router.get("/{form_id}/responses")
async def list_responses(
    form_id: str,
    limit: int = RESPONSES_DEFAULT_LIMIT,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    f = await _load_form(db, form_id, include_deleted=True)
    if f["created_by"] != agent.id:
        raise HTTPException(status_code=403, detail="not_form_owner")

    limit = max(1, min(int(limit), RESPONSES_MAX_LIMIT))
    offset = max(0, int(offset))
    t0 = time.monotonic()
    ref = str(uuid.uuid4())

    await _charge(db, agent, amount=SC_COST["responses_page"], tool="responses", ref=ref)
    try:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT id, answers, submitted_at
                    FROM form_responses
                    WHERE form_id = :fid
                    ORDER BY submitted_at DESC
                    LIMIT :lim OFFSET :off
                    """
                ),
                {"fid": form_id, "lim": limit, "off": offset},
            )
        ).all()
        total = (
            await db.execute(
                text("SELECT COUNT(*) FROM form_responses WHERE form_id = :fid"),
                {"fid": form_id},
            )
        ).scalar_one()

        items = [
            {
                "response_id": str(r.id),
                "answers": r.answers if isinstance(r.answers, dict) else json.loads(r.answers),
                "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            }
            for r in rows
        ]
        out = {
            "form_id": form_id,
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "items": items,
            "sc_charged": SC_COST["responses_page"],
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.form.responses",
            input_summary={"form_id": form_id, "limit": limit, "offset": offset, "total": int(total)},
            output_len=len(json.dumps(out, default=str)),
            sc_cost=SC_COST["responses_page"],
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500, detail={"error": "responses_failed", "msg": str(e)[:200]}
        )


# ── DELETE /{form_id} (owner-only soft-delete) ────────────────────────────


@router.delete("/{form_id}")
async def delete_form(
    form_id: str,
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    f = await _load_form(db, form_id, include_deleted=True)
    if f["created_by"] != agent.id:
        raise HTTPException(status_code=403, detail="not_form_owner")
    if f["deleted_at"]:
        return {"form_id": form_id, "deleted": True, "already": True}
    try:
        await db.execute(
            text("UPDATE forms SET deleted_at = NOW() WHERE id = :id"),
            {"id": form_id},
        )
        await db.commit()
        return {"form_id": form_id, "deleted": True}
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500, detail={"error": "delete_failed", "msg": str(e)[:200]}
        )
