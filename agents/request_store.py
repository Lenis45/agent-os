"""PostgreSQL persistence and lease semantics for the Amori request broker."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from psycopg2.extras import RealDictCursor

import ops_store


TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
WORKER_FRESHNESS_SECONDS = 45


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _row(row) -> Optional[dict]:
    return dict(row) if row else None


def create_request(
    *, source: str, actor_id: str, session_id: str, prompt_text: str,
    mode: str, idempotency_key: str, source_message_id: str = "",
    cwd: str = "", target_device: str = "auto", route: Optional[dict] = None,
    input_artifact_ids: Optional[list[str]] = None, parent_request_id: str = "",
) -> tuple[dict, bool]:
    request_id = str(uuid.uuid4())
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        parent = None
        if parent_request_id:
            cur.execute(
                """SELECT * FROM smart_requests
                   WHERE id=%s AND source=%s AND actor_id=%s AND session_id=%s""",
                (parent_request_id, source, actor_id, session_id),
            )
            parent = cur.fetchone()
            if not parent:
                raise ValueError("Continuation parent does not belong to this session")
        thread_id = str((parent or {}).get("thread_id") or (parent or {}).get("id") or request_id)
        artifacts = input_artifact_ids or ((parent or {}).get("input_artifact_ids") or [])
        cur.execute(
            """
            INSERT INTO smart_requests (
                id, source, actor_id, session_id, source_message_id,
                idempotency_key, prompt_text, mode, cwd, target_device, route,
                input_artifact_ids, parent_request_id, thread_id, status
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,'queued')
            ON CONFLICT (idempotency_key) DO NOTHING RETURNING *
            """,
            (
                request_id, source, actor_id, session_id, source_message_id or None,
                idempotency_key, prompt_text, mode, cwd or None, target_device,
                _json(route or {}), _json(artifacts), parent_request_id or None, thread_id,
            ),
        )
        row = cur.fetchone()
        created = row is not None
        if not row:
            cur.execute("SELECT * FROM smart_requests WHERE idempotency_key=%s", (idempotency_key,))
            row = cur.fetchone()
        if created:
            cur.execute(
                """INSERT INTO smart_sessions(
                       source, actor_id, session_id, current_thread_id, last_request_id, reset_at
                   ) VALUES (%s,%s,%s,%s,%s,NULL)
                   ON CONFLICT (source, actor_id, session_id) DO UPDATE SET
                       current_thread_id=EXCLUDED.current_thread_id,
                       last_request_id=EXCLUDED.last_request_id,
                       reset_at=NULL,
                       updated_at=now()""",
                (source, actor_id, session_id, thread_id, request_id),
            )
        conn.commit()
        result = _row(row)
    finally:
        conn.close()
    if created:
        append_event(str(result["id"]), "accepted", "Запрос принят", 5)
        if parent_request_id:
            append_event(
                str(result["id"]), "continued", "Продолжение текущей задачи", 10,
                {"parent_request_id": parent_request_id, "thread_id": thread_id},
            )
        append_event(str(result["id"]), "routed", "Маршрут выбран", 15, {"route": route or {}})
        # Events are audit stages; the request remains claimable.
        set_status(str(result["id"]), "queued")
    return result, created


def get_request(request_id: str) -> Optional[dict]:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM smart_requests WHERE id=%s", (request_id,))
        return _row(cur.fetchone())
    finally:
        conn.close()


def latest_request(source: str, actor_id: str, session_id: str) -> Optional[dict]:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """SELECT last_request_id FROM smart_sessions
               WHERE source=%s AND actor_id=%s AND session_id=%s""",
            (source, actor_id, session_id),
        )
        state = cur.fetchone()
        if state is not None:
            if not state["last_request_id"]:
                return None
            cur.execute("SELECT * FROM smart_requests WHERE id=%s", (state["last_request_id"],))
            return _row(cur.fetchone())
        cur.execute(
            """SELECT * FROM smart_requests
               WHERE source=%s AND actor_id=%s AND session_id=%s
               ORDER BY created_at DESC LIMIT 1""",
            (source, actor_id, session_id),
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


def reset_session(source: str, actor_id: str, session_id: str) -> None:
    """Start a new topic without deleting the request audit trail."""
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO smart_sessions(
                   source, actor_id, session_id, current_thread_id, last_request_id, reset_at
               ) VALUES (%s,%s,%s,NULL,NULL,now())
               ON CONFLICT (source, actor_id, session_id) DO UPDATE SET
                   current_thread_id=NULL, last_request_id=NULL,
                   reset_at=now(), updated_at=now()""",
            (source, actor_id, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_thread_requests(thread_id: str, *, before_request_id: str = "", limit: int = 4) -> list[dict]:
    """Return a bounded, chronological context window for one task thread."""
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        before_clause = ""
        params: list[Any] = [thread_id]
        if before_request_id:
            before_clause = "AND created_at < (SELECT created_at FROM smart_requests WHERE id=%s)"
            params.append(before_request_id)
        params.append(max(1, min(limit, 10)))
        cur.execute(
            f"""SELECT * FROM (
                    SELECT * FROM smart_requests
                    WHERE thread_id=%s {before_clause}
                    ORDER BY created_at DESC LIMIT %s
                ) recent ORDER BY created_at""",
            params,
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def set_status(request_id: str, status: str) -> None:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE smart_requests SET status=%s, updated_at=now() WHERE id=%s", (status, request_id))
        conn.commit()
    finally:
        conn.close()


def list_events(request_id: str, after_id: int = 0) -> list[dict]:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM smart_request_events WHERE request_id=%s AND id>%s ORDER BY id",
            (request_id, after_id),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def append_event(
    request_id: str, stage: str, message: str = "", progress: Optional[int] = None,
    meta: Optional[dict] = None,
) -> int:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO smart_request_events(request_id, stage, message, progress, meta)
               VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING id""",
            (request_id, stage, message, progress, _json(meta or {})),
        )
        event_id = cur.fetchone()[0]
        conn.commit()
        return event_id
    finally:
        conn.close()


def heartbeat_worker(
    worker_id: str, device: str, capabilities: list[str], *,
    versions: Optional[dict] = None, auth_status: Optional[dict] = None,
    meta: Optional[dict] = None,
) -> None:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO smart_workers(worker_id, device, capabilities, versions, auth_status, meta)
            VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
            ON CONFLICT (worker_id) DO UPDATE SET
                device=EXCLUDED.device, capabilities=EXCLUDED.capabilities,
                versions=EXCLUDED.versions, auth_status=EXCLUDED.auth_status,
                meta=EXCLUDED.meta, status='online', last_seen=now()
            """,
            (worker_id, device, _json(capabilities), _json(versions or {}), _json(auth_status or {}), _json(meta or {})),
        )
        cur.execute(
            """UPDATE smart_requests SET lease_expires_at=now()+interval '90 seconds'
               WHERE leased_by=%s AND status='running'""",
            (worker_id,),
        )
        conn.commit()
    finally:
        conn.close()


def worker_available(device: str) -> bool:
    """Return whether a fresh worker can accept work for the target device."""
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM smart_workers
                   WHERE status='online'
                     AND last_seen > now()-(%s * interval '1 second')
                     AND device IN ('auto','current',%s)
               )""",
            (WORKER_FRESHNESS_SECONDS, device),
        )
        return bool(cur.fetchone()[0])
    finally:
        conn.close()


def renew_lease(worker_id: str, request_id: str) -> bool:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE smart_requests SET lease_expires_at=now()+interval '90 seconds', updated_at=now()
               WHERE id=%s AND leased_by=%s AND status='running'""",
            (request_id, worker_id),
        )
        changed = cur.rowcount > 0
        cur.execute("UPDATE smart_workers SET last_seen=now() WHERE worker_id=%s", (worker_id,))
        conn.commit()
        return changed
    finally:
        conn.close()


