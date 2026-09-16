# 死链巡检与资源去重板块修复计划

## A. 死链检测逻辑修复(app.py)

1. **夸克误判存活**(app.py:6579 `_check_single_link`):夸克分支改为与 `scripts/scan_all_deadlinks.py` 一致——接口 HTTP 200 但业务 `code != 0`(如“分享不存在”)判 `dead`;仅网络异常判 unknown。
2. **网络异常保守判定**:`Timeout` / `ConnectionError` / 其他异常统一返回 `unknown`(而非 ConnectionError→dead / 其他→error),并记录 last_checked。这样后台巡检的 where 条件天然会重扫它们。
3. **巡检死角**:`_bg_scan_worker_thread` 的查询条件从 `link_status IN ('','unknown')` 扩展为 `last_checked IS NULL OR link_status IN ('','unknown','timeout','error')`;另加“复检存活”开关(bg_start 传 `rescan_alive=true` 时把 alive 一并重扫),前端加复选框。
4. **错误不再静默**:bg worker 与 auto_scan 的 `except: pass` 改为记录到 bg 状态/operation_logs,前端可见失败数与原因。

## B. 死链板块前端修复(admin.html)

5. **接线死代码**:给 `scanDeadlinks()`(手动批量扫描,引用了不存在的 `dlBatchSize` 等 DOM)补齐对应元素和按钮,或直接移除该函数避免误导;`previewAutoClean/runAutoClean` 同理——补齐 `acDays/acResult` 元素并挂到自动清理小节(后端 API `/api/admin/deadlinks/auto_clean/preview|run` 已存在,只差 UI)。
6. 删除按钮的计数不再从 DOM 文本解析,改由 `/api/admin/deadlinks/delete` 返回值驱动。

## C. 资源去重 bug 修复(保持精确匹配规则不变)

7. **文案与实现统一**:`by=title` 时 SQL 改为 `GROUP BY title, source`(与 UI 文案“按标题与来源重复”一致),clean API 同步。
8. **修复 key 转义/XSS**:`cleanSingleGroupDuplicate` 的 onclick 字符串拼接改为 data-属性 + 事件委托,避免 title 含引号时按钮失效或注入。
9. **悬空引用清理**:clean API 删除 resources 时同步清理 favorites/watchlist 等引用表中对应 resource_id(存在哪些引用表在实施时确认)。
10. **keyword 全库去重风险**:在 UI 上对 `by=keyword` 的全库一键去重加确认提示(说明会把不同来源同关键词的记录互相删除,仅保留最新一条)。

## D. 验证

- 后台手动触发一次小批量 bg 巡检,抽查各网盘来源的 link_status 判定是否符合预期(重点:夸克失效分享应标 dead、断网站点应标 unknown 且可重扫)。
- 去重页面:构造重复 title(相同来源)的记录验证分组/清理;含引号 title 验证按钮可用。
- 现有改动均已备份 .bak,沿用同一备份惯例(app.py / admin.html)。

不改动的部分:去重匹配规则保持精确字符串匹配;`scripts/scan_all_deadlinks.py` 独立脚本不动(仅对齐夸克判定逻辑作为参考)。