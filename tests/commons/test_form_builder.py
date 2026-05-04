"""Tests for /commons/form — Surface Form Builder.

Two layers:

1. Pure-Python tests for the schema-validator + answer-coercer (no auth,
   no DB, no network). These are the authoritative coverage of the
   field-type grammar and always run.

2. HTTP integration tests against a running surface-api at
   127.0.0.1:3000. They exercise create → get → submit → list →
   delete end-to-end. Skipped unless SURFACE_SERVICE_KEY +
   SURFACE_TEST_AGENT_ID are set.
"""
from __future__ import annotations

import os
import uuid

import httpx
import pytest

from app.api.commons.form_builder import (
    _coerce,
    _normalize_fields,
    _validate_answers,
    FormFieldIn,
)


BASE = os.environ.get("SURFACE_BASE_URL", "http://127.0.0.1:3000")
SVC_KEY = os.environ.get("SURFACE_SERVICE_KEY") or os.environ.get("SERVICE_API_KEY")
AGENT_ID = os.environ.get("SURFACE_TEST_AGENT_ID")


def _client() -> httpx.Client:
    if not SVC_KEY or not AGENT_ID:
        pytest.skip("SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID required")
    return httpx.Client(
        base_url=BASE,
        headers={"X-Service-Key": SVC_KEY, "X-Agent-Id": AGENT_ID},
        timeout=30.0,
    )


# ── Pure: schema normalization ────────────────────────────────────────────


def test_normalize_assigns_field_ids():
    fields = [
        FormFieldIn(type="text", label="Name", required=True),
        FormFieldIn(type="email", label="Email", required=True),
    ]
    out = _normalize_fields(fields)
    assert [f["id"] for f in out] == ["f1", "f2"]
    assert out[0]["type"] == "text"
    assert out[1]["required"] is True


def test_normalize_choice_requires_options():
    fields = [FormFieldIn(type="single_choice", label="Color", required=True)]
    with pytest.raises(Exception):
        _normalize_fields(fields)


def test_normalize_keeps_options_for_choice_fields():
    fields = [
        FormFieldIn(type="single_choice", label="Color", options=["red", "blue"]),
    ]
    out = _normalize_fields(fields)
    assert out[0]["options"] == ["red", "blue"]


# ── Pure: answer validation + coercion ────────────────────────────────────


SCHEMA = {
    "fields": [
        {"id": "f1", "type": "text", "label": "Name", "required": True},
        {"id": "f2", "type": "email", "label": "Email", "required": True},
        {"id": "f3", "type": "number", "label": "Age", "required": False},
        {"id": "f4", "type": "single_choice", "label": "Color",
         "required": False, "options": ["red", "blue"]},
        {"id": "f5", "type": "multi_choice", "label": "Hobbies",
         "required": False, "options": ["a", "b", "c"]},
        {"id": "f6", "type": "date", "label": "DOB", "required": False},
    ]
}


def test_validate_happy_path():
    out = _validate_answers(SCHEMA, {
        "f1": "Ratnesh",
        "f2": "r@example.com",
        "f3": "42",
        "f4": "red",
        "f5": ["a", "c"],
        "f6": "1990-01-15",
    })
    assert out["f1"] == "Ratnesh"
    assert out["f2"] == "r@example.com"
    assert out["f3"] == 42
    assert out["f4"] == "red"
    assert out["f5"] == ["a", "c"]
    assert out["f6"] == "1990-01-15"


def test_validate_required_missing():
    with pytest.raises(Exception):
        _validate_answers(SCHEMA, {"f2": "r@example.com"})


def test_validate_invalid_email():
    with pytest.raises(Exception):
        _validate_answers(SCHEMA, {"f1": "X", "f2": "not-an-email"})


def test_validate_choice_value_not_in_options():
    with pytest.raises(Exception):
        _validate_answers(SCHEMA, {"f1": "X", "f2": "r@x.io", "f4": "purple"})


def test_validate_multi_choice_partial_invalid():
    with pytest.raises(Exception):
        _validate_answers(SCHEMA, {"f1": "X", "f2": "r@x.io", "f5": ["a", "z"]})


def test_validate_invalid_date():
    with pytest.raises(Exception):
        _validate_answers(SCHEMA, {"f1": "X", "f2": "r@x.io", "f6": "not-a-date"})


def test_validate_drops_unknown_fields():
    out = _validate_answers(SCHEMA, {
        "f1": "X", "f2": "r@x.io", "ghost": "should-vanish",
    })
    assert "ghost" not in out


def test_coerce_number_accepts_int_and_float():
    fdef = {"id": "f", "type": "number"}
    assert _coerce("number", "10", fdef) == 10
    assert _coerce("number", "10.5", fdef) == 10.5
    assert _coerce("number", 7, fdef) == 7


