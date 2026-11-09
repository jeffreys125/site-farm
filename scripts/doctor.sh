#!/usr/bin/env bash
# =============================================================================
#  site-farm domain doctor
#
#  Run this ON THE HOST (the machine running BaoTa / Docker), not inside a
#  container -- the container has extra_hosts entries that fake the DNS
#  answers, so a check from in there always passes.
#
#      chmod +x doctor.sh
#      ./doctor.sh yourdomain.com
#
#  Still on plain HTTP (no cert yet)? Point it at the container port:
#      SCHEME=http PORT=8080 ./doctor.sh yourdomain.com
#
#  For every site it reports: DNS -> TCP/TLS -> HTTP status -> which WordPress
#  actually answered. That last column is what tells you whether the reverse
#  proxy forwards the Host header.
# =============================================================================
set -uo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
    echo "usage: $0 <domain>    e.g. $0 example.com"
    echo "       SCHEME=http PORT=8080 $0 example.com    # before you have a cert"
    exit 1
fi

DOMAIN="${DOMAIN#http://}"; DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%%/*}"; DOMAIN="${DOMAIN%%:*}"

SCHEME="${SCHEME:-https}"
PORT="${PORT:-}"
ORIGIN_SUFFIX=""
[ -n "$PORT" ] && ORIGIN_SUFFIX=":$PORT"

SUBS=( "@" "www" "diy" "woodworking" "pet" "camp" "kitchen" "auto" "garden" "homeoffice" )

if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found -- install it first (apt install curl / yum install curl)"
    exit 1
fi
CURL=curl

# --- resolve without curl, so we can tell "DNS broken" from "server broken" --
resolve() {
    local host="$1" ip=""
    if command -v getent >/dev/null 2>&1; then
        ip=$(getent ahostsv4 "$host" 2>/dev/null | awk 'NR==1{print $1}')
    fi
    if [ -z "$ip" ] && command -v nslookup >/dev/null 2>&1; then
        ip=$(nslookup "$host" 2>/dev/null | awk '/^Address: /{print $2}' | tail -1)
    fi
    if [ -z "$ip" ] && command -v dig >/dev/null 2>&1; then
        ip=$(dig +short A "$host" 2>/dev/null | grep -E '^[0-9.]+$' | head -1)
    fi
    printf '%s' "$ip"
}

# --- the page title tells us which WordPress answered ----------------------
page_title() {
    local url="$1" html
    html=$($CURL -skL --max-time 10 "$url" 2>/dev/null | tr '\n' ' ')
    printf '%s' "$html" \
        | sed -n 's/.*<title>\([^<]*\)<\/title>.*/\1/p' \
        | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' \
        | cut -c1-32
}

fmt='%-30s %-15s %-7s %-7s %s\n'
sep() { printf '%s\n' "--------------------------------------------------------------------------------------------"; }

sep
printf "$fmt" "域名" "解析到" "HTTP" "证书" "实际应答的站点"
sep

declare -a bad=() redirects=() titles=()
first_title=""

for sub in "${SUBS[@]}"; do
    if [ "$sub" = "@" ]; then fqdn="$DOMAIN"; else fqdn="${sub}.${DOMAIN}"; fi
    shown="$fqdn"
    [ ${#shown} -gt 29 ] && shown="${shown:0:26}..."

    ip=$(resolve "$fqdn")
    if [ -z "$ip" ]; then
        printf "$fmt" "$shown" "-" "DNS x" "-" "域名解析不了"
        bad+=("$fqdn|dns")
        continue
    fi

    url="${SCHEME}://${fqdn}${ORIGIN_SUFFIX}/"

    # 先不忽略证书：TLS 问题不能被静默掩盖
    code=$($CURL -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)
    rc=$?
    tls="ok"
    if [ "$rc" -ne 0 ] || [ "$code" = "000" ]; then
        case "$rc" in
            60|51|58|59|77) tls="证书 x" ;;
            6)              tls="DNS x" ;;
            7)              tls="拒连" ;;
            28)             tls="超时" ;;
            *)              tls="失败" ;;
        esac
        # 忽略证书再试一次，看看应用层到底有没有响应
        code=$($CURL -sk -o /dev/null -w '%{http_code}' --max-time 10 "$url" 2>/dev/null)
    fi

    title=$(page_title "$url")

    # 3xx：跟一下 Location，判断是“跳去 https”还是“跳去别的站”
    loc=""
    case "$code" in
        301|302|303|307|308)
            loc=$($CURL -sk -o /dev/null -w '%{redirect_url}' --max-time 10 "$url" 2>/dev/null)
            [ -n "$loc" ] && title="-> ${loc}"
            ;;
    esac

    [ -z "$title" ] && title="-"
    [ ${#title} -gt 40 ] && title="${title:0:37}..."

    printf "$fmt" "$shown" "$ip" "$code" "$tls" "$title"

    [ -n "$first_title" ] || first_title="$title"
    titles+=("$title")

    if [ "$tls" != "ok" ]; then
        bad+=("$fqdn|$tls")
    else
        case "$code" in
            200) ;;
            301|302|303|307|308) redirects+=("$fqdn -> $loc") ;;
            *) bad+=("$fqdn|HTTP $code") ;;
        esac
    fi
