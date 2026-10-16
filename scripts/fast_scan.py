#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速扫库：内存批量检测 + 标记 + 分析报告（2026-10-16）。

设计：SELECT 全量载入内存 -> 线程池高并发检测 -> executemany 批量写回 -> 分析报告。
与 Celery 流水线复用同一 _where_sql 过滤口径，结果口径一致。

用法（N200）：
  # 增量快扫（同 cron 口径：未测/待验 + 过期复检）
  docker exec -e DEADLINK_LOCAL_CHECK_LIMIT=30 resource_web python3 /app/scripts/fast_scan.py
  # 全量快扫（所有可检测链接，不看 last_checked）
  docker exec -e DEADLINK_LOCAL_CHECK_LIMIT=30 resource_web python3 /app/scripts/fast_scan.py --full
  # 只扫某网盘 / 冒烟
  docker exec resource_web python3 /app/scripts/fast_scan.py --source quark --limit 50

注意：高并发打网盘 API 有被限流风险；脚本复用 link_checker 的 per-source
限流 + 失败指数退避（DEADLINK_LOCAL_CHECK_LIMIT 控制每网盘并发，快扫时调高）。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

sys.path.insert(0, "/app")

from services.deadlink_queue import set_fields, set_running  # noqa: E402
from services.deadlink_tasks import _db, _where_sql  # noqa: E402
from services.link_checker import check_single_link  # noqa: E402

PAGE = 10000
FLUSH_EVERY = 2000
HEARTBEAT_EVERY = 300  # 续命 cron 的卡死判定（2h），快扫时定时刷新 start_time


@contextmanager
def _scan_lock():
    """占用流水线 running 状态 + 定时心跳，防止 cron 半路又起 Celery 扫重。

    cron 见 running=1 且 start_time 新鲜会跳过；finally 保证异常退出也释放。
    """
    set_running(True)
    set_fields(start_time=time.time(), engine="fast_scan")
    stop = threading.Event()

    def _hb():
        while not stop.wait(HEARTBEAT_EVERY):
            try:
                set_fields(start_time=time.time())
            except Exception:
                pass

    t = threading.Thread(target=_hb, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        set_running(False)


def load_jobs(source_filter: str, rescan_alive: bool, limit: int) -> list:
    where, params = _where_sql(source_filter, rescan_alive, stale_days=3)
    db = _db()
    cur = db.cursor()
    jobs, last_id = [], 2 ** 31 - 1
    while True:
        cur.execute(
            f"SELECT id, url, source FROM resources WHERE {where} AND id < %s "
            f"ORDER BY id DESC LIMIT %s",
            params + [last_id, PAGE],
        )
        rows = cur.fetchall()
        if not rows:
            break
        for r in rows:
            jobs.append((int(r["id"]), r.get("url") or "", r.get("source") or ""))
            if limit and len(jobs) >= limit:
                return jobs
        last_id = int(rows[-1]["id"])
        if len(rows) < PAGE:
            break
    return jobs


def check_one(job):
    rid, url, source = job
    try:
        status, _c, _m = check_single_link(url, source)
        if status == "invalid":
            status = "unknown"
    except Exception:
        status = "error"
    return rid, status, source


def main() -> int:
    ap = argparse.ArgumentParser(description="快速扫库")
    ap.add_argument("--full", action="store_true", help="全量（不过滤 last_checked）")
    ap.add_argument("--source", default="", help="只扫指定 source")
    ap.add_argument("--limit", type=int, default=0, help="限制条数（冒烟测试）")
    ap.add_argument("--workers", type=int, default=200, help="并发线程数")
    args = ap.parse_args()

    t0 = time.time()
    jobs = load_jobs(args.source, rescan_alive=args.full, limit=args.limit)
    print(f"[fast_scan] 载入 {len(jobs)} 条待检测（{time.time() - t0:.1f}s），开始检测…", flush=True)
    if not jobs:
        print("[fast_scan] 无待检测任务")
        return 0

    db = _db()
    cur = db.cursor()
    counter: Counter = Counter()
    by_source: Counter = Counter()
    buf, done = [], 0
    with _scan_lock(), ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(check_one, j): j for j in jobs}
        for f in as_completed(futs):
            rid, status, source = f.result()
            counter[status] += 1
            by_source[(source, status)] += 1
            buf.append((status, rid))
            done += 1
            if len(buf) >= FLUSH_EVERY:
                cur.executemany(
                    "UPDATE resources SET link_status=%s, last_checked=NOW() WHERE id=%s",
                    buf,
                )
                buf.clear()
            if done % 10000 == 0:
                print(f"[fast_scan] …{done}/{len(jobs)}", flush=True)
        if buf:
            cur.executemany(
                "UPDATE resources SET link_status=%s, last_checked=NOW() WHERE id=%s",
                buf,
            )
        try:
            db.commit()
        except Exception:
            pass
    el = time.time() - t0
    print(f"[fast_scan] 完成：{done} 条，{el:.0f}s，{done / max(el, 1):.0f} 条/秒")
    print("状态分布:", dict(counter))
    print("分网盘（source × 状态）:")
    for (s, st), c in by_source.most_common(15):
        print(f"  {s or '-':12s} {st:8s} {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
