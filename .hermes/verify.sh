#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# 源码已 bind-mount (/app, gunicorn --reload)，无需 rebuild。
# 仅确保容器在跑（幂等；已在跑则无操作）。
docker compose up -d >/dev/null 2>&1 || true

echo "[verify] waiting for app ready..."
for i in {1..60}; do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/ 2>/dev/null || true)
  if [ "$code" = "200" ]; then
    echo "[verify] app ready (http $code)"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "[verify] timeout waiting for app"
    exit 1
  fi
  sleep 2
done

echo "[verify] checking routes..."
check() {
  local label="$1"
  local url="$2"
  local expected="$3"
  local method="${4:-GET}"
  local data="${5:-}"
  if [ "$method" = "POST" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d "$data" "$url" 2>/dev/null || true)
  else
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)
  fi
  if [ "$code" = "$expected" ]; then
    echo "[ok] $label -> $code"
  else
    echo "[fail] $label -> got $code, expected $expected"
    exit 1
  fi
}

check "index" "http://localhost:5000/" "200"
check "login page" "http://localhost:5000/login" "200"
check "github oauth" "http://localhost:5000/login/github" "302"
check "csrf-token" "http://localhost:5000/api/csrf-token" "200"
check "site-settings" "http://localhost:5000/api/site-settings" "200"
check "admin-no-auth" "http://localhost:5000/admin" "302"
# 热搜 rail 已下架：/api/home/feed 应不复存在
check "home-feed-removed" "http://localhost:5000/api/home/feed?limit=18" "404"
# 关联功能仍可用
check "home-posters" "http://localhost:5000/api/home/posters" "200" "POST" '{"titles":["复仇者联盟"]}'

echo "[verify] all checks passed"
