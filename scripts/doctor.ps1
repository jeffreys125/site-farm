<#
    site-farm domain doctor (Windows)

    在跑宝塔 / Docker 的这台 Windows 机器上执行，不要在容器里跑——
    容器里有 extra_hosts 劫持 DNS，在容器里测永远是通的。

        powershell -ExecutionPolicy Bypass -File doctor.ps1 yourdomain.com

    每个域名依次检查：DNS -> TCP/TLS -> HTTP 状态 -> 实际应答的 WordPress。
    最后一列是关键，它告诉你反向代理有没有把 Host 头传进来。
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Domain,

    # 还没配证书时：-Scheme http -Port 8080，直接打容器端口测路由
    [string]$Scheme = 'https',
    [string]$Port   = ''
)

$ErrorActionPreference = 'Continue'

$Domain = $Domain -replace '^https?://', ''
$Domain = $Domain.Split('/')[0]
$Domain = $Domain.TrimEnd('.')

$OriginSuffix = ''
if ($Port -ne '') { $OriginSuffix = ":$Port" }

$subs = @('@', 'www', 'diy', 'woodworking', 'pet', 'camp', 'kitchen', 'auto', 'garden', 'homeoffice')

# curl.exe ships with Windows 10 1803+ ; fall back with a clear message
$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue)
if (-not $curl) {
    Write-Host "找不到 curl.exe。Windows 10 1809 以下需要先装 curl，或者把 Git 的 usr/bin 加进 PATH。" -ForegroundColor Red
    exit 1
}
$curl = $curl.Source

function Get-DnsIp {
    param([string]$Name)
    try {
        $addrs = [System.Net.Dns]::GetHostAddresses($Name) |
                 Where-Object { $_.AddressFamily -eq 'InterNetwork' }
        if ($addrs) { return $addrs[0].IPAddressToString }
    } catch { }
    return ''
}

function Get-PageTitle {
    param([string]$Url)
    try {
        $html = & $curl -skL --max-time 10 $Url 2>$null
        $joined = ($html -join ' ')
        if ($joined -match '<title>([^<]*)</title>') {
            $t = $matches[1].Trim()
            if ($t.Length -gt 34) { $t = $t.Substring(0, 31) + '...' }
            return $t
        }
    } catch { }
    return '-'
}

$fmt = "{0,-34} {1,-16} {2,-6} {3,-8} {4}"
$line = '-' * 100

Write-Host $line
Write-Host ($fmt -f '域名', '解析到', '状态', '证书', '返回的站点')
Write-Host $line

$bad = @()
$titles = @()
$redirects = @()

foreach ($sub in $subs) {
    if ($sub -eq '@') { $fqdn = $Domain } else { $fqdn = "$sub.$Domain" }
    $shown = $fqdn
    if ($shown.Length -gt 33) { $shown = $shown.Substring(0, 30) + '...' }

    $ip = Get-DnsIp $fqdn
    if (-not $ip) {
        Write-Host ($fmt -f $shown, '-', 'DNS x', '-', '域名解析不了')
        $bad += [pscustomobject]@{ Host = $fqdn; Kind = 'dns' }
        continue
    }

    # 先不忽略证书：证书问题不能被掩盖
    $url = "${Scheme}://${fqdn}${OriginSuffix}/"
    $code = & $curl -s -o NUL -w '%{http_code}' --max-time 10 $url 2>$null
    $rc = $LASTEXITCODE
    $tls = 'ok'

    if ($rc -ne 0 -or $code -eq '000') {
        switch ($rc) {
            60 { $tls = '证书 x' }
            51 { $tls = '证书 x' }
            58 { $tls = '证书 x' }
            59 { $tls = '证书 x' }
            77 { $tls = '证书 x' }
            6  { $tls = 'DNS x' }
            7  { $tls = '拒连' }
            28 { $tls = '超时' }
            default { $tls = "失败($rc)" }
        }
        $code = & $curl -sk -o NUL -w '%{http_code}' --max-time 10 $url 2>$null
    }

    $title = Get-PageTitle $url
    $titles += $title

    Write-Host ($fmt -f $shown, $ip, $code, $tls, $title)

    # 3xx 说明路由是通的（常见于 WP_HOME 还写着 http），单独提示，不算故障
    if ($tls -ne 'ok') {
        $bad += [pscustomobject]@{ Host = $fqdn; Kind = $tls }
    } elseif ($code -match '^3') {
        $redirects += $fqdn
    } elseif ($code -ne '200') {
        $bad += [pscustomobject]@{ Host = $fqdn; Kind = "HTTP $code" }
    }
}

