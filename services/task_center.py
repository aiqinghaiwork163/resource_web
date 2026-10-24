# -*- coding: utf-8 -*-
"""
VaultDrive 任务中心 (Task Center)
统一管理与追踪后台异步任务队列状态：
1. 死链全量/来源巡检 (Celery 队列)
2. 批量资源测活任务 (异步线程/队列)
3. 违禁词全库回刷打标任务 (异步内存极速打标)
"""
import json
import os
import threading
import time
from typing import Any, Dict, List

TASK_BATCH_CHECK_KEY = "task:batch_check"
TASK_FILTER_WORDS_KEY = "task:filter_words"


def get_task_center_state(redis_client) -> Dict[str, Any]:
    """汇总所有后台任务状态"""
    state = {
        "workers_online": 0,
        "active_count": 0,
        "tasks": {
            "deadlink": {},
            "batch_check": {},
            "filter_words": {},
        }
    }

    # 1. Celery Workers 在线数
    try:
        from services.celery_app import app as celery_app
        insp = celery_app.control.inspect(timeout=0.6)
        pings = insp.ping() if insp else None
        if pings:
            state["workers_online"] = len(pings)
    except Exception:
        state["workers_online"] = 0

    # 2. 死链巡检状态 (Celery 队列)
    try:
        from services.deadlink_queue import get_state
        dl_st = get_state(redis_client)
        is_running = bool(dl_st.get("running"))
        scanned = int(dl_st.get("scanned") or 0)
        total = int(dl_st.get("total") or 0)
        pct = round((scanned / total * 100), 1) if total > 0 else (100.0 if not is_running and scanned > 0 else 0.0)
        
        state["tasks"]["deadlink"] = {
            "name": "死链后台巡检",
            "type": "deadlink",
            "running": is_running,
            "engine": dl_st.get("engine", "celery"),
            "total": total,
            "scanned": scanned,
            "alive": int(dl_st.get("alive") or 0),
            "dead": int(dl_st.get("dead") or 0),
            "percent": pct,
            "elapsed_s": int(dl_st.get("elapsed_s") or 0),
            "last_error": dl_st.get("last_error") or "",
        }
        if is_running:
            state["active_count"] += 1
    except Exception as e:
        state["tasks"]["deadlink"] = {"running": False, "error": str(e)}

    # 3. 批量测活任务状态
    try:
        raw_bc = redis_client.get(TASK_BATCH_CHECK_KEY)
        if raw_bc:
            bc_data = json.loads(raw_bc)
            state["tasks"]["batch_check"] = bc_data
            if bc_data.get("running"):
                state["active_count"] += 1
        else:
            state["tasks"]["batch_check"] = {"running": False, "total": 0, "done": 0}
    except Exception:
        state["tasks"]["batch_check"] = {"running": False}

    # 4. 违禁词打标任务状态
    try:
        raw_fw = redis_client.get(TASK_FILTER_WORDS_KEY)
        if raw_fw:
            fw_data = json.loads(raw_fw)
            state["tasks"]["filter_words"] = fw_data
            if fw_data.get("running"):
                state["active_count"] += 1
        else:
            state["tasks"]["filter_words"] = {"running": False, "total": 0, "done": 0}
    except Exception:
        state["tasks"]["filter_words"] = {"running": False}

    # 5. 统一网盘测活与死链检测视图 (将全库巡检与勾选批量测活融合成同一心智)
    dl_info = state["tasks"].get("deadlink") or {}
    bc_info = state["tasks"].get("batch_check") or {}
    is_bc_running = bool(bc_info.get("running"))
    is_dl_running = bool(dl_info.get("running"))
    is_any_running = is_bc_running or is_dl_running

    if is_bc_running:
        active_type = "batch_check"
        main_task = bc_info
        title = "手动勾选 · 批量测活"
        mode_desc = "前台勾选异步队列"
    elif is_dl_running:
        active_type = "deadlink"
        main_task = dl_info
        title = "全库/来源 · 死链巡检"
        mode_desc = "Celery 3级流水线"
    else:
        # 两者均空闲：若手动批量有最近完成的数据且全库为0，则展示手动批量的结果，否则展示全库结果
        if bc_info.get("total", 0) > 0 and dl_info.get("total", 0) == 0:
            active_type = "batch_check"
            main_task = bc_info
            title = "手动勾选 · 批量测活"
            mode_desc = "前台勾选异步队列"
        else:
            active_type = "deadlink"
            main_task = dl_info
            title = "全库/来源 · 死链巡检"
            mode_desc = "Celery 3级流水线"

    total_cnt = main_task.get("total", 0)
    done_cnt = main_task.get("done", main_task.get("scanned", 0))
    pct = main_task.get("percent", 0.0)

    state["unified_link_check"] = {
        "running": is_any_running,
        "active_type": active_type,
        "title": title,
        "mode_desc": mode_desc,
        "total": total_cnt,
        "done": done_cnt,
        "alive": main_task.get("alive", 0),
        "dead": main_task.get("dead", 0),
        "unknown": main_task.get("unknown", 0),
        "percent": pct,
        "elapsed_s": main_task.get("elapsed_s", 0),
    }

    return state


