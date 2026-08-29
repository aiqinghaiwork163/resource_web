# resource_web (5000) → N200

路径：`/vol1/1000/docker/resource_web`  
面板：`http://192.168.0.202:5000/`

## 启动（需要时）

```bash
docker network inspect resource-net >/dev/null 2>&1 || docker network create resource-net
cd /vol1/1000/docker/resource_web
docker compose up -d
```

## 说明

- 镜像：`resource_web-resource-web:latest`（已 docker load）
- DB：`DB_HOST=resource-mysql`（compose 内 MySQL）
- Redis：`REDIS_HOST=resource_web_redis`
- 与 N100 共用同一套 GitHub OAuth 回调域名时，注意只开一侧或改回调