Write-Host $line
Write-Host ("共 {0} 个域名，异常 {1} 个，重定向 {2} 个" -f $subs.Count, $bad.Count, $redirects.Count)
Write-Host ''

if ($bad.Count -eq 0 -and $redirects.Count -eq 0) {
    Write-Host '全部正常。' -ForegroundColor Green
    exit 0
}

$dnsFail  = @($bad | Where-Object { $_.Kind -like '*DNS*' }).Count
$tlsFail  = @($bad | Where-Object { $_.Kind -like '*证书*' }).Count
$connFail = @($bad | Where-Object { $_.Kind -match '拒连|超时|失败' }).Count
$httpFail = $bad.Count - $dnsFail - $tlsFail - $connFail

if ($dnsFail -gt 0) {
    Write-Host ">> 有 $dnsFail 个域名解析不出来" -ForegroundColor Yellow
    Write-Host "   DNS 里只加了 @ 和 diy，没有加 * 泛解析；或者加了还没生效（等 10 分钟）。"
    Write-Host "   验证：nslookup test.$Domain   应该返回服务器 IP"
}

if ($tlsFail -gt 0) {
    Write-Host ">> 有 $tlsFail 个域名证书不对 —— 这是「只有一个站能用」最常见的原因" -ForegroundColor Yellow
    Write-Host "   宝塔里签的是单域名证书（只含 $Domain 和 diy.$Domain），其他子域被浏览器直接拦截。"
    Write-Host "   修法：站点 -> SSL -> Let's Encrypt -> 域名填 $Domain 和 *.$Domain"
    Write-Host "         验证方式必须选 DNS（泛域名不支持文件验证），签完开「强制 HTTPS」。"
}

if ($connFail -gt 0) {
    Write-Host ">> 有 $connFail 个域名连不上端口" -ForegroundColor Yellow
    Write-Host "   宝塔没有匹配的站点，或者防火墙 / 安全组没放 443。"
}

if ($httpFail -gt 0) {
    Write-Host ">> 有 $httpFail 个域名通了但状态码不是 200" -ForegroundColor Yellow
    Write-Host "   看「返回的站点」那一列：如果全是同一个标题，说明反代没传 Host 头。"
    Write-Host "   宝塔 -> 站点 -> 反向代理 -> 编辑 -> 发送域名改成 `$host（不是 127.0.0.1）"
}

if ($redirects.Count -gt 0) {
    Write-Host ">> 有 $($redirects.Count) 个域名是 3xx 跳转，没到 200：" -ForegroundColor Yellow
    $redirects | Select-Object -First 4 | ForEach-Object { Write-Host "   $_" }
    if ($Scheme -eq 'https') {
        Write-Host "   多半是 WordPress 的 WP_HOME 还是 http。改 .env 的 SITE_SCHEME=https，"
        Write-Host "   再 docker compose up -d web，日志里会出现 address changed, rewriting wp-config"
    } else {
        Write-Host "   你正在用 -Scheme http -Port 测，WordPress 的 WP_HOME 不含端口，会跳回 80。"
        Write-Host "   这种跳转说明路由是通的，配好域名和证书就正常了。"
    }
    Write-Host ''
}

$distinct = ($titles | Sort-Object -Unique).Count
if ($titles.Count -ge 8 -and $distinct -le 2) {
    Write-Host ''
    Write-Host ">> 注意：几乎所有域名返回了同一个页面" -ForegroundColor Yellow
    Write-Host "   这不可能是 WordPress 的问题（容器内实测 9 个站标题各不相同），"
    Write-Host "   一定是反代丢了 Host 头，或者宝塔只建了 diy 一个站点、其他子域落到默认站。"
}
