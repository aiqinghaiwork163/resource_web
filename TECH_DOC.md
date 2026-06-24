# 网盘资源浏览器 - 技术文档

> 最后更新: 2026-06-24 | 版本: v4.0

## 一、项目概述

网盘资源浏览器是一个聚合搜索平台，整合多个网盘搜索引擎的资源，提供统一的搜索、浏览、收藏体验。支持 TMDB 热门推荐、自动导入、后台管理等功能。

**在线地址**: `http://wp.aiqinghaiwork.cn:5000`

---

## 二、技术栈

| 层级 | 技术 | 版本 | 说明 |
|:-----|:-----|:-----|:-----|
| **后端** | Python Flask | 3.x | Web框架 |
| **数据库** | MySQL | 8.0 | 主存储 |
| **数据库驱动** | PyMySQL | 1.x | 纯Python MySQL驱动 |
| **连接池** | DBUtils (PooledDB) | 2.x | 数据库连接池 |
| **WSGI服务器** | Gunicorn | 22.x | 多worker生产部署 |
| **容器化** | Docker + Docker Compose | - | 单容器部署 |
| **前端** | 原生 HTML/CSS/JS | - | 无框架，单文件SPA |
| **图表** | Chart.js | 4.x | 后台数据统计图表 |
| **图标** | Font Awesome | 6.5 | 图标库 |
| **外部API** | TMDB API | v3 | 电影/剧集元数据 |
| **外部API** | 多网盘搜索引擎 | - | 资源聚合搜索 |

---

## 三、架构设计

```
┌─────────────────────────────────────────────────┐
│                    Nginx (可选)                    │
│              反向代理 / SSL终止                     │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│              Docker Container                     │
│  ┌──────────────────────────────────────────┐   │
│  │         Gunicorn (4 workers)              │   │
│  │  ┌────────────────────────────────────┐  │   │
│  │  │         Flask Application           │  │   │
│  │  │                                     │  │   │
│  │  │  ┌─────┐ ┌─────┐ ┌──────────────┐ │  │   │
│  │  │  │ API │ │ 路由 │ │ 后台线程管理  │ │  │   │
│  │  │  └─────┘ └─────┘ └──────────────┘ │  │   │
│  │  │  ┌─────┐ ┌─────┐ ┌──────────────┐ │  │   │
│  │  │  │缓存 │ │限流 │ │ 自动导入引擎  │ │  │   │
│  │  │  └─────┘ └─────┘ └──────────────┘ │  │   │
│  │  └────────────────────────────────────┘  │   │
│  └──────────────────────────────────────────┘   │
│                       │                          │
│  ┌────────────────────▼──────────────────────┐  │
│  │              MySQL 8.0                     │  │
│  │  resources | users | search_logs | ...     │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   TMDB API     ps.aiqinghaiwork   pansou.app
   (绕GFW)      (主搜索源)        (备用搜索源)
```

---

## 四、数据库设计

### 4.1 核心表

| 表名 | 记录数 | 说明 |
|:-----|:------:|:-----|
| `resources` | ~18,000 | 资源主表 |
| `users` | 1 | 用户表 |
| `search_logs` | ~100 | 搜索日志 |
| `favorites` | 0 | 收藏表 |
| `filter_words` | 71 | 过滤词表 |
| `settings` | 7 | 系统设置 |
| `import_queue` | ~10 | 自动导入队列 |
| `operation_logs` | ~23 | 操作日志 |

### 4.2 resources 表结构

```sql
CREATE TABLE resources (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    keyword VARCHAR(200),          -- 搜索关键词
    source VARCHAR(50),            -- 来源(quark/baidu/aliyun/magnet...)
    url TEXT,                      -- 资源链接
    title VARCHAR(500),            -- 标题
    note TEXT,                     -- 备注
    password VARCHAR(100),         -- 提取码
    datetime VARCHAR(50),          -- 原始发布时间
    quality VARCHAR(50),           -- 画质(4K/1080P/720P)
    type VARCHAR(50),              -- 类型(电影/剧集/动漫/综艺)
    year INT,                      -- 年份
    rating DECIMAL(3,1),           -- TMDB评分
    images TEXT,                   -- 海报图JSON
    tmdb_id BIGINT,                -- TMDB ID
    click_count INT DEFAULT 0,     -- 点击次数
    created_at TIMESTAMP,          -- 入库时间
    UNIQUE KEY uk_url (url(255))   -- URL去重
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
```

