# -*- coding: utf-8 -*-
"""
admin_features.py - 站长核心实质性管理功能增强模块
1. 搜空词与补片需求分析 (Zero-Result Demands & Missing Analytics)
2. 智能网盘链接批量解析与快捷发片 (Smart Batch & Fast Ingestion)
3. 全站网盘生态与画质透视大屏 (Cloud Ecosystem & Quality Distribution)
4. 站长运维与数据库工具箱 (DB Optimization, Dirty Clean & Flush Cache)
"""

import re
import time
import json
import pymysql
from flask import jsonify, request, session
from pypinyin import lazy_pinyin

from .admin import admin_bp
from .common import app, get_db, get_redis, admin_required, csrf, _log_op, _invalidate_resource_cache

# ============================================================
# 辅助解析函数
# ============================================================
def safe_log_op(action: str, detail: str):
    try:
        uid = session.get("user_id") or 1
        username = session.get("username") or "admin"
        _log_op(uid, username, action, detail)
    except Exception:
        pass

def detect_drive_source(url: str) -> str:
    u = (url or '').lower()
    if 'pan.quark.cn' in u or 'pan.quark' in u:
        return 'quark'
    elif 'alipan.com' in u or 'aliyundrive.com' in u:
        return 'aliyun'
    elif 'pan.baidu.com' in u:
        return 'baidu'
    elif 'pan.xunlei.com' in u:
        return 'xunlei'
    elif '115.com' in u or 'anxia.com' in u:
        return '115'
    elif 'drive.uc.cn' in u:
        return 'uc'
    elif 'cloud.189.cn' in u:
        return 'tianyi'
    elif 'pikpak' in u:
        return 'pikpak'
    elif '123pan.com' in u or '123684.com' in u:
        return '123'
    elif u.startswith('magnet:?'):
        return 'magnet'
    return 'other'

def clean_title(raw: str) -> str:
    if not raw:
        return ''
    t = re.sub(r'[\U00010000-\U0010ffff]|[\u2600-\u27bf]|❤️|✨|🔥|👍|📢|🍿', '', raw)
    t = re.sub(r'(?:频道|来源|关注|首发|转存)\s*[:：]\s*[^\s]*', '', t, flags=re.I)
    t = re.sub(r'【(?:纯净版|纯净分享|珍藏版|无水印|6G/集|首发|热播|更新至[^】]*)】', '', t)
    t = re.sub(r'https?://[^\s]+', '', t)
    t = re.sub(r'^(?:夸克|阿里|百度|迅雷|115|UC)?(?:网盘|链接|资源|分享)*[:：\s]*', '', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t).strip(' -_：:,\t\r\n')
    if t in ['链接', '网盘', '分享', '资源', '下载', '提取码', '密码']:
        return ''
    return t

def extract_quality(raw: str) -> str:
    res_m = re.search(r'\b(2160p|4k|1080p|720p)\b', raw or '', re.I)
    if res_m:
        val = res_m.group(1).lower()
        return '4K' if val in ['2160p', '4k'] else val.upper()
    return ''

def get_pinyin_tags(title: str) -> str:
    zh = re.sub(r'[^\u4e00-\u9fa5]', '', title or '')
    if not zh:
        return ''
    p_list = lazy_pinyin(zh[:12])
    full = ''.join(p_list)
    abbr = ''.join([item[0] for item in p_list if item])
    if abbr:
        return f"\n#pinyin: {abbr} {full}"
    return ''

