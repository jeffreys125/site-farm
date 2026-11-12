# site-farm

面向英文垂直小站的半自动内容流水线。它做的事情很具体：

1. 从**允许采集的来源**（RSS / 公开数据）取事实信号
2. 用 **DeepSeek 或任意 OpenAI 兼容模型**生成结构化原创长文（表格、FAQ、JSON-LD、内链）
3. 通过 WordPress REST API 发布到你自己的站
4. 生成 sitemap 并推送到 IndexNow

它**不**做的事情：整站搬运、原文洗稿、批量生成空壳页面。原因写在 `docs/plan.html` 里——那条路在 2024 年之后的 Google 算法下已经走不通了，不是道德问题，是覆盖率问题。

---

## 两种跑法

| | Docker（推荐） | 本地脚本 |
|---|---|---|
| 适合 | 一台机器上把九个站全跑起来 | 已有宝塔、站点已经建好 |
| 需要 | Docker Desktop / Docker Engine | Python 3.10+ |
| 建站 | 自动，一条命令 | 手动装八个 WordPress |
| 文档 | [`docs/deploy-docker.md`](docs/deploy-docker.md) | 下面的「快速开始」 |

---

## Docker 一键起站

```bash
cp .env.docker.example .env     # 改 DOMAIN、数据库密码、AI 配置
docker compose up -d
docker compose logs -f web      # 看九个站依次装好，实测约 2 分钟
```

默认建 **9 个站**：1 个品牌主站（`example.com`）+ 8 个内容子站（`diy.` / `pet.` / `camp.` …）。数量由 `.env` 的 `SITES` 决定，加一个 id 就多一个站。

首次启动会自动做这些事，**不需要手动进任何后台**：

- 建 MySQL，为每个站开独立数据库
- 装 WordPress、设固定链接、关评论、删示例内容
- 为每个站生成 Application Password，写到共享 volume
- 流水线容器启动时自动读取这些密码

默认端口 `8080`，和宝塔的 80/443 不冲突。域名、HTTPS、备份、排错的完整说明见 **[`docs/deploy-docker.md`](docs/deploy-docker.md)**。

---

## AI 配置：DeepSeek / 自建 New API / 任意兼容端点

流水线只依赖 OpenAI 兼容的 `POST /chat/completions`，所以供应商可以自由切换。**在 `.env` 里选一套配置即可，不用改代码。**

### 两套现成配置

**A. DeepSeek 官方（默认）**

```ini
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-你的key
```

**B. 自建 New API（例如 `dots3-note-prev`）**

```ini
LLM_PROVIDER=newapi
LLM_API_KEY=ak-你的key
LLM_BASE_URL=https://你的域名/v1
LLM_MODEL=dots3-note-prev
```

`newapi` 预设会自动关闭思考模式（见下节），所以 `LLM_THINKING` 可以不写。用 `LLM_PROVIDER=custom` 则走 `auto`，按模型名自动判断。

> 密钥只写进 `.env`（已在 `.gitignore` 里），不要写进 `config/sites.yaml`，更不要提交到仓库。

### 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `LLM_PROVIDER` | 预设：`deepseek` / `newapi` / `custom` | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek 官方密钥（旧配置继续可用） | 空 |
| `LLM_API_KEY` | 通用密钥，**优先于** `DEEPSEEK_API_KEY` | 空 |
| `LLM_BASE_URL` | 端点，**必须带 `/v1`**，不要带尾斜杠 | 跟随预设 |
| `LLM_MODEL` | 模型名（以供应商控制台为准） | 跟随预设 |
| `LLM_THINKING` | `on` / `off` / `auto`，覆盖预设 | `deepseek=on`、`newapi=off`、`custom=auto` |
| `LLM_MAX_TOKENS` | 单次生成上限 | `8192` |

优先级：`.env` 变量 > `LLM_PROVIDER` 预设 > `config/sites.yaml` 的 `project.deepseek` 段 > 内置默认值。`project.deepseek` 这个键名只为兼容旧配置保留（渲染脚本也认这个名字），它现在配置的是任意供应商；切换到非 DeepSeek 供应商时，该段里的 `base_url` / `model` 会被忽略，请在 `.env` 里显式填写。

`auto` 会按模型名里的 `qwen3` / `qwq` / `dots3` / `glm-4.5` / `kimi-k2-thinking` / `*-think*` 等线索自动关思考。拿不准就显式写 `LLM_THINKING=off`。

