# site-farm

一个面向英文垂直小站的半自动内容流水线。它做的事情很具体：

1. 从**允许采集的来源**（RSS / 公开数据）取事实信号
2. 用 DeepSeek 生成**结构化原创长文**（带表格、FAQ、JSON-LD、内链）
3. 通过 WordPress REST API 发布到你自己的站
4. 生成 sitemap 并推送到 IndexNow

它**不**做的事情：整站搬运、原文洗稿、批量生成空壳页面。原因写在 `docs/plan.html` 里——那条路在 2024 年之后的 Google 算法下已经走不通了，不是道德问题，是覆盖率问题。

---

## 两种跑法

| | Docker（推荐） | 本地脚本 |
|---|---|---|
| 适合 | 一台机器上把八个站全跑起来 | 已有宝塔、站点已经建好 |
| 需要 | Docker Desktop / Docker Engine | Python 3.10+ |
| 建站 | 自动，一条命令 | 手动装八个 WordPress |
| 文档 | [`docs/deploy-docker.md`](docs/deploy-docker.md) | 下面的「快速开始」 |

---

## Docker 一键起站

```bash
cp .env.docker.example .env     # 改 DOMAIN、数据库密码、DEEPSEEK_API_KEY
docker compose up -d
docker compose logs -f web      # 看八个站依次装好，实测约 2 分钟
```

首次启动会自动做这些事，**不需要手动进任何后台**：

- 建 MySQL，为每个站开独立数据库
- 装 WordPress、设固定链接、关评论、删示例内容
- 为每个站生成 Application Password，写到共享 volume
- 流水线容器启动时自动读取这些密码

默认端口 `8080`，和宝塔的 80/443 不冲突。域名、HTTPS、备份、排错的完整说明见 **[`docs/deploy-docker.md`](docs/deploy-docker.md)**。

---

## 快速开始（Windows 本地脚本）

```bat
:: 1. 环境准备（只需一次）
scripts\setup.bat

:: 2. 填配置
notepad config\sites.yaml     :: 把 example.com 换成你的域名
notepad .env                  :: 填 DeepSeek key 和各站 WordPress 应用密码

:: 3. 先跑一次干跑，不碰线上
.venv\Scripts\activate
python run.py gen --site diy --count 2
python run.py publish --site diy --dry-run
python run.py serve
:: 浏览器打开 http://127.0.0.1:8099 看效果

:: 4. 确认没问题，正式发布
python run.py publish --site diy --wp-status draft
```

日常跑批：`scripts\daily.bat`，然后挂到 Windows 任务计划程序（每天 06:00）。

Linux / 宝塔 Linux 同理，把 `.bat` 换成 `.sh`，路径换掉即可。

---

## 命令

| 命令 | 作用 |
|---|---|
| `status` | 每个站的已生成 / 待发布 / 已发布 / 素材池 / 关键词队列 |
| `keywords --count 25` | 让模型产出长尾关键词，写入队列 |
| `plan --count 25` | 导出内容日历到 `output/content-plan.csv` |
| `fetch` | 拉取配置里的 RSS 到素材池（遵守 robots.txt） |
| `gen --count 3` | 按队列生成文章，**每个站** 3 篇 |
| `publish --wp-status draft` | 推送到 WordPress |
| `submit` | 生成 sitemap + IndexNow 提交 |
| `check` | 验证 WordPress 应用密码是否可用 |
| `serve` | 本地预览生成的 HTML |

全局参数：`--config` 指定配置文件，`--mock` 无 key 也能跑通，`--site` 只处理单站。

---

## 配置文件要点

`config/sites.yaml` 里每个站是一段，关键字段：

- `niche` / `audience` / `tone` —— 直接进 system prompt，决定文风。这三个字段写得越具体，产出质量差距越大。
- `siblings` —— 允许互链的兄弟站。**不要全站互链**，每站只链 3 个强相关站，这是自然引用的样子。
- `sources` —— 每个源必须标 `license`：
  - `public-domain`（美国联邦政府作品，如 USDA / NIST / USGS）→ 事实可自由使用
  - `cc-by` / `cc-by-sa` → 可用但需署名
  - `reference-only` → **只取事实，禁止复用句子**，prompt 里已强制约束

---

## 质量闸门

生成阶段有三道检查，任一不过就重写或跳过：

1. **标题去重** —— 归一化后完全相同的标题直接重试
2. **slug 去重** —— 冲突时自动加后缀
3. **SimHash 正文查重** —— 64 位指纹，汉明距离 ≤ 3 判定为近重复

内容层面靠 prompt 硬约束：禁止连续 10 个词以上与素材重合、禁止编造数据、必须含比较表格、必须含 4-6 条 FAQ。

---

## 发布前必做

- 站点装好 SSL，`http` 全量 301 到 `https`
- WordPress 后台 → 用户 → 个人资料 → **应用程序密码**，生成后填进 `.env`
- 确认 Apache 没有吞掉 `Authorization` 头（`scripts/apache-wildcard-subdomain.conf` 里有对应配置）
- 每个站上传 `<INDEXNOW_KEY>.txt` 到根目录，内容就是 key 本身
- 建好 About / Contact / Privacy Policy / Editorial Policy 四个页面，AdSense 审核必看

---

## 目录结构

```
site-farm/
├─ docker-compose.yml          MySQL + Web + Pipeline 三件套
├─ .env.docker.example         Docker 部署配置模板
├─ docker/
│  ├─ Dockerfile               Apache + PHP + WordPress 底座
│  ├─ entrypoint.sh            首次启动自动装 N 个站 + 生成 API 密码
│  ├─ apache-wildcard.conf.tpl 泛域名路由
│  ├─ pipeline.Dockerfile      内容流水线镜像
│  └─ pipeline-entrypoint.sh   渲染配置 + 加载密码 + 每日循环
├─ config/sites.yaml          站点与内容策略
├─ sitefarm/
│  ├─ config.py               配置解析
│  ├─ store.py                SQLite 台账 + SimHash 查重
│  ├─ llm.py                  DeepSeek 客户端（重试/限速/JSON 修复）
│  ├─ sources.py              合规采集（robots + license）
│  ├─ writer.py               prompt 工程 + 后处理 + JSON-LD
│  ├─ publisher.py            WordPress REST 发布
│  ├─ seo.py                  sitemap + IndexNow
│  └─ cli.py                  命令行
├─ scripts/
│  ├─ setup.bat               一次性环境安装
│  ├─ daily.bat               每日跑批
│  └─ apache-wildcard-subdomain.conf
├─ docs/
│  ├─ plan.html               完整方案（先看这个）
│  └─ deploy-docker.md        Docker 部署、HTTPS、备份、排错
└─ data/pipeline.db           运行状态
```
