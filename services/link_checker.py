"""Cloud drive link validity checks."""
import re

import requests as http_requests
def extract_id(url, source):
    """从URL提取分享ID"""
    if "quark" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "baidu" in source or "bdpan" in source:
        m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        return m.group(1) if m else None
    elif "aliyun" in source or "alipan" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "xunlei" in source:
        m = re.search(r'/s/([a-zA-Z0-9_-]+)', url)
        return m.group(1) if m else None
    elif "115" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    elif "pikpak" in source:
        m = re.search(r'/s/([a-zA-Z0-9]+)', url)
        return m.group(1) if m else None
    return None

def check_single_link(url, source):
    """根据网盘类型使用API精准检测"""
    share_id = extract_id(url, source)
    if not share_id:
        return "unknown", 0, "无法提取分享ID"

    try:
        # 夸克网盘
        if "quark" in source:
            r = http_requests.post(
                "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token",
                json={"pwd_id": share_id, "passcode": ""},
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 404:
                msg = r.json().get("message", "")
                if "失效" in msg or "不存在" in msg:
                    return "dead", 404, msg
            return "alive", r.status_code, ""

        # 百度网盘
        elif "baidu" in source or "bdpan" in source:
            r = http_requests.post(
                "https://pan.baidu.com/share/verify",
                data={"surl": share_id, "pwd": ""},
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            d = r.json()
            errno = d.get("errno", -1)
            if errno == -12 or errno == -9:
                return "dead", r.status_code, "分享已失效"
            elif errno == 0 or errno == -6:
                return "alive", r.status_code, ""
            return "unknown", r.status_code, f"errno={errno}"

        # 阿里云盘
        elif "aliyun" in source or "alipan" in source:
            # 先提取真实share_id (alipan.com/s/xxx 中的xxx)
            r = http_requests.post(
                "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous",
                json={"share_id": share_id},
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 404:
                msg = r.json().get("message", "")
                if "not found" in msg.lower() or "cannot" in msg.lower():
                    return "dead", 404, msg
            elif r.status_code == 200:
                return "alive", 200, ""
            return "unknown", r.status_code, ""

        # 迅雷/115/pikpak/其他 - 用HTTP GET检查页面
        else:
            r = http_requests.get(url, timeout=10, allow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"})
            body = r.text[:2000].lower()
            dead_words = ['已失效', '已过期', '已删除', '不存在', '取消分享', '违规',
                          '页面不存在', '资源不存在', '分享已过期', '链接已失效',
                          '文件已删除', '该分享已过期', '此分享已过期', 'expired',
                          'removed', 'not found']
            for word in dead_words:
                if word in body:
                    return "dead", r.status_code, f"页面包含'{word}'"
            if r.status_code == 200:
                return "alive", 200, ""
            return "unknown", r.status_code, ""

    except http_requests.exceptions.Timeout:
        return "timeout", 0, "请求超时"
    except http_requests.exceptions.ConnectionError:
        return "dead", 0, "连接失败"
    except Exception as e:
        return "error", 0, str(e)[:50]
