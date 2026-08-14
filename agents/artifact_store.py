"""Private local artifact storage shared by Telegram and the request broker."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DEFAULT_RETENTION_DAYS = 30


def artifact_root() -> Path:
    configured = os.getenv("AMORI_ARTIFACT_ROOT")
    root = Path(configured).expanduser() if configured else Path.home() / ".local" / "share" / "amori" / "artifacts"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _safe_name(value: str) -> str:
    name = Path(value or "artifact.bin").name
    name = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", name).strip(" .")
    return name[:180] or "artifact.bin"


def _owner_key(owner: str) -> str:
    return hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:24]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Artifact:
    id: str
    owner: str
    source: str
    original_name: str
    stored_path: str
    mime_type: str
    size_bytes: int
    sha256: str
    created_at: str
    expires_at: str
    kind: str = "input"
    extracted_text_path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def store_file(
    source_path: str | Path,
    original_name: str,
    owner: str,
    *,
    source: str,
    kind: str = "input",
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> Artifact:
    source_file = Path(source_path)
    if not source_file.is_file() or source_file.is_symlink():
        raise ValueError("Artifact source must be a regular file")
    artifact_id = str(uuid.uuid4())
    directory = artifact_root() / artifact_id
    directory.mkdir(mode=0o700)
    destination = directory / _safe_name(original_name)
    shutil.copyfile(source_file, destination)
    destination.chmod(0o600)
    now = datetime.now(timezone.utc)
    artifact = Artifact(
        id=artifact_id,
        owner=str(owner),
        source=source,
        original_name=Path(original_name).name,
        stored_path=str(destination),
        mime_type=mimetypes.guess_type(destination.name)[0] or "application/octet-stream",
        size_bytes=destination.stat().st_size,
        sha256=_sha256(destination),
        created_at=now.isoformat(),
        expires_at=(now + timedelta(days=retention_days)).isoformat(),
        kind=kind,
    )
    (directory / "manifest.json").write_text(
        json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "manifest.json").chmod(0o600)
    set_active(owner, artifact.id)
    return artifact


def attach_extracted_text(artifact: Artifact, text: str) -> Artifact:
    path = Path(artifact.stored_path).parent / "extracted.txt"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    updated = Artifact(**{**artifact.to_dict(), "extracted_text_path": str(path)})
    (path.parent / "manifest.json").write_text(
        json.dumps(updated.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return updated


def set_active(owner: str, artifact_id: str) -> None:
    index = artifact_root() / "active"
    index.mkdir(mode=0o700, exist_ok=True)
    path = index / f"{_owner_key(owner)}.json"
    path.write_text(json.dumps({"artifact_id": artifact_id}), encoding="utf-8")
    path.chmod(0o600)


def get_artifact(artifact_id: str) -> Optional[Artifact]:
    if not re.fullmatch(r"[0-9a-f-]{36}", artifact_id or ""):
        return None
    manifest = artifact_root() / artifact_id / "manifest.json"
    try:
        return Artifact(**json.loads(manifest.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def get_active(owner: str) -> Optional[Artifact]:
    path = artifact_root() / "active" / f"{_owner_key(owner)}.json"
    try:
        artifact_id = json.loads(path.read_text(encoding="utf-8"))["artifact_id"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    artifact = get_artifact(str(artifact_id))
    if artifact and artifact.owner == str(owner):
        return artifact
    return None


def cleanup_expired(now: Optional[datetime] = None) -> int:
    current = now or datetime.now(timezone.utc)
    removed = 0
    for child in artifact_root().iterdir():
        if not child.is_dir() or child.name == "active":
            continue
        artifact = get_artifact(child.name)
        if not artifact:
            continue
        try:
            expires = datetime.fromisoformat(artifact.expires_at)
        except ValueError:
            continue
        if expires <= current:
            shutil.rmtree(child)
            removed += 1
    return removed