### 4.3 settings 表结构

```sql
CREATE TABLE settings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    `key` VARCHAR(100) NOT NULL UNIQUE,
    value TEXT,
    description VARCHAR(500),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

当前配置项：
| key | 默认值 | 说明 |
|:----|:------|:-----|
| `import_api_url` | 多行URL | 搜索API地址(换行分隔多个) |
| `import_api_limit` | 30 | 单次导入最大条数 |
| `tmdb_api_key` | 91f75e... | TMDB API密钥 |
| `auto_import_enabled` | 1 | 自动导入开关 |
| `auto_import_min_searches` | 2 | 触发自动导入的最小搜索次数 |
| `site_name` | 网盘资源浏览器 | 站点名称 |
| `site_footer` | ... | 页脚文字 |

---

## 五、API 接口文档

### 5.1 公开 API（无需认证）

| 方法 | 路径 | 说明 |
|:-----|:-----|:-----|
| GET | `/api/resources?q=&source=&page=&per_page=` | 搜索资源 |
| GET | `/api/stats` | 全站统计 |
| GET | `/api/sources` | 来源分布 |
| GET | `/api/tmdb/trending?media=&time=` | TMDB热榜 |
| GET | `/api/hot_searches` | 热门搜索 |
| GET | `/api/autocomplete?q=` | 搜索自动补全 |
| GET | `/api/resource/{id}/detail` | 资源详情 |
| GET | `/api/import_status?q=` | 检查自动导入状态 |
| GET | `/api/recommendations` | 推荐资源 |
| GET | `/api/docs` | API文档 |
| GET | `/api/img_proxy?url=` | 图片代理缓存 |

### 5.2 用户 API（需登录）

| 方法 | 路径 | 说明 |
|:-----|:-----|:-----|
| POST | `/login` | 登录 |
| GET | `/logout` | 登出 |
| GET | `/api/me` | 当前用户信息 |
| GET/POST/DELETE | `/api/favorites` | 收藏管理 |
| GET/POST/DELETE | `/api/search_history` | 搜索历史 |
| POST | `/api/change_password` | 修改密码 |
| POST | `/api/subscriptions` | 订阅管理 |

### 5.3 管理 API（需admin权限）

| 方法 | 路径 | 说明 |
|:-----|:-----|:-----|
| GET | `/api/admin/settings` | 获取系统设置 |
| POST | `/api/admin/settings` | 更新系统设置 |
| GET | `/api/admin/resources` | 后台资源列表 |
| POST | `/api/admin/resources/batch` | 批量操作 |
| GET | `/api/admin/import_queue` | 导入队列 |
| POST | `/api/admin/import_queue/retry` | 重试失败任务 |
| POST | `/api/admin/process_queue` | 手动处理队列 |
| GET | `/api/admin/stats/dashboard` | 数据统计面板 |
| GET | `/api/admin/pending_imports` | 待导入关键词 |
| GET | `/api/admin/logs` | 操作日志 |
| GET/POST/DELETE | `/api/admin/filter_words` | 过滤词管理 |
| GET | `/api/admin/users` | 用户管理 |
| POST | `/api/admin/users/add` | 添加用户 |
| POST | `/api/admin/users/{id}/toggle` | 启用/禁用用户 |
| DELETE | `/api/admin/users/{id}` | 删除用户 |
| GET | `/api/admin/test_apis` | API测试工具 |

---

## 六、核心功能实现

### 6.1 自动导入引擎

```
用户搜索 → 本地0结果 → _auto_import_trigger()
    │
    ├─ 检查是否已在队列中(避免重复)
    ├─ 检查是否已尝试2次+(避免无限重试)
    ├─ 写入import_queue表(status=processing)
    │
    └─ 启动后台线程 _process_import()
         │
         ├─ 从settings读取所有API地址(支持多个)
         ├─ 多线程并发请求所有API
         ├─ 按URL去重合并结果
         ├─ INSERT IGNORE写入resources表
         └─ 更新queue状态为done/failed
