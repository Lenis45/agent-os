"""Authenticated FastAPI surface for the Amori Unified Intelligence Gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import artifact_store
from document_pipeline import extract_document
import ops_store
import request_store


APP_VERSION = "2.0.0"
TOKEN_FILE = Path.home() / ".config" / "amori" / "broker_token"
MAX_UPLOAD_BYTES = int(os.getenv("AMORI_BROKER_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


class RequestCreate(BaseModel):
    source: str = Field(pattern="^(telegram|hermes|opencode|terminal)$")
    actor_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=250_000)
    mode: str = Field(default="ask", pattern="^(ask|act)$")
    source_message_id: str = Field(default="", max_length=300)
    cwd: str = Field(default="", max_length=2000)
    target_device: str = Field(default="auto", max_length=100)
    artifact_ids: list[str] = Field(default_factory=list, max_length=20)


class WorkerHeartbeat(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    device: str = Field(min_length=1, max_length=100)
    capabilities: list[str]
    versions: dict[str, Any] = Field(default_factory=dict)
    auth_status: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class WorkerClaim(BaseModel):
    worker_id: str
    device: str
    capabilities: list[str]


class WorkerEvent(BaseModel):
    stage: str
    message: str = ""
    progress: Optional[int] = Field(default=None, ge=0, le=100)
    meta: dict[str, Any] = Field(default_factory=dict)


class WorkerComplete(BaseModel):
    status: str = Field(default="completed", pattern="^(completed|partial)$")
    result_text: str = ""
    evidence: list[Any] = Field(default_factory=list)


class WorkerFail(BaseModel):
    error_code: str = "execution_failed"
    error_message: str


class RequestConfirm(BaseModel):
    actor_id: str = Field(min_length=1, max_length=200)


def _expected_token() -> str:
    configured = os.getenv("AMORI_BROKER_TOKEN", "").strip()
    if configured:
        return configured
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def require_bearer(authorization: str = Header(default="")) -> None:
    expected = _expected_token()
    supplied = authorization.removeprefix("Bearer ").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Broker token is not configured")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _router_command(text: str, mode: str) -> list[str]:
    executable = os.getenv("AMORI_AI_CLI", "amori-ai")
    command = [executable, "--route-only", "--json"]
    if mode == "act":
        command.append("--act")
    command.append(text)
    return command


def route_prompt(text: str, mode: str) -> dict:
    """Get Router v2 decision; retry deterministic rules before failing."""
    command = _router_command(text, mode)
    attempts = [command, command[:-1] + ["--no-neural-route", command[-1]]]
    errors = []
    for candidate in attempts:
        completed = subprocess.run(candidate, capture_output=True, text=True, timeout=45, check=False)
        if completed.returncode == 0:
            payload = json.loads(completed.stdout)
            return payload.get("decision", payload)
        errors.append((completed.stderr or completed.stdout).strip()[-500:])
    raise RuntimeError("; ".join(error for error in errors if error) or "Router unavailable")


def _idempotency_key(payload: RequestCreate) -> str:
    if payload.source_message_id:
        raw = f"{payload.source}:{payload.actor_id}:{payload.source_message_id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return str(uuid.uuid4())


def _target_device(payload: RequestCreate, route: dict) -> str:
    if payload.target_device != "auto":
        return payload.target_device
    if payload.source == "opencode":
        return "macbook"
    target = str(route.get("target_device", "auto"))
    return "mac-mini" if target in {"auto", "current", "mac-mini"} else target


def submit(payload: RequestCreate) -> tuple[dict, bool]:
    route = route_prompt(payload.text, payload.mode)
    target_device = _target_device(payload, route)
    request, created = request_store.create_request(
        source=payload.source,
        actor_id=payload.actor_id,
        session_id=payload.session_id,
        prompt_text=payload.text,
        mode=payload.mode,
        idempotency_key=_idempotency_key(payload),
        source_message_id=payload.source_message_id,
        cwd=payload.cwd,
        target_device=target_device,
        route=route,
        input_artifact_ids=payload.artifact_ids,
    )
    if created and payload.mode == "act":
        request_store.set_status(str(request["id"]), "awaiting_confirmation")
        request_store.append_event(
            str(request["id"]), "awaiting_confirmation",
            "Требуется подтверждение действия", 20,
        )
        request = request_store.get_request(str(request["id"]))
    elif created and not request_store.worker_available(target_device):
        request_store.set_status(str(request["id"]), "waiting_for_device")
        request_store.append_event(
            str(request["id"]), "waiting_for_device",
            f"Ожидается исполнитель на {target_device}", 20,
        )
        request = request_store.get_request(str(request["id"]))
    return request, created


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ops_store.init()
    yield


app = FastAPI(title="Amori Request Broker", version=APP_VERSION, lifespan=lifespan)


def _public_artifact(artifact: dict) -> dict:
    public = {key: value for key, value in artifact.items() if key != "stored_path"}
    public["download_url"] = f"/v1/artifacts/{artifact['id']}/download"
    return public


async def _store_upload(file: UploadFile, owner_id: str, *, source: str, kind: str) -> dict:
    suffix = Path(file.filename or "artifact.bin").suffix
    size = 0
    temporary_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary_path = temporary.name
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Artifact is too large")
                temporary.write(chunk)
        artifact = artifact_store.store_file(
            temporary_path, file.filename or "artifact.bin", owner_id,
            source=source, kind=kind,
        )
        if kind == "input":
            extraction = extract_document(artifact.stored_path)
            if extraction.ok:
                artifact = artifact_store.attach_extracted_text(artifact, extraction.text)
        return artifact.to_dict()
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


@app.post("/v1/uploads", dependencies=[Depends(require_bearer)])
async def upload_input(owner_id: str, file: UploadFile = File(...)) -> dict:
    artifact = await _store_upload(file, owner_id, source="broker-upload", kind="input")
    return {"artifact": {key: value for key, value in artifact.items() if key != "stored_path"}}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "amori-request-broker", "version": APP_VERSION}


@app.post("/v1/requests", dependencies=[Depends(require_bearer)])
def create_request(payload: RequestCreate) -> dict:
    try:
        request, created = submit(payload)
    except (RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=503, detail=f"Router unavailable: {error}") from error
    return {"created": created, "request": request}


@app.get("/v1/requests/{request_id}", dependencies=[Depends(require_bearer)])
def read_request(request_id: str) -> dict:
    request = request_store.get_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    return {
        "request": request,
        "events": request_store.list_events(request_id),
        "artifacts": [_public_artifact(item) for item in request_store.list_artifacts(request_id)],
    }


@app.get("/v1/artifacts/{artifact_id}/download", dependencies=[Depends(require_bearer)])
def download_artifact(artifact_id: str):
    artifact = request_store.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = Path(artifact["stored_path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Artifact expired or missing")
    return FileResponse(path, media_type=artifact["mime_type"], filename=artifact["original_name"])


@app.get("/v1/requests/{request_id}/events", dependencies=[Depends(require_bearer)])
def read_events(request_id: str, after_id: int = 0) -> dict:
    return {"events": request_store.list_events(request_id, after_id)}


@app.get("/v1/sessions/{source}/{actor_id}/{session_id}/latest", dependencies=[Depends(require_bearer)])
def read_latest(source: str, actor_id: str, session_id: str) -> dict:
    request = request_store.latest_request(source, actor_id, session_id)
    return {"request": request}


@app.post("/v1/requests/{request_id}/cancel", dependencies=[Depends(require_bearer)])
def cancel(request_id: str) -> dict:
    return {"cancelled": request_store.cancel_request(request_id)}


@app.post("/v1/requests/{request_id}/confirm", dependencies=[Depends(require_bearer)])
def confirm(request_id: str, payload: RequestConfirm) -> dict:
    return {"confirmed": request_store.confirm_request(request_id, payload.actor_id)}


@app.post("/v1/workers/heartbeat", dependencies=[Depends(require_bearer)])
def worker_heartbeat(payload: WorkerHeartbeat) -> dict:
    request_store.heartbeat_worker(
        payload.worker_id, payload.device, payload.capabilities,
        versions=payload.versions, auth_status=payload.auth_status, meta=payload.meta,
    )
    return {"ok": True}


@app.post("/v1/workers/claim", dependencies=[Depends(require_bearer)])
def worker_claim(payload: WorkerClaim) -> dict:
    request_store.requeue_expired_leases()
    return {"request": request_store.claim_request(payload.worker_id, payload.device, payload.capabilities)}


@app.post("/v1/workers/{request_id}/events", dependencies=[Depends(require_bearer)])
def worker_event(request_id: str, payload: WorkerEvent) -> dict:
    event_id = request_store.append_event(request_id, payload.stage, payload.message, payload.progress, payload.meta)
    return {"event_id": event_id}


@app.post("/v1/workers/{request_id}/complete", dependencies=[Depends(require_bearer)])
def worker_complete(request_id: str, payload: WorkerComplete) -> dict:
    request_store.finish_request(
        request_id, status=payload.status, result_text=payload.result_text, evidence=payload.evidence
    )
    return {"ok": True}


@app.post("/v1/workers/{request_id}/fail", dependencies=[Depends(require_bearer)])
def worker_fail(request_id: str, payload: WorkerFail) -> dict:
    request_store.finish_request(
        request_id, status="failed", error_code=payload.error_code, error_message=payload.error_message
    )
    return {"ok": True}


@app.post("/v1/workers/{request_id}/artifacts", dependencies=[Depends(require_bearer)])
async def worker_artifact(request_id: str, owner_id: str, kind: str = "output", file: UploadFile = File(...)) -> dict:
    request = request_store.get_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Request not found")
    if owner_id != str(request["actor_id"]):
        raise HTTPException(status_code=403, detail="Artifact owner does not match request owner")
    artifact = await _store_upload(file, owner_id, source="worker", kind=kind)
    request_store.register_artifact(request_id, artifact)
    return {"artifact": _public_artifact(artifact)}