def start_batch_check_async(ids: List[int], redis_client, get_db_fn) -> Dict[str, Any]:
    """提交批量资源测活任务至后台队列"""
    if not ids:
        return {"ok": False, "msg": "未指定资源ID"}

    # 检查是否已在运行
    raw = redis_client.get(TASK_BATCH_CHECK_KEY)
    if raw:
        try:
            curr = json.loads(raw)
            if curr.get("running"):
                return {"ok": False, "msg": "已有批量测活任务在运行中，请稍候"}
        except Exception:
            pass

    task_info = {
        "name": "批量资源测活",
        "type": "batch_check",
        "running": True,
        "total": len(ids),
        "done": 0,
        "alive": 0,
        "dead": 0,
        "unknown": 0,
        "percent": 0.0,
        "start_time": time.time(),
        "elapsed_s": 0,
        "stop_requested": False,
    }
    redis_client.set(TASK_BATCH_CHECK_KEY, json.dumps(task_info))

    def _worker():
        from services.link_checker import check_single_link
        total = len(ids)
        done = 0
        alive = 0
        dead = 0
        unknown = 0
        t0 = time.time()

        try:
            db = get_db_fn()
            cur = db.cursor()
            # 获取这批资源的详情
            fmt = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT id, url, source FROM resources WHERE id IN ({fmt})", ids)
            rows = cur.fetchall()
            db.close()

            row_map = {r["id"]: r for r in rows}

            for rid in ids:
                # 检查是否请求停止
                raw_st = redis_client.get(TASK_BATCH_CHECK_KEY)
                if raw_st:
                    try:
                        st_obj = json.loads(raw_st)
                        if st_obj.get("stop_requested"):
                            st_obj["running"] = False
                            st_obj["last_msg"] = "用户手动停止任务"
                            redis_client.set(TASK_BATCH_CHECK_KEY, json.dumps(st_obj))
                            return
                    except Exception:
                        pass

                r = row_map.get(rid)
                if not r or not r.get("url"):
                    done += 1
                    continue

                status, code, msg = check_single_link(r["url"], r.get("source") or "")
                if status == "alive":
                    alive += 1
                elif status == "dead":
                    dead += 1
                else:
                    unknown += 1

                done += 1

                # 写回数据库
                try:
                    wdb = get_db_fn()
                    wcur = wdb.cursor()
                    wcur.execute(
                        "UPDATE resources SET last_checked=NOW(), link_status=%s WHERE id=%s",
                        (status, rid)
                    )
                    wdb.commit()
                    wdb.close()
                except Exception:
                    pass

                # 更新 Redis 进度 (每处理1条或多条更新)
                pct = round(done / total * 100, 1)
                task_info["done"] = done
                task_info["alive"] = alive
                task_info["dead"] = dead
                task_info["unknown"] = unknown
                task_info["percent"] = pct
                task_info["elapsed_s"] = int(time.time() - t0)
                redis_client.set(TASK_BATCH_CHECK_KEY, json.dumps(task_info))

            # 完成
            task_info["running"] = False
            task_info["percent"] = 100.0
            task_info["elapsed_s"] = int(time.time() - t0)
            redis_client.set(TASK_BATCH_CHECK_KEY, json.dumps(task_info))

            # 清理相关统计缓存
            redis_client.delete("admin:cache:unchecked_count")
        except Exception as e:
            task_info["running"] = False
            task_info["error"] = str(e)
            redis_client.set(TASK_BATCH_CHECK_KEY, json.dumps(task_info))

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "msg": f"已将 {len(ids)} 条测活任务加入后台队列", "total": len(ids)}


def stop_task(task_type: str, redis_client) -> Dict[str, Any]:
    """停止指定的后台任务"""
    if task_type in ("deadlink", "batch_check", "link_check"):
        stopped = []
        # 1. 停批量测活
        raw = redis_client.get(TASK_BATCH_CHECK_KEY)
        if raw:
            try:
                data = json.loads(raw)
                if data.get("running"):
                    data["stop_requested"] = True
                    data["running"] = False
                    redis_client.set(TASK_BATCH_CHECK_KEY, json.dumps(data))
                    stopped.append("批量测活")
            except Exception:
                pass

        # 2. 停死链巡检
        try:
            from services.deadlink_tasks import stop_job
            stop_job()
            stopped.append("死链巡检流水线")
        except Exception:
            try:
                from services.deadlink_queue import request_stop, set_running
                request_stop(redis_client)
                set_running(False, redis_client)
                stopped.append("死链巡检")
            except Exception:
                pass

        msg = f"已停止：{'、'.join(stopped)}" if stopped else "已发送停止指令"
        return {"ok": True, "msg": msg}

    return {"ok": False, "msg": f"未知任务类型: {task_type}"}