```

**关键设计**:
- 使用 `threading.Thread(daemon=True)` 实现后台处理
- 多API并发：每个API一个线程，`join(timeout=90)` 等待
- 结果按URL去重，避免重复入库
- 同一关键词搜索2次+无结果后标记为`exhausted`，不再重试

### 6.2 TMDB 热门推荐

```
前端请求 /api/tmdb/trending?media=movie&time=week
    │
    ├─ 后端通过 socket+ssl 直连 TMDB API（绕GFW）
    ├─ 解析响应，生成海报URL（通过 /api/img_proxy 代理）
    ├─ 内存缓存30分钟（@cached装饰器）
    └─ 返回 {items: [{title, poster, rating, media_type, year, overview}]}
```

**绕GFW方案**: 
- DNS解析 `www.themoviedb.org` 获取IP
- `socket.create_connection((ip, 443))` 直连
- SSL SNI设为 `api.themoviedb.org`
- 手动构造HTTP请求

### 6.3 前端缓存策略

```javascript
// localStorage缓存封装
var _lscache = {
  get: function(key, ttl) {
    var d = JSON.parse(localStorage.getItem('c_' + key));
    if (d && Date.now() - d.t < ttl * 1000) return d.v;
    return null;
  },
  set: function(key, value) {
    localStorage.setItem('c_' + key, JSON.stringify({t: Date.now(), v: value}));
  }
};

// 使用: cachedFetch(url, 缓存秒数)
var data = await cachedFetch('/api/tmdb/trending', 1800); // 缓存30分钟
```

**缓存时间规划**:
| 数据 | 缓存时间 | 原因 |
|:-----|:------:|:-----|
| TMDB热榜 | 30分钟 | 变化慢 |
| 来源统计 | 2分钟 | 变化中等 |
| 搜索结果 | 不缓存 | 实时性要求高 |
| 系统设置 | 不缓存 | 管理操作 |

### 6.4 图片代理

```
TMDB海报 → /api/img_proxy?url=https://image.tmdb.org/...
    │
    ├─ 内存缓存24小时（_img_cache字典）
    ├─ 代理请求TMDB图片
    └─ 返回图片+Cache-Control: max-age=86400
```

---

## 七、页面结构

| 页面 | 文件 | 行数 | 说明 |
|:-----|:-----|:---:|:-----|
| 首页 | `index.html` | 1457 | 搜索+推荐+结果列表 |
| 后台 | `admin.html` | 1309 | 7个Tab管理面板 |
| 个人中心 | `profile.html` | 667 | 搜索历史+收藏 |
| API文档 | `api-docs.html` | 77 | 静态API说明 |
| 登录页 | 内嵌`app.py` | - | 渐变主题登录 |

### 首页功能
- TMDB 热门推荐（4个Tab：本周/今日/电影/剧集）
- 搜索框 + 自动补全
- 网盘来源筛选
- 资源卡片列表 + 分页
- 详情面板 + 分享链接
- 批量操作（收藏/导出/选择）
- 主题切换（深色/浅色）

### 后台管理Tab
1. 👥 用户管理
2. 📋 操作日志
3. 🛡 过滤词
4. 📊 数据统计（Chart.js图表）
5. ⚙️ 系统设置
6. 📥 导入队列
7. 🧪 API测试

---

## 八、部署架构

### Docker Compose 配置

```yaml
services:
  resource-web:
    build: .
    container_name: resource_web
    ports:
      - "5000:5000"
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
      - DB_HOST=172.23.0.2
      - DB_PORT=3306
      - DB_USER=root
      - DB_PASSWORD=***
      - DB_NAME=pan_resource
      - SECRET_KEY=***
      - TMDB_API_KEY=***
    networks:
      - resource_db_default
