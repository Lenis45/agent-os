"""
content_factory — конвейер контента для продаж Amori (Фаза 2).

Поток: бриф → текст (копирайтер) → визуальный бриф (дизайнер) → ревью →
аппрув → публикация. Использует доменных воркеров из worker_handlers (Фаза 3).

Аппрув-гейт: кнопки в дашборде (:8099) + превью в Telegram (одностороннее).
Публикация: реальная в Telegram-канал, если задан TELEGRAM_CHANNEL_ID; иначе
контент сохраняется со статусом published как «готово к ручной публикации»
(VK/landing/email требуют своих токенов — заложены хуки).

CLI:
  python3 content_factory.py "бриф" [channel] [kind]   — создать на аппрув
  python3 content_factory.py approve <id>              — одобрить + опубликовать
  python3 content_factory.py reject  <id> [причина]    — отклонить
  python3 content_factory.py publish <id>              — опубликовать
"""
import os
import sys
import json
import urllib.request

import ops_store
import notify
import report as report_mod
import worker_handlers

CHANNELS = {"telegram", "vk", "email", "landing", "ad"}
KINDS = {"post", "email", "ad_creative", "landing"}


def _conn():
    return ops_store.get_conn()


def create(brief, channel="telegram", kind="post", project_id=None, review=True) -> int:
    """Сгенерировать единицу контента и положить на аппрув (status=pending)."""
    channel = channel if channel in CHANNELS else "telegram"
    kind = kind if kind in KINDS else "post"
    spec = f"Канал: {channel}. Формат: {kind}.\n\nБриф: {brief}"

    body = worker_handlers.content_writer({"title": f"{kind} для {channel}", "spec": spec})
    image_brief = worker_handlers.content_designer({
        "title": f"визуал для {kind}", "spec": spec,
        "deps_results": [{"id": 0, "title": "текст контента", "result": body}],
    })
    rev = ""
    if review:
        rev = worker_handlers.content_reviewer({
            "title": "ревью контента", "spec": spec,
            "deps_results": [
                {"id": 0, "title": "текст контента", "result": body},
                {"id": 1, "title": "визуальный бриф", "result": image_brief},
            ],
        })

    cid = _insert(channel, kind, brief, body, image_brief, rev, project_id)
    _notify_preview(cid, channel, kind, body, image_brief)
    report_mod.report(
        "content_factory", kind="content",
        title=f"Контент на аппрув: {kind}/{channel} #{cid}",
        summary=(body or "")[:400], body=body, project_id=project_id,
        meta={"content_id": cid, "channel": channel, "kind": kind, "status": "pending"},
    )
    print(f"[content_factory] #{cid} {kind}/{channel} → pending")
    return cid


def _insert(channel, kind, brief, body, image_brief, rev, project_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        title = (body or "").strip().split("\n")[0][:120]
        cur.execute(
            "INSERT INTO content_items(project_id, channel, kind, brief, title, body, "
            "image_brief, review, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id",
            (project_id, channel, kind, brief, title, body, image_brief, rev),
        )
        cid = cur.fetchone()[0]
        conn.commit()
        return cid
    finally:
        conn.close()


def _notify_preview(cid, channel, kind, body, image_brief):
    txt = (f"🏭 Контент на аппрув #{cid} ({kind}/{channel})\n\n{(body or '')[:1500]}\n\n"
           f"🎨 Визуал: {(image_brief or '')[:300]}\n\n"
           f"👉 Одобрить/отклонить в дашборде :8099")
    try:
        notify.send(txt, level="info")
    except Exception:
        pass


def get(cid):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, project_id, channel, kind, brief, title, body, image_brief, "
            "review, status FROM content_items WHERE id=%s", (cid,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d[0] for d in (cur.description or [])], row))
    finally:
        conn.close()


def _set_status(cid, status, stamp=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        extra = f", {stamp}=now()" if stamp else ""
        cur.execute(f"UPDATE content_items SET status=%s, updated_at=now(){extra} WHERE id=%s",
                    (status, cid))
        conn.commit()
    finally:
        conn.close()


def approve(cid):
    item = get(cid)
    if not item:
        return {"ok": False, "error": "not found"}
    _set_status(cid, "approved", "approved_at")
    return {"ok": True, "published": publish(cid)}


def reject(cid, reason=""):
    if not get(cid):
        return {"ok": False, "error": "not found"}
    _set_status(cid, "rejected")
    report_mod.report("content_factory", kind="content", title=f"Контент отклонён #{cid}",
                      summary=str(reason)[:300], meta={"content_id": cid})
    return {"ok": True}


def publish(cid):
    item = get(cid)
    if not item:
        return {"ok": False, "error": "not found"}
    ok, info = _do_publish(item["channel"], item)
    if ok:
        _set_status(cid, "published", "published_at")
    else:
        # реальная отправка упала → НЕ помечаем published, оставляем approved для повтора
        _set_status(cid, "approved")
    report_mod.report(
        "content_factory", kind="content",
        title=f"{'Опубликовано' if ok else 'Ошибка публикации'} #{cid} ({item['channel']})",
        summary=info[:300], project_id=item.get("project_id"),
        meta={"content_id": cid, "channel": item["channel"], "ok": ok}, telegram=True,
    )
    print(f"[content_factory] #{cid} publish ok={ok}: {info}")
    return {"ok": ok, "info": info}


def _do_publish(channel, item):
    body = item.get("body") or ""
    if channel == "telegram":
        chan = os.getenv("TELEGRAM_CHANNEL_ID")
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if chan and token:
            try:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                data = json.dumps({"chat_id": chan, "text": body[:4000]}).encode()
                req = urllib.request.Request(url, data=data,
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
                return True, f"отправлено в Telegram-канал {chan}"
            except Exception as e:
                return False, f"ошибка публикации в TG: {e}"
        return True, "TG-канал не настроен (TELEGRAM_CHANNEL_ID) — сохранено как готовое к публикации"
    if channel == "vk":
        return True, "VK-токен не настроен — сохранено как готовое к публикации"
    return True, f"{channel}: сохранено как готовое к публикации (ручная отправка)"


def recent(limit=20):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, channel, kind, status, COALESCE(NULLIF(title,''), left(body,60)) title, "
            "to_char(created_at,'MM-DD HH24:MI') created FROM content_items "
            "ORDER BY id DESC LIMIT %s", (limit,))
        return [dict(zip([d[0] for d in (cur.description or [])], r)) for r in cur.fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    ops_store.init()
    a = sys.argv[1:]
    if a and a[0] == "approve":
        print(approve(int(a[1])))
    elif a and a[0] == "reject":
        print(reject(int(a[1]), " ".join(a[2:])))
    elif a and a[0] == "publish":
        print(publish(int(a[1])))
    else:
        brief = a[0] if a else "Продающий пост про водозащиту GPS-ошейника Amori для Telegram"
        ch = a[1] if len(a) > 1 else "telegram"
        kd = a[2] if len(a) > 2 else "post"
        print(create(brief, channel=ch, kind=kd))
