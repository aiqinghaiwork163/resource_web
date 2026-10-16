# migrations — resource_web 数据库版本化

## 编号规则

- `001_baseline.sql`：基线，2026-10-16 从生产库 `pan_resource` 经 `SHOW CREATE TABLE`
  导出（含 app.py 懒建的 4 个边缘表 + `resources_archive`）。新环境/灾后重建从这里起。
- 后续变更按 `002_xxx.sql`、`003_xxx.sql`……三位零填充递增，一个文件只做一件事
  （建表 / 加列 / 加索引 / 数据订正），文件名用英文简述内容，
  例：`002_add_resources_expires_at.sql`。
- 每个文件必须幂等（`IF NOT EXISTS` / 先判存在再 ALTER），可重复执行。

## 如何执行

在 N200 上，进仓库目录后执行（密码从 `.env` 取，不要写进命令历史）：

```bash
cd /vol1/1000/docker/resource_web
U=$(grep '^DB_USER=' .env | cut -d= -f2)
P=$(grep '^DB_PASSWORD=' .env | cut -d= -f2)
N=$(grep '^DB_NAME=' .env | cut -d= -f2)
docker exec -i resource-mysql mysql -h 127.0.0.1 -u"$U" -p"$P" "$N" < migrations/002_xxx.sql
```

注意：`res_app` 账号限制了来源 host，在宿主机上 `docker exec` 进 mysql 容器用
localhost 会被拒绝，必须加 `-h 127.0.0.1` 走 TCP。

## 验证基线

```bash
# 建个空库跑一遍，确认无报错、表数量符合预期（当前 19 张）
docker exec -i resource-mysql mysql -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" \
  -e "CREATE DATABASE migtest;" \
  && docker exec -i resource-mysql mysql -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" \
  migtest < migrations/001_baseline.sql \
  && docker exec -i resource-mysql mysql -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" \
  -N -B -e "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='migtest';"
# 验完删掉
docker exec -i resource-mysql mysql -h 127.0.0.1 -uroot -p"$MYSQL_ROOT_PASSWORD" \
  -e "DROP DATABASE migtest;"
```

## 记录

| 编号 | 日期 | 内容 |
|---|---|---|
| 001 | 2026-10-16 | 基线：14 张生产表 + resources_archive + 4 边缘表 |