# ── HTTP: /health (public) ────────────────────────────────────────────────


def test_health_public():
    r = httpx.get(f"{BASE}/api/v1/commons/form/health", timeout=10)
    if r.status_code == 404:
        pytest.skip("surface-api not running with form_builder router mounted")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["service"] == "commons.form"
    assert "text" in j["field_types"]
    assert "single_choice" in j["field_types"]
    assert j["sc_cost"]["create"] >= 0


# ── HTTP: end-to-end create → get → submit → list → delete ────────────────


def _form_payload() -> dict:
    return {
        "title": f"Test form {uuid.uuid4().hex[:8]}",
        "fields": [
            {"type": "text", "label": "Name", "required": True},
            {"type": "email", "label": "Email", "required": True},
            {"type": "single_choice", "label": "Color",
             "required": True, "options": ["red", "blue", "green"]},
            {"type": "number", "label": "Age", "required": False},
        ],
    }


def test_create_get_submit_list_delete_e2e():
    with _client() as c:
        # create
        r = c.post("/api/v1/commons/form/create", json=_form_payload())
        if r.status_code == 404:
            pytest.skip("router not mounted")
        if r.status_code == 402:
            pytest.skip(f"insufficient SC for test agent: {r.text}")
        assert r.status_code == 200, r.text
        cj = r.json()
        form_id = cj["form_id"]
        assert "public_url" in cj
        assert cj["field_count"] == 4

        # get (public)
        r = httpx.get(f"{BASE}/api/v1/commons/form/{form_id}", timeout=10)
        assert r.status_code == 200, r.text
        gj = r.json()
        assert gj["form_id"] == form_id
        ids = {f["id"] for f in gj["fields"]}
        assert ids == {"f1", "f2", "f3", "f4"}

        # submit (public)
        sub = {"answers": {"f1": "Test", "f2": "t@x.io", "f3": "red", "f4": "30"}}
        r = httpx.post(
            f"{BASE}/api/v1/commons/form/{form_id}/submit",
            json=sub, timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] is True

        # submit with bad value rejected
        bad = {"answers": {"f1": "T", "f2": "t@x.io", "f3": "purple"}}
        r = httpx.post(
            f"{BASE}/api/v1/commons/form/{form_id}/submit",
            json=bad, timeout=10,
        )
        assert r.status_code == 400

        # submit missing required rejected
        miss = {"answers": {"f1": "T", "f3": "red"}}
        r = httpx.post(
            f"{BASE}/api/v1/commons/form/{form_id}/submit",
            json=miss, timeout=10,
        )
        assert r.status_code == 400

        # list responses (owner)
        r = c.get(f"/api/v1/commons/form/{form_id}/responses?limit=5")
        assert r.status_code == 200, r.text
        lj = r.json()
        assert lj["total"] >= 1
        assert len(lj["items"]) >= 1
        assert "answers" in lj["items"][0]

        # list without auth rejected
        r = httpx.get(
            f"{BASE}/api/v1/commons/form/{form_id}/responses", timeout=10,
        )
        assert r.status_code in (401, 422)

        # delete (owner)
        r = c.delete(f"/api/v1/commons/form/{form_id}")
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] is True

        # get after delete → 410
        r = httpx.get(f"{BASE}/api/v1/commons/form/{form_id}", timeout=10)
        assert r.status_code == 410


def test_create_rejects_choice_field_without_options():
    with _client() as c:
        bad = {
            "title": "bad",
            "fields": [
                {"type": "single_choice", "label": "X", "required": True},
            ],
        }
        r = c.post("/api/v1/commons/form/create", json=bad)
        if r.status_code == 404:
            pytest.skip("router not mounted")
        if r.status_code == 402:
            pytest.skip("insufficient SC")
        assert r.status_code == 400


def test_get_unknown_form_404():
    bogus = str(uuid.uuid4())
    r = httpx.get(f"{BASE}/api/v1/commons/form/{bogus}", timeout=10)
    if r.status_code in (404,) and "not_found" not in r.text and "deleted" not in r.text:
        # Either router missing (404 from FastAPI) or our 404. Cheap probe to disambiguate.
        h = httpx.get(f"{BASE}/api/v1/commons/form/health", timeout=5)
        if h.status_code == 404:
            pytest.skip("router not mounted")
    assert r.status_code == 404


def test_create_auth_rejected_without_key():
    r = httpx.post(
        f"{BASE}/api/v1/commons/form/create",
        json={"title": "x", "fields": [{"type": "text", "label": "X"}]},
        timeout=10,
    )
    if r.status_code == 404:
        pytest.skip("router not mounted")
    assert r.status_code in (401, 422)
