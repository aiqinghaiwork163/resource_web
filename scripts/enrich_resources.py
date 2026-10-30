#!/usr/bin/env python3
# enrich_resources.py - 存量 19万 资源深度清洗、画质补全与拼音全索引
import os
import re
import sys
import time
import pymysql
import pymysql.cursors
from pypinyin import lazy_pinyin

# 加载 .env
env_vars = {}
env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
with open(env_file, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env_vars[k.strip()] = v.strip()

host = env_vars.get('DB_HOST', '192.168.0.201')
port = int(env_vars.get('DB_PORT', '3306'))
user = env_vars.get('DB_USER', 'res_app')
password = env_vars.get('DB_PASSWORD', '')
db_name = env_vars.get('DB_NAME', 'pan_resource')

print(f'[{time.strftime("%X")}] 连接 MySQL 数据库: {host}:{port}/{db_name} ...')
conn = pymysql.connect(
    host=host, port=port, user=user, password=password, database=db_name,
    charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor, autocommit=False
)

def clean_text(raw):
    if not raw: return ''
    t = re.sub(r'[\U00010000-\U0010ffff]|[\u2600-\u27bf]|❤️|✨|🔥|👍', '', raw)
    t = re.sub(r'(?:频道|来源)\s*[:：]\s*[^\s]+', '', t, flags=re.I)
    t = re.sub(r'【(?:纯净版|纯净分享|珍藏版|无水印|6G/集|首发|热播|更新至[^】]*)】', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def extract_quality(raw):
    res_m = re.search(r'\b(2160p|4k|1080p|720p)\b', raw, re.I)
    if res_m:
        val = res_m.group(1).lower()
        return '4K' if val in ['2160p', '4k'] else val.upper()
    return ''

def get_pinyin(title):
    zh_chars = re.sub(r'[^\u4e00-\u9fa5]', '', title)
    if not zh_chars: return '', ''
    p_list = lazy_pinyin(zh_chars[:12])
    full = ''.join(p_list)
    abbr = ''.join([item[0] for item in p_list if item])
    return full, abbr

try:
    with conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) as total FROM resources;')
        total = cur.fetchone()['total']
        print(f'[{time.strftime("%X")}] 数据库总资源量: {total} 条，开始清洗与补全...')

        batch_size = 2000
        last_id = 0
        processed = 0
        updated_count = 0
        t0 = time.time()

        while True:
            cur.execute(
                'SELECT id, title, keyword, quality, note FROM resources WHERE id > %s ORDER BY id ASC LIMIT %s;',
                (last_id, batch_size)
            )
            rows = cur.fetchall()
            if not rows:
                break

            updates = []
            for r in rows:
                rid = r['id']
                last_id = rid
                old_title = r['title'] or ''
                old_quality = r['quality'] or ''
                old_note = r['note'] or ''

                new_title = clean_text(old_title)
                new_quality = old_quality
                if not new_quality:
                    new_quality = extract_quality(old_title + ' ' + (r['keyword'] or '') + ' ' + old_note)

                # 拼音索引挂载到 note 尾部（利用已有的 FULLTEXT ft_title_note 索引实现拼音缩写秒搜）
                p_full, p_abbr = get_pinyin(new_title or r['keyword'] or '')
                new_note = old_note
                if p_abbr and ('#pinyin:' not in old_note):
                    p_tag = f'\n#pinyin: {p_abbr} {p_full}'
                    new_note = (old_note + p_tag).strip()

                if new_title != old_title or new_quality != old_quality or new_note != old_note:
                    updates.append((new_title, new_quality, new_note, rid))

            if updates:
                cur.executemany(
                    'UPDATE resources SET title = %s, quality = %s, note = %s WHERE id = %s;',
                    updates
                )
                conn.commit()
                updated_count += len(updates)

            processed += len(rows)
            elapsed = time.time() - t0
            speed = processed / elapsed if elapsed > 0 else 0
            eta = (total - processed) / speed if speed > 0 else 0
            print(f'  进度: {processed}/{total} ({processed*100/total:.1f}%) | 更新: {updated_count} | 速度: {speed:.0f} 条/秒 | 剩余约: {eta:.0f}s', end='\r')

        print(f'\n[{time.strftime("%X")}] 数据清洗完成！共处理: {processed} 条，优化更新: {updated_count} 条，耗时: {time.time()-t0:.1f} 秒。')
finally:
    conn.close()
