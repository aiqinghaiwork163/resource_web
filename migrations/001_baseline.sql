-- ============================================================
-- resource_web 数据库基线建表脚本
-- 来源：2026-10-16 从 N200 生产库 pan_resource 经 SHOW CREATE TABLE 导出
--       + app.py 内 4 个边缘表定义（group_activity / group_member_snapshots /
--         watchlist / announcements，库中尚未创建，app 运行时懒建）
--       + services/auto_clean.py 的 resources_archive（LIKE resources + archived_at）
-- 用途：新环境初始化 / 灾后重建。幂等（IF NOT EXISTS），可重复执行。
-- 库内 14 张表均无外键约束，顺序按核心程度排列，无严格依赖要求。
-- 后续变更：新增 migrations/002_xxx.sql、003_xxx.sql……递增编号，见 README.md。
-- ============================================================

-- ---- users ----
CREATE TABLE IF NOT EXISTS `users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `email` varchar(200) COLLATE utf8mb4_unicode_ci DEFAULT '',
  `password` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '',
  `role` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT 'user',
  `status` int DEFAULT '1' COMMENT '1=?? 0=??',
  `last_login` datetime DEFAULT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `allow_login` tinyint(1) DEFAULT '1',
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- resources ----
CREATE TABLE IF NOT EXISTS `resources` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `keyword` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '搜索关键词',
  `source` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '来源平台',
  `url` text COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '资源链接',
  `title` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '资源标题',
  `note` text COLLATE utf8mb4_unicode_ci COMMENT '备注信息',
  `password` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '提取密码',
  `datetime` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '收录时间',
  `images` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin COMMENT '封面图片',
  `quality` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '画质(4K/1080P/720P)',
  `size` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '文件大小',
  `language` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '语言',
  `type` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '类型(电影/剧集/动漫/短剧/其他)',
  `year` int DEFAULT NULL COMMENT '年份',
  `rating` decimal(3,1) DEFAULT NULL COMMENT '评分',
  `tmdb_id` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT 'TMDB ID',
  `douban_id` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '豆瓣ID',
  `tags` text COLLATE utf8mb4_unicode_ci COMMENT '标签(JSON数组)',
  `is_valid` tinyint(1) DEFAULT '1' COMMENT '是否有效',
  `click_count` int DEFAULT '0' COMMENT '点击次数',
  `download_count` int DEFAULT '0' COMMENT '下载次数',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `last_checked` datetime DEFAULT NULL,
  `link_status` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_source_url` (`source`(20),`url`(200)),
  KEY `idx_keyword` (`keyword`),
  KEY `idx_source` (`source`),
  KEY `idx_type` (`type`),
  KEY `idx_quality` (`quality`),
  KEY `idx_year` (`year`),
  KEY `idx_rating` (`rating`),
  KEY `idx_created` (`created_at`),
  KEY `idx_keyword_source` (`keyword`,`source`),
  KEY `idx_type_created` (`type`,`created_at`),
  KEY `idx_source_type` (`source`,`type`),
  FULLTEXT KEY `ft_title_note` (`title`,`note`),
  CONSTRAINT `resources_chk_1` CHECK (json_valid(`images`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='网盘资源表';

-- ---- share_links ----
CREATE TABLE IF NOT EXISTS `share_links` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `resource_id` bigint NOT NULL,
  `token` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_by` int DEFAULT NULL,
  `click_count` int DEFAULT '0',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `token` (`token`),
  KEY `idx_token` (`token`),
  KEY `idx_resource` (`resource_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- favorites ----
CREATE TABLE IF NOT EXISTS `favorites` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `resource_id` bigint NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_res` (`user_id`,`resource_id`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- resource_aggregate ----
CREATE TABLE IF NOT EXISTS `resource_aggregate` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `resource_hash` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '资源哈希(用于去重)',
  `title` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '资源标题',
  `sources` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL COMMENT '来源列表 [{"source":"quark","url":"xxx"},{"source":"baidu","url":"yyy"}]',
  `best_source` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '最佳来源',
  `best_url` text COLLATE utf8mb4_unicode_ci COMMENT '最佳链接',
  `best_quality` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '最佳画质',
  `type` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT '' COMMENT '类型',
  `year` int DEFAULT NULL,
  `rating` decimal(3,1) DEFAULT NULL,
  `tags` text COLLATE utf8mb4_unicode_ci,
  `view_count` int DEFAULT '0',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_hash` (`resource_hash`),
  KEY `idx_title` (`title`(100)),
  KEY `idx_type` (`type`),
  KEY `idx_year` (`year`),
  CONSTRAINT `resource_aggregate_chk_1` CHECK (json_valid(`sources`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='资源聚合表(去重合并)';

-- ---- import_queue ----
CREATE TABLE IF NOT EXISTS `import_queue` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `keyword` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` enum('pending','processing','done','failed') COLLATE utf8mb4_unicode_ci DEFAULT 'pending',
  `requested_by` int DEFAULT NULL,
  `result_count` int DEFAULT '0',
  `error_msg` text COLLATE utf8mb4_unicode_ci,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_status` (`status`),
  KEY `idx_keyword` (`keyword`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- search_history ----
CREATE TABLE IF NOT EXISTS `search_history` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `keyword` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `source_count` int DEFAULT '0',
  `result_count` int DEFAULT '0',
  `duration_ms` int DEFAULT '0',
  `from_cache` tinyint(1) DEFAULT '0',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_keyword` (`keyword`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='搜索历史记录';

-- ---- search_logs ----
CREATE TABLE IF NOT EXISTS `search_logs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `keyword` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `ip` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `is_imported` tinyint(1) DEFAULT '0',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_keyword` (`keyword`),
  KEY `idx_imported` (`is_imported`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- search_suggestions ----
CREATE TABLE IF NOT EXISTS `search_suggestions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `keyword` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `hit_count` int DEFAULT '1',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `keyword` (`keyword`),
  KEY `idx_kw` (`keyword`),
  KEY `idx_hits` (`hit_count` DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- movie_ratings ----
CREATE TABLE IF NOT EXISTS `movie_ratings` (
  `id` int NOT NULL AUTO_INCREMENT,
  `title` varchar(500) COLLATE utf8mb4_unicode_ci NOT NULL,
  `year` varchar(10) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `imdb_rating` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `rotten_tomatoes` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `metacritic` varchar(20) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `genre` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `director` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `actors` text COLLATE utf8mb4_unicode_ci,
  `plot` text COLLATE utf8mb4_unicode_ci,
  `poster` varchar(1000) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_title` (`title`,`year`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- subscriptions ----
CREATE TABLE IF NOT EXISTS `subscriptions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `keyword` varchar(200) COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_kw` (`user_id`,`keyword`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- settings ----
CREATE TABLE IF NOT EXISTS `settings` (
  `id` int NOT NULL AUTO_INCREMENT,
  `key` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `value` text COLLATE utf8mb4_unicode_ci,
  `description` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `key` (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- filter_words ----
CREATE TABLE IF NOT EXISTS `filter_words` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `word` varchar(100) COLLATE utf8mb4_unicode_ci NOT NULL,
  `category` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT 'general',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_word` (`word`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- operation_logs ----
CREATE TABLE IF NOT EXISTS `operation_logs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int DEFAULT NULL,
  `username` varchar(100) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `action` varchar(50) COLLATE utf8mb4_unicode_ci NOT NULL,
  `detail` text COLLATE utf8mb4_unicode_ci,
  `ip` varchar(50) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_action` (`action`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- resources_archive ----
-- 与 services/auto_clean.py 的建表逻辑一致：LIKE resources 再加 archived_at 列
CREATE TABLE IF NOT EXISTS `resources_archive` LIKE `resources`;

-- ---- group_activity ----
-- 来源：app.py 内 CREATE TABLE IF NOT EXISTS（懒建逻辑，库中尚无此表）
CREATE TABLE IF NOT EXISTS `group_activity` (
  `tg_id` varchar(32) NOT NULL,
  `day` date NOT NULL,
  PRIMARY KEY (`tg_id`,`day`),
  KEY `idx_day` (`day`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- group_member_snapshots ----
-- 来源：app.py 内 CREATE TABLE IF NOT EXISTS（懒建逻辑，库中尚无此表）
CREATE TABLE IF NOT EXISTS `group_member_snapshots` (
  `id` int NOT NULL AUTO_INCREMENT,
  `counted_at` datetime NOT NULL,
  `member_count` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_counted` (`counted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- watchlist ----
-- 来源：app.py 内 CREATE TABLE IF NOT EXISTS（懒建逻辑，库中尚无此表）
CREATE TABLE IF NOT EXISTS `watchlist` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `tmdb_id` bigint NOT NULL,
  `media_type` varchar(16) NOT NULL DEFAULT 'movie',
  `title` varchar(255) NOT NULL DEFAULT '',
  `poster` text NULL,
  `year` varchar(8) NULL,
  `rating` varchar(16) NULL,
  `overview` text NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_tmdb` (`user_id`,`tmdb_id`,`media_type`),
  KEY `idx_watchlist_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---- announcements ----
-- 来源：app.py 内 CREATE TABLE IF NOT EXISTS（懒建逻辑，库中尚无此表）
CREATE TABLE IF NOT EXISTS `announcements` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `title` varchar(255) NOT NULL DEFAULT '',
  `content` mediumtext NULL,
  `type` varchar(32) NOT NULL DEFAULT 'text',
  `active` tinyint(1) NOT NULL DEFAULT 1,
  `start_time` datetime NULL DEFAULT NULL,
  `end_time` datetime NULL DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_active_window` (`active`,`start_time`,`end_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