def parse_pasted_text(text: str, check_db: bool = True) -> tuple:
    items = []
    url_matches = list(re.finditer(r'(https?://[^\s<>"\'\)]+|magnet:\?[^\s<>"\'\)]+)', text or ''))
    if not url_matches:
        return [], 0

    urls_to_check = []
    for i, m in enumerate(url_matches):
        url = m.group(1).strip('.,;!?:')
        urls_to_check.append(url)

    # 查库测重复
    existing_urls = set()
    if check_db and urls_to_check:
        try:
            db = get_db()
            cur = db.cursor()
            format_strings = ','.join(['%s'] * len(urls_to_check))
            cur.execute(f"SELECT url FROM resources WHERE url IN ({format_strings});", tuple(urls_to_check))
            for row in cur.fetchall():
                existing_urls.add(row['url'])
            db.close()
        except Exception:
            pass

    dup_count = 0
    for i, m in enumerate(url_matches):
        url = m.group(1).strip('.,;!?:')
        start_idx = url_matches[i-1].end() if i > 0 else 0
        end_idx = url_matches[i+1].start() if i + 1 < len(url_matches) else len(text)
        block = text[start_idx:end_idx]

        # 提取密码/提取码
        pwd = ''
        pwd_m = re.search(r'(?:提取码|密码|pwd|code|访问码|key)[：:\s]*([a-zA-Z0-9]{4,8})', block, re.I)
        if pwd_m:
            pwd = pwd_m.group(1)
        else:
            url_pwd = re.search(r'[?&]pwd=([a-zA-Z0-9]+)', url)
            if url_pwd:
                pwd = url_pwd.group(1)

        quality = extract_quality(block) or '1080P'

        # 提取标题
        pre_url = text[start_idx:m.start()].strip()
        raw_lines = [clean_title(line) for line in pre_url.split('\n')]
        clean_lines = [l for l in raw_lines if l and len(l) >= 2]
        title = ''
        if clean_lines:
            bracket_lines = [l for l in clean_lines if '《' in l]
            title = bracket_lines[-1] if bracket_lines else clean_lines[-1]
        if not title:
            title = '新分享资源'
        title = clean_title(title)
        source = detect_drive_source(url)

        is_exist = url in existing_urls
        if is_exist:
            dup_count += 1

        items.append({
            "title": title[:100],
            "url": url,
            "password": pwd,
            "code": pwd,
            "quality": quality,
            "source": source,
            "type": "剧集" if re.search(r'(第[一二三四五六七八九十0-9]+季|全集|集数|更新至|EP\d+)', block, re.I) else "电影",
            "exists": is_exist,
            "note": f"网盘资源: {title} {quality}".strip()
        })
    return items, dup_count


