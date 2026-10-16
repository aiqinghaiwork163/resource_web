# _archive

- `scan_all_deadlinks.py`：2024-08 的旧独立死链扫描脚本，已被 Celery 流水线
 （`services/deadlink_tasks.py` + `deadlink-celery-*`）取代。2026-10-16 移入归档：
  它与 Celery 流水线无互斥，同时运行会双写 `link_status`/`last_checked`。