```

### 网络架构
```
外部网络 → Nginx(可选) → Docker(5000) → Flask → MySQL(172.23.0.2:3306)
```

### 文件结构
```
/vol2/1000/docker/resource_web/
├── app.py              # 后端主文件 (1900+行)
├── index.html          # 首页 (1450+行)
├── admin.html          # 后台管理 (1300+行)
├── profile.html        # 个人中心 (670行)
├── api-docs.html       # API文档
├── tmdb_crawler.py     # TMDB爬虫脚本
├── docker-compose.yml  # Docker配置
├── requirements.txt    # Python依赖
├── static/             # 静态资源
│   └── css/all.min.css # Font Awesome
└── TECH_DOC.md         # 本文档
```

---

## 九、已知问题 & 改进方向

### 🔴 架构层面

| 问题 | 现状 | 建议改进 |
|:-----|:-----|:---------|
| **单文件架构** | app.py 1900+行，前后端未分离 | 考虑前后端分离(React/Vue + Flask API) |
| **无ORM** | 原生SQL拼接，有注入风险 | 引入SQLAlchemy或Peewee |
| **无模板引擎** | HTML内嵌JS，维护困难 | 用Jinja2模板或前端框架 |
| **无认证框架** | 自实现session认证 | 考虑Flask-Login或JWT |
| **无任务队列** | threading.Thread做后台任务 | 考虑Celery + Redis |
| **无测试** | 0测试覆盖率 | 添加pytest单元测试 |
| **无日志系统** | print输出 | 用logging模块+结构化日志 |

### 🟡 性能层面

| 问题 | 现状 | 建议改进 |
|:-----|:-----|:---------|
| **连接池配置** | PooledDB默认参数 | 调整min/max连接数 |
| **无Redis缓存** | 内存字典缓存，重启丢失 | 引入Redis做持久缓存 |
| **无CDN** | 静态文件直出 | 考虑CDN加速TMDB图片 |
| **搜索无索引** | LIKE查询，大数据量慢 | 考虑全文索引或Elasticsearch |
| **无分页缓存** | 每次搜索都查DB | 热门搜索结果缓存 |

### 🟢 功能层面

| 功能 | 优先级 | 说明 |
|:-----|:-----:|:-----|
| 更多搜索源接入 | 高 | pansou.app已接入，可继续扩展 |
| 资源去重优化 | 高 | 当前URL去重，可加标题相似度去重 |
| 用户注册 | 中 | 目前仅admin可添加用户 |
| 资源评分/评论 | 中 | 用户互动功能 |
| 资源分类自动识别 | 中 | 用NLP从标题推断类型/画质 |
| RSS订阅 | 低 | 关键词订阅+新资源推送 |
| 移动端APP | 低 | PWA或小程序 |

### 🔵 安全层面

| 问题 | 现状 | 建议改进 |
|:-----|:-----|:---------|
| **CSRF防护** | 无 | 添加Flask-WTF |
| **SQL注入** | 参数化查询(部分) | 全面使用参数化 |
| **XSS防护** | 前端esc()函数 | CSP头+更严格的转义 |
| **密码哈希** | SHA256(不安全) | 换bcrypt/argon2 |
| **Rate Limiting** | 简单内存限流 | 用Flask-Limiter+Redis |
| **API密钥管理** | 硬编码在settings表 | 用环境变量或密钥管理服务 |

---

## 十、技术亮点

1. **TMDB绕GFW直连**: socket+ssl直连TMDB API，无需代理
2. **多API并发搜索**: threading多线程并发请求，结果自动去重
3. **自动导入引擎**: 搜索无结果时自动排队+后台处理+状态反馈
4. **前端缓存策略**: localStorage + 内存缓存 + 图片代理三层缓存
5. **零框架前端**: 纯原生JS实现SPA，无构建工具依赖
6. **容器化部署**: Docker单容器，一键部署

---

## 十一、依赖清单

### Python (requirements.txt)
```
flask>=3.0
pymysql>=1.1.0
requests>=2.31.0
DBUtils>=2.0
gunicorn>=22.0
```

### 前端CDN
```
Chart.js 4.x: cdn.jsdelivr.net/npm/chart.js
Font Awesome 6.5: cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/
```

---

## 十二、版本历史

| 版本 | 日期 | 主要变更 |
|:-----|:-----|:---------|
| v1.0 | 2026-06-23 | 初始版本，基础搜索+浏览 |
| v2.0 | 2026-06-23 | 后台管理+过滤词+用户系统 |
| v3.0 | 2026-06-24 | TMDB推荐+自动导入+数据统计+系统设置 |
| v4.0 | 2026-06-24 | TMDB主题UI重写+多API并发+手机适配+API测试 |

---

*本文档可作为技术评审参考，欢迎提出改进建议。*
