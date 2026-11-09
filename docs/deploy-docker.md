# Docker 部署

整套站群（MySQL + 8 个独立 WordPress + 内容流水线）跑在三个容器里。
首次启动会自动装好所有站点并生成 API 密码，不需要手动进后台点八次。

---

## 零、Windows 前提

你机器上已经有宝塔的 Apache，所以这里只补 Docker 本身要准备的东西：

| 检查项 | 要求 |
|---|---|
| Docker | 装 **Docker Desktop**，安装时勾选 WSL2 backend |
| WSL2 | PowerShell 里 `wsl -l -v` 要能看到 `docker-desktop`，状态 `Running` |
| 虚拟化 | 任务管理器 → 性能 → CPU → 虚拟化：**已启用**（关了要进 BIOS 开 VT-x） |
| 磁盘 | 预留 **15 GB**（三个镜像 + 数据卷） |
| 内存 | Docker Desktop 设置里给 **4 GB 以上**，8 个 WordPress 一起跑不宽裕但不能低于 2 GB |

验证：

```powershell
docker version           # Client 和 Server 都要有输出，Server 空说明 Docker Desktop 没启动
docker compose version   # 要 v2.x，v1 的 docker-compose 命令写法不一样
```

> 第一次 `docker compose build` 会编译 PHP 扩展，**耗时 10–20 分钟**，取决于网络。之后重建只要几秒。
> 如果构建卡在 `apt-get` 不动，是 Debian 源太慢——在 Dockerfile 顶部把 `deb.debian.org` 换成清华镜像 `mirrors.tuna.tsinghua.edu.cn` 再构建。

---

## 一、先解决端口冲突

你机器上已经有宝塔的 Apache 占着 **80** 和 **443**。Docker 默认映射到 **8080**，两边可以共存：

| 方案 | 做法 | 适合 |
|---|---|---|
| **并存**（默认） | 容器走 8080，宝塔继续占 80/443，用宝塔做反向代理 | 保留现有环境 |
| **独占** | 停掉宝塔 Apache，`.env` 里改 `HTTP_PORT=80` | 只跑站群 |

先按默认走，验证通过了再决定。

---

## 二、启动

```bash
cd site-farm
cp .env.docker.example .env
```

打开 `.env`，**至少改这四个**：

```ini
DOMAIN=yourdomain.com          # 你的主域名
DB_PASSWORD=一个强密码
MYSQL_ROOT_PASSWORD=另一个强密码
DEEPSEEK_API_KEY=sk-xxxx       # 没有可以先空着，能起站但生成不了文章
```

然后：

```bash
docker compose up -d
docker compose logs -f web
```

首次启动会依次做这些事，**实测约 2 分钟**（干净的 volume、9 个站）：

1. 初始化 MySQL（10 秒左右）
2. 下载 WordPress 核心（镜像构建时已预下载，通常跳过）
3. 逐个创建数据库 `wp_hub` / `wp_diy` / `wp_pet` …
4. 装 WordPress、设置固定链接、关评论、删示例文章
5. **为每个站生成 Application Password**（流水线要用）
6. 启动 Apache

日志里看到这行就说明成功了：

```
[sitefarm] sites ready: hub diy woodworking pet camp kitchen auto garden homeoffice
[sitefarm] credentials: /var/www/sites/.sitefarm-credentials.env (9 site(s))
```

> 如果 `WP_ADMIN_PASSWORD` 留空，系统会随机生成一个，**只在日志里显示这一次**。用 `docker compose logs web | grep "generated admin password"` 找回来。

后台账号密码对所有站相同，站点之间靠子域区分。

---

## 三、这套东西实际跑通过什么

下面这些不是"应该可以"，是干净 volume 上真跑出来的结果：

| 检查 | 命令 | 结果 |
|---|---|---|
| 9 个站自动装好 | `docker compose logs web \| grep "sites ready"` | `auto camp diy garden homeoffice hub kitchen pet woodworking` |
| 应用密码自动生成 | `... grep credentials` | `9 site(s)` |
| 子域路由 | `curl -I -H "Host: diy.example.com" localhost:8080/` | `200` |
| 伪静态 + REST | `curl -o /dev/null -w "%{http_code}" -H "Host: diy.example.com" localhost:8080/wp-json/` | `200`（133 条路由） |
| REST 鉴权 | `curl -u editor:<应用密码> .../wp/v2/users/me` | `200`，返回 `editor` |
| 发布文章 | `docker compose exec pipeline python run.py publish --site diy` | `published=1`，WordPress 里出现草稿 |
| sitemap | `python run.py submit --site diy` | `diy.xml` 写出，IndexNow 无 key 时自动跳过 |