### 为什么推理模型必须关思考

带思考的模型（`dots3-note-prev`、Qwen3 等）会把 `max_tokens` 几乎全部消耗在 `reasoning_content` 上，返回里**根本没有 `content`**，文章生成直接失败。同一篇长文的实测对比：

| | 不关思考 | 关思考 |
|---|---|---|
| 耗时 | 125 s | **49 s** |
| completion tokens | 8192（打满，`finish_reason=length`） | **2282** |
| 返回内容 | 36194 字符思考，**无正文** | **8669 字符合法 JSON** |

关思考时客户端会发送 `chat_template_kwargs: {"enable_thinking": false}` 和 `reasoning_effort: "none"`（两种写法，覆盖 vLLM/Qwen3 风格与 OpenAI 风格网关）。`deepseek` 预设**不会**发送这两个参数，保证 DeepSeek 官方 API 的请求保持干净。

即使忘了关，现在也不会再崩成一句 `error: 'content'`：客户端会明确告诉你「思考耗尽了 max_tokens，请设置 `LLM_THINKING=off`」，并带上 `finish_reason` 和思考内容长度。

### 换完先自检，再试水

```bash
docker compose exec -T pipeline python run.py llm-check
docker compose exec -T pipeline python run.py gen --site diy --count 1
```

`llm-check` 会打印当前生效的 provider / base_url / model / 思考开关（密钥打码），并发一个最小真实请求，报告 HTTP 状态、耗时、token 用量、是否返回了 `reasoning_content`、JSON 能否解析。看到 `[diy] + 标题 (xxxw)` 才算生成成功。

---

## 定时发布

文章最终由 `site-farm-pipeline-1` 容器里的 `python run.py publish` 通过 **WordPress REST API**（`POST /wp-json/wp/v2/posts`）推给同机的 `web` 容器，`web` 再写进 MySQL。容器之间走内网 `http://web`，不经过公网。

默认行为是「生成完立刻 `publish` 公开」。想改成按时间慢慢放出去，用 `PUBLISH_STATUS=future`：文章以 WordPress 的 **future（计划发布）** 状态创建，带 `date` / `date_gmt`，由 WordPress 到点自动转公开——**届时 pipeline 容器不需要还醒着**。

```ini
# .env
DAILY_ENABLED=true
DAILY_AT=04:00              # 每天 04:00 生成（绝对钟点，重启不漂移）
RUN_ON_START=false          # 别在容器启动那一刻就跑第一轮
DAILY_GEN_COUNT=3

PUBLISH_STATUS=future       # publish=立刻公开 | draft=进草稿箱 | future=排期
PUBLISH_SCHEDULE_AT=06:00   # 每天第一个公开时间点
PUBLISH_SPACING_MINUTES=180 # 06:00 / 09:00 / 12:00
PUBLISH_PER_DAY=3           # 超过 3 篇顺延到次日
PUBLISH_TZ=Asia/Shanghai
```

一轮生成 3 篇/站，就排到当天 06:00、09:00、12:00；下一轮再排到次日同一组时间。生成时间（`DAILY_AT`）和公开时间（`PUBLISH_SCHEDULE_AT`）是两回事，别设成同一个。

**一个必须知道的坑**：WordPress 的 WP-Cron 是「有人访问站点时才顺带检查」的懒触发，站群刚起步没流量时，计划文章可能拖几小时才公开。要么接受这个延迟，要么在宿主机挂一个系统 cron 主动触发：

```bash
# crontab -e，每 10 分钟，九个子站各跑一次
*/10 * * * * cd /opt/site-farm && for s in hub diy woodworking pet camp kitchen auto garden homeoffice; do \
  docker compose exec -T web wp cron event run --due-now --allow-root --path=/var/www/sites/$s >/dev/null 2>&1; done
```

`future` 模式下 `submit` 只写 sitemap、不往 IndexNow 推 URL（文章还没公开，推了只会让爬虫吃 404）；公开后的下一轮循环会补交。

**不想让容器常驻跑循环？** 设 `DAILY_ENABLED=false`，改由外部定时任务到点执行：

```bash
docker compose exec -T pipeline python run.py gen --count 3
docker compose exec -T pipeline python run.py publish --wp-status future \
  --schedule-at 06:00 --spacing-minutes 180 --per-day 3 --timezone Asia/Shanghai
```

