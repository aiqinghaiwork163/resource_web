# -*- coding: utf-8 -*-
"""失效链接自动清理。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def _invalid_where(days: int) -> tuple[str, list]:
    """失效且 last_checked 早于 N 天（无 last_checked 则用 updated_at）。"""
    days = max(1, int(days or 7))
    return (
        """
        (link_status='dead' OR is_valid=0)
        AND COALESCE(last_checked, updated_at, created_at) < (NOW() - INTERVAL %s DAY)
        """,
        [days],
    )


def preview_auto_clean(cur, days: int = 7, limit: int = 50) -> dict[str, Any]:
    where, params = _invalid_where(days)
    cur.execute(f"SELECT COUNT(*) c FROM resources WHERE {where}", params)
    total = int((cur.fetchone() or {}).get("c") or 0)
    cur.execute(
        f"""
        SELECT id, title, source, url, link_status, is_valid, last_checked, updated_at
        FROM resources
        WHERE {where}
        ORDER BY COALESCE(last_checked, updated_at) ASC
        LIMIT %s
        """,
        params + [min(int(limit or 50), 200)],
    )
    return {"total": total, "days": days, "samples": cur.fetchall() or []}


def _ensure_archive_table(cur) -> None:
    """确保归档表存在：结构同 resources，另加 archived_at（P0-3 2026-10-16）。

    auto_clean 原来是物理 DELETE，误删不可回；现改为先归档后删除。
    """
    cur.execute("CREATE TABLE IF NOT EXISTS resources_archive LIKE resources")
    cur.execute("SHOW COLUMNS FROM resources_archive LIKE 'archived_at'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE resources_archive "
            "ADD COLUMN archived_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
        )


def run_auto_clean(cur, days: int = 7, limit: int = 5000) -> dict[str, Any]:
    where, params = _invalid_where(days)
    limit = min(max(1, int(limit or 5000)), 20000)
    cur.execute(
        f"""
        SELECT id FROM resources
        WHERE {where}
        ORDER BY id ASC
        LIMIT %s
        """,
        params + [limit],
    )
    ids = [r["id"] for r in (cur.fetchall() or [])]
    if not ids:
        return {"ok": True, "deleted": 0, "archived": 0, "days": days}
    _ensure_archive_table(cur)
    # 动态取列名，保证归档 INSERT 与主表列对齐
    cur.execute("SHOW COLUMNS FROM resources")
    cols = [r["Field"] for r in (cur.fetchall() or [])]
    collist = ",".join(f"`{c}`" for c in cols)
    deleted = 0
    for i in range(0, len(ids), 200):
        batch = ids[i:i + 200]
        placeholders = ",".join(["%s"] * len(batch))
        # 先归档后删除：INSERT IGNORE 防重跑主键冲突
        cur.execute(
            f"INSERT IGNORE INTO resources_archive ({collist}) "
            f"SELECT {collist} FROM resources WHERE id IN ({placeholders})",
            batch,
        )
        cur.execute(
            f"UPDATE resources_archive SET archived_at=NOW() WHERE id IN ({placeholders})",
            batch,
        )
        cur.execute(f"DELETE FROM resources WHERE id IN ({placeholders})", batch)
        deleted += cur.rowcount
        # 同步清理 favorites/share_links 中的悬空引用
        for ref_table in ("favorites", "share_links"):
            try:
                cur.execute(f"DELETE FROM {ref_table} WHERE resource_id IN ({placeholders})", batch)
            except Exception:
                pass  # 表不存在等场景忽略,主删除不受影响
    return {"ok": True, "deleted": deleted, "archived": deleted, "days": days, "ids_sample": ids[:20]}


def scan_invalid_stats(cur) -> dict[str, Any]:
    cur.execute(
        "SELECT COUNT(*) c FROM resources WHERE link_status='dead' OR is_valid=0"
    )
    invalid = int((cur.fetchone() or {}).get("c") or 0)
    cur.execute(
        "SELECT COUNT(*) c FROM resources WHERE link_status='dead' OR is_valid=0"
    )
    # 分档：按 last_checked 年龄
    cur.execute(
        """
        SELECT
          SUM(CASE WHEN COALESCE(last_checked, updated_at, created_at) < NOW() - INTERVAL 7 DAY THEN 1 ELSE 0 END) AS d7,
          SUM(CASE WHEN COALESCE(last_checked, updated_at, created_at) < NOW() - INTERVAL 30 DAY THEN 1 ELSE 0 END) AS d30
        FROM resources
        WHERE link_status='dead' OR is_valid=0
        """
    )
    row = cur.fetchone() or {}
    return {
        "invalid": invalid,
        "older_than_7d": int(row.get("d7") or 0),
        "older_than_30d": int(row.get("d30") or 0),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