过程中踩到并已经修掉的四个坑（都写进代码注释了，改的时候别改回去）：

1. **MariaDB 客户端连不上 MySQL 8** —— 镜像里 `default-mysql-client` 是 MariaDB 的，遇到 MySQL 8 自签名证书直接报 `TLS/SSL error`。健康检查因此永远不通过，改用 PHP 的 mysqli 探活（和 WordPress 同一套栈）。
2. **`wp` 用户只有 `wp_hub` 的权限** —— MySQL 官方镜像只给 `MYSQL_DATABASE` 授权，建 `wp_diy` 会 access denied。`docker/db-init.sh` 在首次初始化时对 `wp_%` 通配授权。
3. **wp-cli 写不了 `.htaccess`** —— 它跑在 CLI 上下文里，`got_url_rewrite()` 为假，`wp rewrite flush --hard` 只会警告 `Regenerating a .htaccess file requires special configuration`，结果所有固定链接 404。重写规则改到 vhost 配置里，不依赖站点目录可写。
4. **Application Password 在 http 下被禁用** —— `is_ssl()` 为假时 WordPress 直接拒绝应用密码，REST 返回 401 `rest_not_logged_in`。用 mu-plugin 重新打开；等你前面挂了 TLS 代理，这个过滤器就是无害的空操作。

另外 `docker compose exec pipeline ...` 是**新进程**，不继承 entrypoint 里 source 的环境变量，所以启动时会把应用密码写进渲染后的配置（`/app/runtime/sites.yaml`），手动执行命令才不会报"没有应用密码"。

---

## 四、验证

没配 DNS 也能先验证，用 Host 头直接打：

```bash
curl -I -H "Host: diy.yourdomain.com" http://localhost:8080/
```

返回 `HTTP/1.1 200` 或 `301` 就对了。

想在浏览器里点，改本机 hosts 文件（Windows）：

```
C:\Windows\System32\drivers\etc\hosts
```

加一行：

```
127.0.0.1  yourdomain.com diy.yourdomain.com pet.yourdomain.com camp.yourdomain.com
```

然后浏览器打开 `http://diy.yourdomain.com:8080`。

后台地址 `http://diy.yourdomain.com:8080/wp-admin`，账号是 `.env` 里的 `WP_ADMIN_USER`。

---

## 五、DNS 配置

上线前在域名商处加两条记录（**两条都要**，`*` 不覆盖裸域）：

| 类型 | 主机记录 | 值 | 说明 |
|---|---|---|---|
| A | `@` | 服务器公网 IP | 裸域，hub 站用 |
| A | `*` | 服务器公网 IP | 泛解析，覆盖 diy / pet / camp … 全部子域 |

加完等 10 分钟，然后验证——**这一步别跳过**，泛解析没生效的话后面全是白忙：

```bash
nslookup diy.yourdomain.com      # 必须返回你的服务器 IP
nslookup yourdomain.com          # 裸域也要返回同一个 IP
```

返回 `NXDOMAIN` 或别的 IP，说明记录没生效或填错了主机记录（有些域名商要求填 `*.yourdomain.com` 而不是 `*`）。

> 服务器在中国大陆的话，域名必须先完成 ICP 备案，否则 80/443 会被运营商拦截。没备案之前只能用 `IP:8080` 测试，站点能跑但绑不了域名。

---

## 六、HTTPS（三选一）

### 方案 A：宝塔反向代理（你的情况，推荐）

你机器上宝塔 Apache 已经占着 80/443，所以让宝塔在前台收流量、转发给容器。**一个泛域名站点就能覆盖全部 9 个站**，不用建 9 次。

**第 1 步 · 先确认容器活着**（不碰 DNS，纯本机验证）：

```bash
curl -I -H "Host: diy.yourdomain.com" http://127.0.0.1:8080/
```

返回 `200` 再往下做；返回 404 说明容器没起来或 `DOMAIN` 填错了。

**第 2 步 · 建站点**：宝塔 → 网站 → 添加站点

| 字段 | 填什么 |
|---|---|
| 域名 | 两行：`yourdomain.com` 和 `*.yourdomain.com` |
| 根目录 | 随便建个空目录，反代用不到它 |
| PHP 版本 | **纯静态**（不要选 PHP，否则多一层无用的 FastCGI） |

