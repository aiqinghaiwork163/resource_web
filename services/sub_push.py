# -*- coding: utf-8 -*-
"""订阅关键词 → 新资源通知（TG / 公告兜底）。"""
from __future__ import annotations

from typing import Any, Callable, Optional


def find_new_for_subscriptions(cur, since_hours: int | None = None) -> list[dict]:
    """返回 [{user_id, telegram_id, keyword, sub_id, resources:[...]}]。"""
    cur.execute(
        """
        SELECT s.id AS sub_id, s.user_id, s.keyword, s.created_at,
               u.telegram_id, u.username
        FROM subscriptions s
        JOIN users u ON u.id = s.user_id
        ORDER BY s.id DESC
        LIMIT 200
        """
    )
    subs = cur.fetchall() or []
    out = []
    for sub in subs:
        kw = (sub.get("keyword") or "").strip()
        if not kw:
            continue
        if since_hours:
            cur.execute(
                """
                SELECT id, title, source, url, quality, created_at
                FROM resources
                WHERE (title LIKE %s OR keyword LIKE %s)
                  AND created_at > (NOW() - INTERVAL %s HOUR)
                  AND (link_status IS NULL OR link_status != 'dead')
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (f"%{kw}%", f"%{kw}%", int(since_hours)),
            )
        else:
            cur.execute(
                """
                SELECT id, title, source, url, quality, created_at
                FROM resources
                WHERE (title LIKE %s OR keyword LIKE %s)
                  AND created_at > %s
                  AND (link_status IS NULL OR link_status != 'dead')
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (f"%{kw}%", f"%{kw}%", sub.get("created_at")),
            )
        rows = cur.fetchall() or []
        if not rows:
            continue
        out.append(
            {
                "sub_id": sub.get("sub_id"),
                "user_id": sub.get("user_id"),
                "username": sub.get("username"),
                "telegram_id": sub.get("telegram_id"),
                "keyword": kw,
                "resources": rows,
            }
        )
    return out


def format_push_text(keyword: str, resources: list[dict]) -> str:
    lines = [f"🔔 订阅「{keyword}」有 {len(resources)} 条新资源："]
    for r in resources[:8]:
        title = (r.get("title") or "")[:60]
        src = r.get("source") or ""
        lines.append(f"· [{src}] {title}")
    if len(resources) > 8:
        lines.append(f"…还有 {len(resources) - 8} 条")
    return "\n".join(lines)


def push_hits(
    hits: list[dict],
    *,
    send_telegram: Optional[Callable[[str, str], Any]] = None,
    create_announcement: Optional[Callable[[str, str], Any]] = None,
) -> dict[str, Any]:
    """对命中结果推送。send_telegram(chat_id, text)；无 TG 则公告。"""
    tg_ok = tg_fail = ann = 0
    details = []
    for hit in hits:
        text = format_push_text(hit["keyword"], hit["resources"])
        tg_id = str(hit.get("telegram_id") or "").strip()
        pushed = False
        if tg_id and send_telegram:
            try:
                send_telegram(tg_id, text)
                tg_ok += 1
                pushed = True
                details.append({"user_id": hit["user_id"], "via": "telegram", "keyword": hit["keyword"]})
            except Exception as e:
                tg_fail += 1
                details.append({"user_id": hit["user_id"], "via": "telegram_fail", "error": str(e)})
        if not pushed and create_announcement:
            try:
                create_announcement(
                    f"订阅更新：{hit['keyword']}",
                    text,
                )
                ann += 1
                details.append({"user_id": hit["user_id"], "via": "announcement", "keyword": hit["keyword"]})
            except Exception as e:
                details.append({"user_id": hit["user_id"], "via": "announcement_fail", "error": str(e)})
    return {
        "ok": True,
        "hits": len(hits),
        "telegram_ok": tg_ok,
        "telegram_fail": tg_fail,
        "announcements": ann,
        "details": details,
    }
