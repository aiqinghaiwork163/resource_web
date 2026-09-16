#!/usr/bin/env python3
"""
自动死链巡检触发脚本（用于定时任务 Cron）
- 默认：仅增量检测新入库未测（NULL）及待验（unknown/timeout/error）资源
- 支持防重叠：若上一轮尚未结束，自动跳过
"""
import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("REDIS_HOST", "resource_web_redis")

try:
    from services.deadlink_queue import get_state
    from services.deadlink_tasks import start_job

    st = get_state()
    if st.get("running"):
        print("[auto_deadlink_cron] 上一轮检测流水线正在运行中，跳过本次触发。")
        sys.exit(0)

    # 触发增量检测：rescan_alive=False，仅捞取未测与待验
    res = start_job(source_filter="", rescan_alive=False)
    print(f"[auto_deadlink_cron] 自动化增量检测已触发: {res.get('msg')}")
except Exception as e:
    print(f"[auto_deadlink_cron] 触发失败: {e}", file=sys.stderr)
    sys.exit(1)