done

sep
printf '共 %d 个域名，异常 %d 个，重定向 %d 个\n' "${#SUBS[@]}" "${#bad[@]}" "${#redirects[@]}"

# --- verdict ---------------------------------------------------------------
if [ ${#bad[@]} -eq 0 ] && [ ${#redirects[@]} -eq 0 ]; then
    echo
    echo "全部正常。"
    exit 0
fi

echo
dns_fail=0; tls_fail=0; conn_fail=0; http_fail=0
for item in "${bad[@]}"; do
    case "${item#*|}" in
        dns)            dns_fail=$((dns_fail+1)) ;;
        证书*)          tls_fail=$((tls_fail+1)) ;;
        拒连|超时|失败) conn_fail=$((conn_fail+1)) ;;
        *)              http_fail=$((http_fail+1)) ;;
    esac
done

if [ "$dns_fail" -gt 0 ]; then
    echo ">> 有 ${dns_fail} 个域名解析不出来"
    echo "   DNS 里只加了 @ 和 diy，没有加 * 泛解析；或者加了还没生效（等 10 分钟）。"
    echo "   验证：nslookup test.${DOMAIN}   应该返回服务器 IP"
    echo
fi

if [ "$tls_fail" -gt 0 ]; then
    echo ">> 有 ${tls_fail} 个域名证书不对 —— 这是「只有一个站能用」最常见的原因"
    echo "   宝塔里签的是单域名证书（只含 ${DOMAIN} 和 diy.${DOMAIN}），其他子域被浏览器直接拦截。"
    echo "   修法：站点 -> SSL -> Let's Encrypt -> 域名填 ${DOMAIN} 和 *.${DOMAIN}"
    echo "         验证方式必须选 DNS（泛域名不支持文件验证），签完开「强制 HTTPS」。"
    echo
fi

if [ "$conn_fail" -gt 0 ]; then
    echo ">> 有 ${conn_fail} 个域名连不上端口"
    echo "   宝塔没有匹配的站点，或者防火墙 / 安全组没放 443。"
    echo
fi

if [ "$http_fail" -gt 0 ]; then
    echo ">> 有 ${http_fail} 个域名通了但状态码不是 200"
    echo "   看「实际应答的站点」那一列：如果全是同一个标题，说明反代没传 Host 头。"
    echo "   宝塔 -> 站点 -> 反向代理 -> 编辑 -> 发送域名改成 \$host（不是 127.0.0.1）"
    echo
fi

if [ ${#redirects[@]} -gt 0 ]; then
    echo ">> 有 ${#redirects[@]} 个域名是 3xx 跳转，没到 200"
    printf '   %s\n' "${redirects[@]}" | head -4
    if [ "$SCHEME" = "https" ]; then
        echo "   如果 Location 指向 http://，说明 WordPress 的 WP_HOME 还是 http。"
        echo "   把 .env 的 SITE_SCHEME 改成 https 再 docker compose up -d web，"
        echo "   日志里会出现 [xxx] address changed, rewriting wp-config"
    else
        echo "   你现在是用 SCHEME=http PORT=... 在测。WordPress 的 WP_HOME 不含端口，"
        echo "   所以会跳回 80 端口。这种跳转说明路由是通的，配好域名和证书后就会正常。"
    fi
    echo
fi

distinct=$(printf '%s\n' "${titles[@]}" | sort -u | wc -l)
if [ "${#titles[@]}" -ge 8 ] && [ "$distinct" -le 2 ]; then
    echo ">> 注意：几乎所有域名返回了同一个页面"
    echo "   这不可能是 WordPress 的问题（容器内实测 9 个站标题各不相同），"
    echo "   一定是反代丢了 Host 头，或者宝塔只建了 diy 一个站点、其他子域落到默认站。"
fi
