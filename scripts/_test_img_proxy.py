import sys
sys.path.insert(0, '/app')
import app

cases = [
    ("https://image.tmdb.org/t/p/w500/abc.jpg",          True,  "合法 TMDB 图片"),
    ("https://image.tmdb.org/t/p/original/x.png",        True,  "合法 TMDB 原图"),
    ("https://img1.doubanio.com/view/photo/x.jpg",       True,  "合法 豆瓣"),
    ("https://evil.com/?x=image.tmdb.org",               False, "子串绕过(旧漏洞1)"),
    ("https://tmdb.org.evil.com/steal",                  False, "域名伪装(旧漏洞2)"),
    ("https://image.tmdb.org@evil.com/x.jpg",            False, "userinfo混淆(旧漏洞3)"),
    ("https://eviltmdb.org/x.jpg",                       False, "后缀伪装 eviltmdb.org"),
    ("http://image.tmdb.org/x.jpg",                      False, "非 https"),
    ("https://127.0.0.1:5000/api/health",                False, "直连本机"),
    ("https://192.168.0.201/x.jpg",                      False, "直连内网"),
    ("https://169.254.169.254/latest/meta-data/",        False, "云元数据地址"),
    ("https://[::1]/x.jpg",                              False, "IPv6 环回"),
    ("",                                                 False, "空串"),
    ("not-a-url",                                        False, "非 URL"),
    ("https://nonexistent-domain-xyz-12345.tmdb.org/a",  False, "DNS 解析失败"),
]

fails = 0
for url, want, desc in cases:
    ok, reason = app._img_proxy_url_ok(url)
    verdict = "PASS" if ok == want else "**FAIL**"
    if ok != want:
        fails += 1
    got = "允许" if ok else "拒绝(" + reason + ")"
    print("  [%s] 期望=%s 实际=%s  %s" % (verdict, "允许" if want else "拒绝", got, desc))

print()
print("失败数: %d (0 = 全部符合预期)" % fails)
