"""轻量拼音首字母工具 — 用于模糊搜索（如 lldq → 流浪地球）"""
import re

try:
    from pypinyin import lazy_pinyin, Style
    _HAS_PYPINYIN = True
except ImportError:
    _HAS_PYPINYIN = False


def to_initials(text: str) -> str:
    """将中文文本转为拼音首字母小写串，如 '流浪地球' → 'lldq'"""
    if not text:
        return ""
    if _HAS_PYPINYIN:
        letters = lazy_pinyin(text, style=Style.FIRST_LETTER)
        return "".join(letters).lower()
    # 无 pypinyin 时仅保留 ASCII
    return re.sub(r"[^a-zA-Z0-9]", "", text).lower()


def is_pinyin_query(q: str) -> bool:
    """判断查询是否为纯字母拼音/首字母输入"""
    return bool(q) and q.isascii() and q.isalpha() and len(q) >= 2


def pinyin_match(query: str, text: str) -> bool:
    """检查 text 的拼音首字母是否包含 query"""
    if not query or not text:
        return False
    initials = to_initials(text)
    q = query.lower()
    return q in initials or initials.startswith(q)
