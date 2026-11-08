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

上线前在域名商处加两条记录：

| 类型 | 主机记录 | 值 |
|---|---|---|
| A | `@` | 服务器公网 IP |
| A | `*` | 服务器公网 IP |

`*` 泛解析覆盖全部子域，以后加新站不用再动 DNS。

---

## 六、HTTPS（三选一）

### 方案 A：Caddy 反代（推荐，全自动证书）

```bash
docker run -d --name caddy --restart unless-stopped \
  --network site-farm_farm \
  -p 80:80 -p 443:443 \
  -v caddy_data:/data -v caddy_config:/config \
  caddy:2
```

写一个 Caddyfile（8 个子域要显式列出，泛域名证书需要 DNS 插件）：

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

再 `docker compose up -d web pipeline` 重建配置。否则 WordPress 会一直跳回 http，产生重定向循环。

### 方案 B：宝塔反向代理

1. 宝塔新建站点 `diy.yourdomain.com`，根目录随便填（不会用到）
2. 站点设置 → 反向代理 → 目标 `http://127.0.0.1:8080`，发送域名填 `$host`
3. 申请 Let's Encrypt 证书（每个子域单独申请，或者用 DNS 方式申请泛域名）
4. 八个站重复八次

宝塔的反代默认会传 `X-Forwarded-Proto`，容器里的 wp-config 已经处理了这个头，`is_ssl()` 能正确返回 true。

### 方案 C：Cloudflare（最省事）

1. 域名 NS 托管到 Cloudflare
2. A 记录开橙云，指向服务器 IP，同样加 `*` 泛解析
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
8 个站空闲时约 600–900 MB（Apache + mod_php + MySQL）。建议宿主机至少 2 GB，4 GB 更稳。

**想加第 9 个站**
改 `.env` 的 `SITES` 加一个 id，重启 web 容器。然后在 `config/sites.yaml` 里补一段配置，并在 `docker-compose.yml` 的 `extra_hosts` 里补一行域名映射。

**想删掉某个站**
删 `SITES` 里的 id、删配置段、`docker compose exec web rm -rf /var/www/sites/<id>`，数据库手动 drop。