**第 3 步 · 加反向代理**：点进这个站点 → 反向代理 → 添加

| 字段 | 填什么 |
|---|---|
| 代理名称 | `sitefarm` |
| 目标 URL | `http://127.0.0.1:8080` |
| 发送域名 | **`$host`** ← 这一格是成败关键 |

为什么必须是 `$host`：容器用 Host 头的第一段决定读哪个目录（`mod_vhost_alias` 的 `%1`）。发送域名留默认的 `127.0.0.1`，9 个站的请求会全部落到主域，看起来就像“所有子域都打开了同一个站”。

加完打开站点「配置文件」，确认有这三行（宝塔一般会自动写，缺了手动补）：

```apache
ProxyPreserveHost On
ProxyPass / http://127.0.0.1:8080/
ProxyPassReverse / http://127.0.0.1:8080/
```

**第 4 步 · 上证书**：站点设置 → SSL → Let's Encrypt

- 域名列表填 `yourdomain.com` 和 `*.yourdomain.com`
- 验证方式必须选 **DNS 验证**（泛域名不支持文件验证），先在宝塔「计划任务 / DNS」里配好阿里云或腾讯云的 API 密钥
- 签发成功后打开「强制 HTTPS」

**第 5 步 · 告诉容器现在走 https**：

```ini
SITE_SCHEME=https
```

```bash
docker compose up -d web pipeline
```

不改这一步，WordPress 会一直把访客跳回 `http://`，浏览器报重定向次数过多。

**第 6 步 · 按顺序验证**（从内到外，出问题好定位）：

```bash
# 1. 容器直连，绕过宝塔
curl -I -H "Host: diy.yourdomain.com" http://127.0.0.1:8080/

# 2. 经过宝塔，绕过 DNS
curl -I -H "Host: diy.yourdomain.com" http://127.0.0.1/

# 3. 走真实域名
curl -I https://diy.yourdomain.com/
```

三步都是 `200` 就通了。第 2 步失败是宝塔反代的问题，第 3 步失败是 DNS 或证书的问题。

> 不想保留宝塔？停掉宝塔 Apache，`.env` 里改 `HTTP_PORT=80`，`docker compose up -d web`，容器直接对外，省掉一层转发。

### 方案 B：Caddy 反代（不用宝塔时，全自动证书）

```bash
docker run -d --name caddy --restart unless-stopped \
  --network site-farm_farm \
  -p 80:80 -p 443:443 \
  -v caddy_data:/data -v caddy_config:/config \
  caddy:2
```

写一个 Caddyfile（子域要显式列出，泛域名证书需要 DNS 插件）：

```
yourdomain.com, www.yourdomain.com,
diy.yourdomain.com, woodworking.yourdomain.com,
pet.yourdomain.com, camp.yourdomain.com,
kitchen.yourdomain.com, auto.yourdomain.com,
garden.yourdomain.com, homeoffice.yourdomain.com {
    reverse_proxy web:80
    encode gzip
}
```

然后 `docker cp Caddyfile caddy:/etc/caddy/Caddyfile && docker exec caddy caddy reload -c /etc/caddy/Caddyfile`。

Caddy 自动申请并续期证书，不用管。

**切到 HTTPS 后必须改 `.env`：**

```ini
SITE_SCHEME=https
HTTP_PORT=8080        # 或不映射，只让 Caddy 走内网
```

### 方案 C：Cloudflare（最省事）

1. 域名 NS 托管到 Cloudflare
2. A 记录开橙云，指向服务器 IP，同样加 `@` 和 `*` 两条
3. SSL/TLS 模式选 **Flexible**（源站是 http）或 **Full**（源站配了证书选这个）

Cloudflare 会终止 TLS，回源走 http，所以 `.env` 里 `SITE_SCHEME=https`，`HTTP_PORT` 保持 8080。

> ⚠️ 用 Cloudflare 时**别开 Rocket Loader**，它会把 WordPress 后台的 JS 打乱。

---

## 七、日常使用

```bash
# 看状态
docker compose exec pipeline python run.py status

# 生成 2 篇文章（不指定 --site 就是全部站各 2 篇）
docker compose exec pipeline python run.py gen --site diy --count 2

# 推送到 WordPress（draft = 草稿，建议先这样）
docker compose exec pipeline python run.py publish --site diy --wp-status draft

# 确认没问题后改成直接发布
docker compose exec pipeline python run.py publish --site diy --wp-status publish

# 提交 sitemap 到 IndexNow
docker compose exec pipeline python run.py submit

# 进 web 容器看某个站的文件
docker compose exec web bash
```