def claim_request(worker_id: str, device: str, capabilities: list[str]) -> Optional[dict]:
    offered = set(capabilities)
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT child.* FROM smart_requests child
            WHERE child.status IN ('queued','waiting_for_device')
              AND child.target_device IN ('auto','current',%s)
              AND (
                  child.parent_request_id IS NULL OR EXISTS (
                      SELECT 1 FROM smart_requests parent
                      WHERE parent.id=child.parent_request_id
                        AND (
                            parent.status IN ('completed','partial','failed','cancelled') OR
                            (parent.status='awaiting_confirmation' AND child.mode='ask')
                        )
                  )
              )
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 20
            """,
            (device,),
        )
        selected = None
        for row in cur.fetchall():
            required = set((row.get("route") or {}).get("required_capabilities", []))
            if required.issubset(offered):
                selected = row
                break
        if not selected:
            conn.rollback()
            return None
        cur.execute(
            """
            UPDATE smart_requests SET status='running', leased_by=%s,
                lease_expires_at=now()+interval '90 seconds', attempts=attempts+1,
                started_at=COALESCE(started_at,now()), updated_at=now()
            WHERE id=%s RETURNING *
            """,
            (worker_id, selected["id"]),
        )
        claimed = _row(cur.fetchone())
        cur.execute(
            "UPDATE smart_workers SET active_request_id=%s, last_seen=now() WHERE worker_id=%s",
            (selected["id"], worker_id),
        )
        conn.commit()
    finally:
        conn.close()
    append_event(str(selected["id"]), "running", f"Выполняет {worker_id}", 30)
    return claimed


def requeue_expired_leases() -> int:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE smart_requests SET status=CASE WHEN attempts >= 2 THEN 'failed' ELSE 'queued' END,
                error_code=CASE WHEN attempts >= 2 THEN 'worker_lost' ELSE error_code END,
                error_message=CASE WHEN attempts >= 2 THEN 'Исполнитель потерял соединение' ELSE error_message END,
                leased_by=NULL, lease_expires_at=NULL, updated_at=now()
            WHERE status='running' AND lease_expires_at < now()
            """
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def finish_request(
    request_id: str, *, status: str, result_text: str = "", evidence: Optional[list] = None,
    error_code: str = "", error_message: str = "",
) -> bool:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"Invalid terminal status: {status}")
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE smart_requests SET status=%s, result_text=%s, evidence=%s::jsonb,
                error_code=%s, error_message=%s, finished_at=now(), updated_at=now(),
                lease_expires_at=NULL WHERE id=%s AND status <> 'cancelled'
            """,
            (status, result_text or None, _json(evidence or []), error_code or None, error_message or None, request_id),
        )
        changed = cur.rowcount > 0
        cur.execute("UPDATE smart_workers SET active_request_id=NULL WHERE active_request_id=%s", (request_id,))
        conn.commit()
    finally:
        conn.close()
    if changed:
        append_event(request_id, status, "Готово" if status == "completed" else error_message, 100)
    return changed


def cancel_request(request_id: str) -> bool:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE smart_requests SET status='cancelled', finished_at=now(), updated_at=now()
               WHERE id=%s AND status NOT IN ('completed','partial','failed','cancelled')""",
            (request_id,),
        )
        changed = cur.rowcount > 0
        conn.commit()
    finally:
        conn.close()
    if changed:
        append_event(request_id, "cancelled", "Запрос отменён", 100)
    return changed