# ============================================================
# 1. 智能链接解析预览接口
# ============================================================
@admin_bp.route("/api/admin/resources/parse_text", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_resources_parse_text():
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请提供待解析的文本内容"}), 400
    try:
        items, dups = parse_pasted_text(text, check_db=True)
        return jsonify({
            "ok": True,
            "items": items,
            "count": len(items),
            "duplicates_in_db": dups
        })
    except Exception as e:
        app.logger.exception("api_admin_resources_parse_text failed")
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================
# 2. 确认一键入库接口
# ============================================================
@admin_bp.route("/api/admin/resources/quick_ingest", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_resources_quick_ingest():
    data = request.get_json() or {}
    items = data.get("items") or data.get("resources") or []
    if not items and data.get("text"):
        items, _ = parse_pasted_text(data.get("text"), check_db=False)

    if not items:
        return jsonify({"ok": False, "error": "没有可入库的资源条目"}), 400

    db = get_db()
    success_count = 0
    skip_count = 0
    try:
        cur = db.cursor()
        for it in items:
            url = (it.get("url") or "").strip()
            title = clean_title(it.get("title") or "")
            if not url or not title:
                continue
            cur.execute("SELECT id FROM resources WHERE url = %s LIMIT 1;", (url,))
            if cur.fetchone():
                skip_count += 1
                continue

            pwd = (it.get("password") or it.get("code") or "").strip()
            quality = (it.get("quality") or "").strip() or extract_quality(title) or "1080P"
            source = (it.get("source") or "").strip() or detect_drive_source(url)
            rtype = (it.get("type") or "电影").strip()
            note = (it.get("note") or f"网盘资源: {title} {quality}").strip()
            p_tag = get_pinyin_tags(title)
            if p_tag and '#pinyin:' not in note:
                note = (note + p_tag).strip()

            cur.execute("""
                INSERT INTO resources 
                (keyword, source, url, title, note, password, quality, type, is_valid, datetime, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NOW(), NOW());
            """, (title[:50], source, url, title, note, pwd, quality, rtype))
            success_count += 1

        db.commit()
        if success_count > 0:
            _invalidate_resource_cache()
            safe_log_op("quick_ingest", f"快捷录入完成: 成功 {success_count} 条，重复跳过 {skip_count} 条")
        return jsonify({
            "ok": True,
            "success_count": success_count,
            "skip_count": skip_count,
            "inserted": success_count,
            "skipped_duplicates": skip_count
        })
    except Exception as e:
        app.logger.exception("api_admin_resources_quick_ingest failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# 3. 搜空词与补片需求分析接口 (Zero-Result Demands)
# ============================================================
@admin_bp.route("/api/admin/stats/zero_searches")
@admin_required
def api_admin_stats_zero_searches():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("""
            SELECT keyword, COUNT(*) as cnt, MAX(created_at) as last_time
            FROM search_logs
            WHERE keyword IS NOT NULL AND TRIM(keyword) != ''
            GROUP BY keyword
            ORDER BY cnt DESC
            LIMIT 150;
        """)
        rows = cur.fetchall()
        zero_demands = []
        hot_satisfied = []
        total_zero_pv = 0

        for r in rows:
            kw = r['keyword'].strip()
            if not kw or kw.startswith('<') or len(kw) > 80:
                continue
            safe_kw = re.sub(r"[^\w\u4e00-\u9fa5]", " ", kw).strip()
            tokens = [t.strip() for t in safe_kw.split() if t.strip()]
            if not tokens:
                continue
            b_expr = " ".join([f"+{t}*" for t in tokens])
            try:
                cur.execute("SELECT count(*) as total FROM resources WHERE MATCH(title, note) AGAINST(%s IN BOOLEAN MODE);", (b_expr,))
                total = cur.fetchone()['total']
            except Exception:
                continue

            last_time_str = str(r['last_time'])[:19] if r['last_time'] else ""
            item = {
                "keyword": kw,
                "count": r['cnt'],
                "last_time": last_time_str,
                "latest": last_time_str,
                "db_matches": total,
                "matched": total
            }
            if total == 0:
                zero_demands.append(item)
                total_zero_pv += r['cnt']
            else:
                hot_satisfied.append(item)

        total_analyzed = len(zero_demands) + len(hot_satisfied)
        sat_rate = round((len(hot_satisfied) / max(total_analyzed, 1)) * 100, 1)

        return jsonify({
            "ok": True,
            "zero_demands": zero_demands[:50],
            "hot_satisfied": hot_satisfied[:30],
            "zero_count": len(zero_demands),
            "summary": {
                "zero_count": len(zero_demands),
                "zero_searches": total_zero_pv,
                "satisfied_rate_pct": sat_rate,
                "top_missing": zero_demands[0]["keyword"] if zero_demands else "无"
            }
        })
    except Exception as e:
        app.logger.exception("api_admin_stats_zero_searches failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# 4. 全站网盘生态与画质透视大屏接口 (Ecosystem & Quality Analytics)
# ============================================================
@admin_bp.route("/api/admin/stats/ecosystem")
@admin_required
def api_admin_stats_ecosystem():
    db = get_db()
    try:
        cur = db.cursor()
        # 1. 渠道分布
        cur.execute("""
            SELECT 
                CASE 
                    WHEN source LIKE '%quark%' OR source LIKE '%夸克%' THEN '夸克网盘'
                    WHEN source LIKE '%ali%' OR source LIKE '%阿里%' THEN '阿里网盘'
                    WHEN source LIKE '%baidu%' OR source LIKE '%百度%' THEN '百度网盘'
                    WHEN source LIKE '%xunlei%' OR source LIKE '%迅雷%' THEN '迅雷网盘'
                    WHEN source LIKE '%115%' THEN '115网盘'
                    WHEN source LIKE '%uc%' OR source LIKE '%UC%' THEN 'UC网盘'
                    WHEN source LIKE '%123%' THEN '123网盘'
                    WHEN source LIKE '%tianyi%' OR source LIKE '%天翼%' THEN '天翼云盘'
                    WHEN source LIKE '%magnet%' OR source LIKE '%磁力%' THEN '磁力链接'
                    WHEN source LIKE '%thepiratebay%' THEN '海盗湾BT'
                    ELSE IFNULL(NULLIF(source, ''), '其他来源')
                END as drive_name,
                COUNT(*) as cnt
            FROM resources
            GROUP BY drive_name
            ORDER BY cnt DESC
            LIMIT 12;
        """)
        drives = cur.fetchall()

        # 2. 画质分布
        cur.execute("""
            SELECT 
                CASE 
                    WHEN quality IN ('4K', '2160P', '2160p') THEN '4K 超清'
                    WHEN quality IN ('1080P', '1080p') THEN '1080P 高清'
                    WHEN quality IN ('720P', '720p') THEN '720P 标清'
                    ELSE '其他/未标注'
                END as q_name,
                COUNT(*) as cnt
            FROM resources
            GROUP BY q_name
            ORDER BY cnt DESC;
        """)
        qualities = cur.fetchall()

        # 3. 总体健康统计
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN is_valid = 1 THEN 1 ELSE 0 END) as valid_cnt,
                SUM(CASE WHEN is_valid = 0 THEN 1 ELSE 0 END) as dead_cnt,
                SUM(CASE WHEN is_valid IS NULL OR is_valid = -1 THEN 1 ELSE 0 END) as unchecked_cnt
            FROM resources;
        """)
        health = cur.fetchone() or {"total": 0, "valid_cnt": 0, "dead_cnt": 0, "unchecked_cnt": 0}
        total_res = health.get("total") or 0

        # 转换并计算百分比供前端渲染
        sources_list = []
        drive_color_map = {
            '夸克网盘': '#58a6ff',
            '阿里网盘': '#f0883e',
            '百度网盘': '#38bdf8',
            '迅雷网盘': '#2ea043',
            '115网盘': '#db61a2',
            'UC网盘': '#f78166',
            '123网盘': '#a371f7',
            '天翼云盘': '#56d364',
            '磁力链接': '#79c0ff',
            '海盗湾BT': '#8b949e',
            '其他来源': '#8b949e'
        }
        for d in drives:
            name = d['drive_name']
            count = d['cnt']
            pct = round((count / max(total_res, 1)) * 100, 1)
            sources_list.append({
                "source": name,
                "label": name,
                "count": count,
                "pct": pct,
                "color": drive_color_map.get(name, '#58a6ff')
            })

        qualities_list = []
        for q in qualities:
            q_name = q['q_name']
            count = q['cnt']
            pct = round((count / max(total_res, 1)) * 100, 1)
            qualities_list.append({
                "quality": q_name,
                "count": count,
                "pct": pct
            })

        return jsonify({
            "ok": True,
            "total": total_res,
            "sources": sources_list,
            "qualities": qualities_list,
            "drives": drives,
            "health": health
        })
    except Exception as e:
        app.logger.exception("api_admin_stats_ecosystem failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()


# ============================================================
# 5. 站长运维与数据库工具箱 (Webmaster Operations)
# ============================================================
@admin_bp.route("/api/admin/tools/optimize_db", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_tools_optimize_db():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("OPTIMIZE TABLE resources, search_logs;")
        res = cur.fetchall()
        safe_log_op("optimize_db", "站长手动执行了 resources 表碎片优化与索引整理")
        return jsonify({"ok": True, "message": "数据库表优化与碎片整理完成！", "result": res})
    except Exception as e:
        app.logger.exception("api_admin_tools_optimize_db failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()

@admin_bp.route("/api/admin/tools/clean_dirty", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_tools_clean_dirty():
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("""
            DELETE FROM resources 
            WHERE (url IS NULL OR TRIM(url) = '') 
               OR ((title IS NULL OR TRIM(title) = '') AND (keyword IS NULL OR TRIM(keyword) = ''));
        """)
        deleted = cur.rowcount
        db.commit()
        if deleted > 0:
            _invalidate_resource_cache()
            safe_log_op("clean_dirty", f"站长清理了 {deleted} 条无效空数据")
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": deleted, "message": f"成功清理 {deleted} 条空垃圾记录！"})
    except Exception as e:
        app.logger.exception("api_admin_tools_clean_dirty failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()

@admin_bp.route("/api/admin/tools/flush_all_cache", methods=["POST"])
@csrf.exempt
@admin_required
def api_admin_tools_flush_all_cache():
    try:
        _invalidate_resource_cache()
        r = get_redis()
        flushed_count = 0
        if r:
            keys = r.keys("res_web:*")
            if keys:
                flushed_count = r.delete(*keys)
        safe_log_op("flush_cache", f"站长刷新了全站缓存，清理键数: {flushed_count}")
        return jsonify({"ok": True, "cleared_keys": flushed_count, "message": f"全站缓存已彻底刷新！共清理 {flushed_count} 个 Redis 缓存键。"})
    except Exception as e:
        app.logger.exception("api_admin_tools_flush_all_cache failed")
        return jsonify({"ok": False, "error": str(e)}), 500