**开每日自动跑**，改 `.env`：

```ini
DAILY_ENABLED=true
DAILY_GEN_COUNT=3        # 每站每天 3 篇 = 24 篇/天
PUBLISH_STATUS=draft     # 前两个月保持 draft，人工过一遍
```

然后 `docker compose up -d pipeline`。

### 改了 `.env` 之后怎么生效

先说最容易踩的：**`docker compose restart` 不会重新读 `.env`**。它只是把容器里的进程重启一遍，环境变量还是创建容器时那一份。改了 `.env` 必须用 `up -d`——它会重新解析文件、比对配置，只重建真正变了的容器。

| 你改了 | 执行 | 为什么 |
|---|---|---|
| `.env` 里任何值 | `docker compose up -d` | 只有 `up` 会重读 `.env` 并重建变更的容器 |
| `config/sites.yaml`（定位、受众、来源、互链） | `docker compose restart pipeline` | 这个文件是**挂载**进去的，不用重建镜像，但要在启动时重新渲染 |
| `sitefarm/*.py`（prompt、查重逻辑） | `docker compose up -d --build pipeline` | 代码是**打进镜像**的，不是挂载，改完不重建等于没改 |
| `Dockerfile` / `requirements.txt` | `docker compose up -d --build` | 同上，且要重新装依赖 |
| `WP_ADMIN_PASSWORD` | 改了也没用 | 只在首次安装时用一次，老站要 `wp user update` |

验证改动是否真的生效，别只看"容器起来了"：

```bash
docker compose config | grep -E "DOMAIN|SITE_SCHEME"    # .env 有没有被读到
docker compose ps                                        # STATUS 里看是不是刚重建
docker compose exec pipeline head -20 /app/runtime/sites.yaml   # 渲染后的实际配置
```

**`DOMAIN` / `SITE_SCHEME` 改了会怎样**：容器重建时会把每个站 `wp-config.php` 里的 `WP_HOME` / `WP_SITEURL` 重写成新地址（日志里出现 `[diy] address changed, rewriting wp-config -> ...`），同时更新数据库里的同名选项。没变则完全不碰，日志只有 `already installed, skipping`。

**`DB_PASSWORD` / `MYSQL_ROOT_PASSWORD` 绝对不要直接改**：数据卷已经初始化过了，MySQL 里的密码不会跟着 `.env` 变，改完所有站立刻 500。正确顺序是先改数据库、再改文件：

```bash
# 1. 用旧密码进数据库改密码
docker compose exec db mysql -uroot -p'旧root密码' -e \
  "ALTER USER 'wp'@'%' IDENTIFIED BY '新密码'; ALTER USER 'root'@'localhost' IDENTIFIED BY '新root密码';"

# 2. 再改 .env 里的两行
# 3. 重建
docker compose up -d
```

---

## 八、备份

```bash
# 数据库（八站全在一个 MySQL 里，一条命令搞定）
docker compose exec db sh -c 'exec mysqldump --all-databases -uroot -p"$MYSQL_ROOT_PASSWORD"' > backup-$(date +%F).sql

# 站点文件（上传的图片、插件、主题）
docker run --rm -v site-farm_wp_sites:/data -v "$(pwd)":/backup alpine tar czf /backup/sites-$(date +%F).tar.gz -C /data .
```

Windows 的 PowerShell 里把 `$(date +%F)` 换成 `%date:~0,4%%date:~5,2%%date:~8,2%`。

---

## 九、常见问题

**绑了域名，所有子域打开都是同一个站**
宝塔反代的「发送域名」没填 `$host`。填成 `127.0.0.1` 后，容器收到的 Host 头永远是 127.0.0.1，绕过了按子域选目录的逻辑，全部落到主域。

**域名打不开，但 `127.0.0.1:8080` 正常**
三种可能，按顺序查：DNS 没生效（`nslookup diy.yourdomain.com` 看返回 IP）、服务器防火墙没放行 80/443（阿里云/腾讯云还要单独看安全组）、域名没备案被运营商拦。

**502 Bad Gateway**
宝塔能收到请求但连不上容器。`docker compose ps` 看 web 是否 `Up`；`curl -I -H "Host: yourdomain.com" http://127.0.0.1:8080/` 看容器是否响应。容器在跑但 502，通常是宝塔的代理超时太短（默认 60 秒，装插件时容易超）。