挂在宿主机 crontab 最省事。注意：**WorkBuddy 的自动化定时任务跑在独立沙箱，进不了你服务器的 Docker 内网**，没法直接 `docker compose exec`。要让它触发，得在服务器上额外放一个公网可达、带 token 的触发接口（和「家事安康会」那个任务的 `receiver.php` 是同一套模式），或者让它直接走公网的 WordPress REST API 发文章。

**别一上来跑 27 篇**：前 1–2 个月建议保持 `draft` 人工过一遍，AdSense 审核期间尤其如此。

---

## 改完 `.env` 之后怎么让它生效

**一句话：`docker compose restart` 不会重新读 `.env`，必须用 `docker compose up -d`。**

`restart` 只是把容器里的进程重启一遍，环境变量还是创建容器时的那一份，改了等于没改。`up -d` 才会重新解析 `.env`、比对配置，并且只重建真正变了的容器。

| 改了什么 | 执行（均省略 `docker compose`） | 为什么 |
|---|---|---|
| `.env` 里的任何值（含 `LLM_*`） | `up -d` 或 `up -d pipeline` | 只有 `up` 会重读 `.env` |
| `config/sites.yaml` | `restart pipeline` | 挂载文件，只需重跑渲染 |
| `sitefarm/*.py` | `up -d --build pipeline` | 代码打进镜像，不重建不生效 |
| `Dockerfile`、`requirements.txt` | `up -d --build` | 同上，还要重装依赖 |
| `WP_ADMIN_USER` | `up -d web` | 缺这个账号会自动补建 |
| `WP_ADMIN_PASSWORD` | 改了不影响已有账号 | 只在建账号时用一次 |

### 两个不能直接改的

**`DB_PASSWORD` / `MYSQL_ROOT_PASSWORD`** —— 数据卷已经初始化过，MySQL 里的密码不会跟着 `.env` 变，直接改会让所有站立刻 500。必须先改数据库、再改文件：

```bash
docker compose exec db mysql -uroot -p'旧root密码' -e \
  "ALTER USER 'wp'@'%' IDENTIFIED BY '新密码';"
# 然后改 .env，再 docker compose up -d
```

**`DOMAIN` / `SITE_SCHEME`** —— 这两个可以放心改。容器重建时会自动把每个站 `wp-config.php` 里的 `WP_HOME` / `WP_SITEURL` 改写成新地址，同时更新数据库里的同名选项。地址没变时完全不碰，只打一行 `already installed, skipping`。

### 确认改动真的生效

```bash
docker compose config | grep -E "LLM_|DOMAIN|SITE_SCHEME"       # .env 有没有被读到
docker compose ps                                                # STATUS 里看是不是刚重建
docker compose exec pipeline env | grep -E "^LLM_"                # 容器里实际拿到的值
docker compose exec pipeline python run.py llm-check               # 真实请求验证
```

### 什么时候需要 `--build`

改 `.env` **永远不需要** `--build`。只有改了打进镜像的东西才需要：`sitefarm/` 下的 Python 代码、`docker/` 下的文件、`requirements.txt`。有层缓存，通常 20–40 秒。

---

## 快速开始（Windows 本地脚本）

```bat
:: 1. 环境准备（只需一次）
scripts\setup.bat

:: 2. 填配置：.env 里选 DeepSeek 或自建 New API 那套配置
notepad config\sites.yaml     :: 把 example.com 换成你的域名
notepad .env

:: 3. 先跑一次干跑，不碰线上
.venv\Scripts\activate
python run.py llm-check
python run.py gen --site diy --count 2
python run.py publish --site diy --dry-run
python run.py serve
```

Linux / 宝塔同理，把 `.bat` 换成 `.sh`。日常跑批：`scripts\daily.bat`，挂到 Windows 任务计划程序（每天 06:00）。

---

## 命令

