# Cloudflare / 反向代理 — TLS 与安全加固参考
# 应用层已通过 Flask 注入 CSP、HSTS（HTTPS 下）、安全 Cookie。
# 以下项需在 Cloudflare 或 Nginx 边缘完成（审计报告 P0）。

## Cloudflare（vault.aaaaaaaa.host 已接入）

SSL/TLS → 概述：
  - 加密模式：Full (strict)
  - 最低 TLS 版本：TLS 1.2（推荐 1.3 only）
  - 启用：Always Use HTTPS、Automatic HTTPS Rewrites、HSTS（可与源站二选一）

SSL/TLS → 边缘证书：自动续期

网络：HTTP/2、HTTP/3 (QUIC) 默认开启

## Nginx 示例（若直连源站）

```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
ssl_stapling on;
ssl_stapling_verify on;

add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
# 源站可省略 CSP/HSTS，由 Flask 统一输出
proxy_set_header X-Forwarded-Proto $scheme;
```

## 环境变量（docker-compose）

```yaml
SESSION_COOKIE_SECURE=1   # HTTPS 部署必开
ENABLE_HSTS=1             # 强制注入 HSTS（Cloudflare 已开时可省略）
VAULT_UI=1                # VaultDrive 界面
```