**上了 HTTPS 后浏览器报「重定向次数过多」**
`.env` 里 `SITE_SCHEME` 还是 `http`。WordPress 会把 https 访客往 http 跳，反代又把他跳回来。改成 `https` 后 `docker compose up -d web pipeline` 重建配置。

**改动 `.env` 里的 `DOMAIN` 之后站点没变**
`wp-config.php` 里的 `WP_HOME` / `WP_SITEURL` 是首次安装时写死的，改 `.env` 只会影响新站。老站要改地址：`docker compose exec web wp option update home https://新域名 --allow-root --path=/var/www/sites/diy`（`siteurl` 同样一条）。或者干脆 `down -v` 重装。

**容器起来了但站点 500**
看 `docker compose logs web`。多半是 MySQL 还没就绪就被访问了，等 30 秒。如果持续报错，检查 `.env` 里 `DB_PASSWORD` 和 `MYSQL_ROOT_PASSWORD` 有没有混用。

**`publish` 报 401**
Application Password 没加载。检查：`docker compose exec pipeline env | grep WP_APP_PASSWORD`。
如果为空，说明 web 容器还没装完站点，等它跑完再重启 pipeline：`docker compose restart pipeline`。

**`publish` 报 403 / 404**
`.env` 里 `DOMAIN` 和 `config/sites.yaml` 里的域名对不上。流水线启动时会把配置里的 `example.com` 替换成 `DOMAIN`，但如果你手工改过配置里的域名就不生效了。统一改成 `example.com` 再让它自动替换。

**重启会不会重装站点、丢数据**
不会。entrypoint 判断 `wp-config.php` 存在就跳过，数据在 `wp_sites` 和 `db_data` 两个 volume 里。
除非你 `docker compose down -v`（`-v` 会删卷，慎用）。

**内存占用**
8 个站空闲时约 550–900 MB。建议宿主机至少 2 GB，4 GB 更稳。实测拆分见下一条。

**为什么是三个容器，不能合成一个吗**

9 个站装完、完全空闲时的实测（`docker stats --no-stream`）：

| 容器 | 镜像 | 空闲内存 | 职责 |
|---|---|---|---|
| `db` | `mysql:8.0` | 420 MB | 9 个库，全部文章、设置、用户 |
| `web` | 自建，807 MB | 121 MB | Apache + mod_php，跑 9 个 WordPress |
| `pipeline` | 自建，190 MB | **0.7 MB** | DeepSeek 生成 + 发布，空闲时 `sleep infinity` |

`pipeline` 合并进 `web` 省不下任何东西（它就占 0.7 MB），反而多两个麻烦：镜像要装 Python 依赖，体积 +200 MB，改一行代码就得重建这 1 GB 的镜像；以及 `docker compose up` 重建 web 时会顺手杀掉正在跑的生成任务（一次 `gen` 要几十秒到几分钟）。

`db` 独立是硬要求，不是洁癖：改一行 Apache 配置要连着重启数据库；PHP 进程 OOM 会拖死 MySQL；MySQL 自己的崩溃恢复也要独占进程。这三件事绑在一个容器里，任何一次小改动都变成全站停机。

真要合成一个容器，只能用 supervisord 同时管 `mysqld` + `apache2` + `python`，代价是：

- 任一进程挂了容器不退出，`restart: unless-stopped` 和 healthcheck 全部失效，故障变成静默的
- 三条日志混进一个流，排错只能 grep
- 没法单独重启或升级某一层
- 备份前必须停整个容器，才能保证 MySQL 数据一致
- 镜像 1.1 GB 起，重建一次几分钟

换来的只是 `docker ps` 少两行。不建议。

**不想看到 pipeline 这个容器**

日常不跑批的时候可以停掉，它只是个空壳：

```bash
docker compose stop pipeline     # docker ps 只剩 db 和 web
docker compose start pipeline    # 要用之前先启动，否则 exec 会报 not running
```

`db` 和 `web` 不能这么干，两个都是常驻服务。

**`docker ps` 里冒出来八、九个容器**

说明跑过多次 `up`，而且项目名不同（换了目录名、或者加了 `-p` 参数），每组都是独立的 db/web/pipeline 三件套。先看有哪些项目：

```bash
docker compose ls
docker ps --format '{{.Label "com.docker.compose.project"}}' | sort | uniq -c
```