| 命令 | 作用 |
|---|---|
| `llm-check` | **发一个真实请求，验证当前 AI 配置是否可用** |
| `status` | 每个站的已生成 / 待发布 / 已发布 / 素材池 / 关键词队列 |
| `keywords --count 25` | 让模型产出长尾关键词，写入队列 |
| `plan --count 25` | 导出内容日历到 `output/content-plan.csv` |
| `fetch` | 拉取配置里的 RSS 到素材池（遵守 robots.txt） |
| `gen --count 3` | 按队列生成文章，**每个站** 3 篇 |
| `publish --wp-status draft` | 推送到 WordPress（`draft` / `publish` / `future`） |
| `publish --wp-status future --schedule-at 06:00 --spacing-minutes 180 --per-day 3` | 按时间排期发布 |
| `submit` | 生成 sitemap + IndexNow 提交 |
| `check` | 验证 WordPress 应用密码是否可用 |
| `serve` | 本地预览生成的 HTML |

全局参数：`--config` 指定配置文件，`--mock` 无 key 也能跑通，`--site` 只处理单站。

---

## 配置文件要点

`config/sites.yaml` 里每个站是一段，关键字段：

- `niche` / `audience` / `tone` —— 直接进 system prompt，决定文风。这三个字段写得越具体，产出质量差距越大。
- `siblings` —— 允许互链的兄弟站。**不要全站互链**，每站只链 3 个强相关站。
- `sources` —— 每个源必须标 `license`：`public-domain`（美国联邦政府作品）→ 事实可自由使用；`cc-by` / `cc-by-sa` → 可用但需署名；`reference-only` → **只取事实，禁止复用句子**。
- `min_words` / `max_words` —— 写进 prompt 的目标字数，**不是硬性闸门**。不同模型对长度指令的遵循度差别很大，换模型后建议抽查几篇。

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

## 排错速查

| 现象 | 原因 / 处理 |
|---|---|
| 文章是 `This is a mock article body` | **没有任何 key**（`LLM_API_KEY` / `DEEPSEEK_API_KEY` 都为空）。代码会静默进入 mock 模式，不报错也不调模型——不是"生成失败"，是根本没调 API。检查 `docker compose exec pipeline env \| grep LLM_` |
| `llm-check` 返回 404 | `LLM_BASE_URL` 少了 `/v1`，程序会拼 `/chat/completions` |
| `llm-check` 返回 308 / 超时 | `LLM_BASE_URL` 结尾多了 `/`，去掉 |
| 返回 403 `..._key_not_allowed` | 密钥无效、没绑模型、或额度/白名单限制，去 New API 后台检查 |
| `模型响应里没有正文 content` | 推理模型没关思考，思考耗尽了 `max_tokens`。设 `LLM_THINKING=off` 或用 `LLM_PROVIDER=newapi` |
| `HTTP 400 with json mode` | 供应商不支持 `response_format: json_object`；客户端会自动去掉该参数重试一次，靠解析兜底，不致命 |
| `site 'diy' has no WordPress application password configured` | 流水线启动时 web 还没装完站，`docker compose restart pipeline` 重新渲染 |

---

## 目录结构

```
site-farm/
├─ docker-compose.yml          MySQL + Web + Pipeline 三件套
├─ .env.docker.example         Docker 部署配置模板（含两套 AI 配置）
├─ .env.example                本地脚本配置模板
├─ docker/
│  ├─ Dockerfile               Apache + PHP + WordPress 底座
│  ├─ entrypoint.sh            首次启动自动装 N 个站 + 生成 API 密码
│  ├─ apache-wildcard.conf.tpl 泛域名路由
│  ├─ pipeline.Dockerfile      内容流水线镜像
│  └─ pipeline-entrypoint.sh   渲染配置 + 加载密码 + 每日循环
├─ config/sites.yaml          站点与内容策略
├─ sitefarm/
│  ├─ config.py               配置解析（provider 预设 / 环境变量）
│  ├─ store.py                SQLite 台账 + SimHash 查重
│  ├─ llm.py                  通用 OpenAI 兼容客户端（重试/限速/思考模式/JSON 修复）
│  ├─ sources.py              合规采集（robots + license）
│  ├─ writer.py               prompt 工程 + 后处理 + JSON-LD
│  ├─ publisher.py            WordPress REST 发布
│  ├─ seo.py                  sitemap + IndexNow
│  └─ cli.py                  命令行（含 llm-check）
├─ scripts/                    Windows / Linux 跑批与自检脚本
├─ docs/
│  ├─ plan.html               完整方案（先看这个）
│  └─ deploy-docker.md        Docker 部署、HTTPS、备份、排错
└─ data/pipeline.db           运行状态（.gitignore 已排除）
```