def confirm_request(request_id: str, actor_id: str) -> bool:
    """Release an explicitly approved action to the worker queue."""
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE smart_requests SET status=CASE
                   WHEN EXISTS (
                       SELECT 1 FROM smart_workers
                       WHERE status='online'
                         AND last_seen > now()-(%s * interval '1 second')
                         AND device IN ('auto','current',smart_requests.target_device)
                   ) THEN 'queued' ELSE 'waiting_for_device' END,
                   updated_at=now()
               WHERE id=%s AND actor_id=%s AND status='awaiting_confirmation'""",
            (WORKER_FRESHNESS_SECONDS, request_id, actor_id),
        )
        changed = cur.rowcount > 0
        conn.commit()
    finally:
        conn.close()
    if changed:
        append_event(request_id, "confirmed", "Действие подтверждено", 20)
    return changed


def register_artifact(request_id: str, artifact: dict, meta: Optional[dict] = None) -> None:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO smart_artifacts(
                id, request_id, owner_id, kind, original_name, stored_path,
                mime_type, size_bytes, sha256, expires_at, meta
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                artifact["id"], request_id, artifact["owner"], artifact.get("kind", "output"),
                artifact["original_name"], artifact["stored_path"], artifact["mime_type"],
                artifact["size_bytes"], artifact["sha256"], artifact["expires_at"], _json(meta or {}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_artifacts(request_id: str) -> list[dict]:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM smart_artifacts WHERE request_id=%s ORDER BY created_at", (request_id,))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_artifact(artifact_id: str) -> Optional[dict]:
    conn = ops_store.get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM smart_artifacts WHERE id=%s", (artifact_id,))
        return _row(cur.fetchone())
    finally:
        conn.close()