确认是废弃的再清：`docker compose -p <项目名> down -v`。**`-v` 会删数据卷**，执行前想清楚哪个项目是真的在用的。

**想加第 9 个站**
改 `.env` 的 `SITES` 加一个 id，重启 web 容器。然后在 `config/sites.yaml` 里补一段配置，并在 `docker-compose.yml` 的 `extra_hosts` 里补一行域名映射。

**想删掉某个站**
删 `SITES` 里的 id、删配置段、`docker compose exec web rm -rf /var/www/sites/<id>`，数据库手动 drop。

---

## 十、只有一个子域能打开，其他打不开

这个现象**几乎从来不是容器的问题**。先在容器里排除一遍，再往外查。

### 第一步：30 秒确认容器是好的

```bash
docker compose exec web sh -c 'for h in example.com diy.example.com pet.example.com camp.example.com; do printf "%-28s " "$h"; curl -s -H "Host: $h" http://127.0.0.1/ | grep -o "<title>[^<]*" | head -1; done'
```

把 `example.com` 换成你的域名。正常输出是这样，**每个站的标题都不一样**：

```
example.com                  <title>Northline Hub
diy.example.com              <title>Northline Diy
pet.example.com              <title>Northline Pet
camp.example.com             <title>Northline Camp
```

如果这里 9 个站都正常（一定是正常的，`docker compose logs web | grep "sites ready"` 能看到 9 个目录），那问题 100% 在容器的外面：DNS、宝塔站点、或者证书。

### 第二步：跑自检脚本，一次定位

```bash
# Windows（Git Bash / PowerShell 都行）
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1 yourdomain.com

# Linux / 宝塔
chmod +x scripts/doctor.sh && ./scripts/doctor.sh yourdomain.com
```

它会逐个域名检查 DNS → 证书 → HTTP 状态 → **实际应答的站点标题**，最后给结论。必须跑到 200 才算通过。

> 这个脚本要在**宿主机**上跑，不能在容器里跑。容器的 `extra_hosts` 把 9 个域名都强行指到 web 容器，在容器里测永远是通的，等于没测。

### 第三步：对号入座

| 症状 | 原因 | 修法 |
|---|---|---|
| 浏览器报 `DNS_PROBE_FINISHED_NXDOMAIN`，只有 diy 能解析 | DNS 只加了 `@` 和 `diy`，没有 `*` 泛解析 | 加一条 A 记录，主机记录 `*`，值填服务器 IP |
| 报 `NET::ERR_CERT_COMMON_NAME_INVALID`，点“继续访问”能看到内容 | 证书只签了 `yourdomain.com` 和 `diy.yourdomain.com` 两个名字 | 重新申请，域名填 `yourdomain.com` 和 `*.yourdomain.com`，**验证方式选 DNS** |
| 能打开，但**每个子域显示的都是 diy 的内容** | 反代没传 `Host` 头，或宝塔只建了 diy 一个站点、其他子域落到默认站 | 建一个 `*.yourdomain.com` 站点；反代的“发送域名”改成 `$host` |
| 404 / 403 | 宝塔站点的域名列表里没有这个子域 | 站点 → 域名管理 → 补 `*.yourdomain.com` |
| 一直跳回 `http://` | `.env` 的 `SITE_SCHEME` 还是 `http` | 改成 `https`，`docker compose up -d web` |

### 最常见的那个坑：宝塔里只需要一个站点，不是九个

如果你的宝塔里有 `diy.yourdomain.com` 这一个站点、没有 `*.yourdomain.com`，那么其他子域的请求**没有站点匹配**，Apache 会把它交给默认站点处理——表现就是“pet 打开是 diy 的内容”，或者“只有 diy 正常”。

正确做法是**一个站点覆盖全部**：

```
站点域名：
    yourdomain.com
    *.yourdomain.com
```

站点根目录随便填（不会用到），PHP 版本选纯静态，反向代理指向 `http://127.0.0.1:8080`，发送域名 `$host`。这样以后加第 10 个站也不用动宝塔。

### 如果你的环境是 HTTP 还没上证书

先跳过证书那一栏，直接测路由是不是通的：

```bash
SCHEME=http PORT=8080 ./scripts/doctor.sh yourdomain.com
```

这时候 WordPress 会 301 跳回 80 端口（因为 `WP_HOME` 里没有端口号），**这种跳转说明路由是通的**，配好域名和证书就正常了。真正要警惕的是“所有域名跳到同一个地址”。
