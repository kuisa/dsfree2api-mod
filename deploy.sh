#!/usr/bin/env bash
# ============================================================
#  dsfree2api + 自建 Turnstile 求解器  ·  一键部署脚本
#
#  在干净的 Debian/Ubuntu VPS 上执行：
#      bash deploy.sh
#
#  非交互（全自动，适合批量）：
#      API_KEY=xxx ADMIN_PASSWORD=yyy bash deploy.sh -y
#
#  只检查不安装：
#      bash deploy.sh --dry-run
#
#  做什么：
#    1. 装依赖（Docker / Xvfb / Chrome / seleniumbase）
#    2. 部署 Turnstile 求解器（systemd 托管 + 看门狗）
#    3. 部署 dsfree2api（打增强补丁：轮询 / 双基线预热 / role 修复）
#    4. 自动探测 Docker 网桥网关并接线
#    5. 端到端验证
# ============================================================
set -euo pipefail

VERSION="1.0.0"

# ── 可覆盖的配置 ──────────────────────────────────────────────
# 下游调用 API 用的 key（留空 = 自动生成）
API_KEY="${API_KEY:-}"
# 管理台密码（留空 = 自动生成）
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
# 求解器自身的 API key（留空 = 自动生成）
SOLVER_KEY="${SOLVER_KEY:-}"

SITEKEY="${SITEKEY:-0x4AAAAAADlLZ3ljqZP6cQwq}"
ACTION="${ACTION:-chat}"
MAIN_URL="${MAIN_URL:-https://deepseek.de}"

# 安装位置
BASE_DIR="${BASE_DIR:-/opt/turnstile-stack}"
DSFREE_DIR="$BASE_DIR/dsfree2api"
SOLVER_DIR="$BASE_DIR/solver"

# dsfree2api 源码 + 补丁对应的 commit（补丁必须打在同一个版本上）
DSFREE_REPO="${DSFREE_REPO:-https://github.com/nyoungo/dsfree2api.git}"
DSFREE_COMMIT="${DSFREE_COMMIT:-e44e960fe91baf01cfc578d62dce4185eacb6f2b}"

# 本仓库的 raw 地址（脚本不是从仓库里跑时需要）
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/kuisa/dsfree2api-mod/main}"

# 预热参数
PREWARM_MIN_REMAINING="${PREWARM_MIN_REMAINING:-10000}"
PREWARM_THRESHOLD="${PREWARM_THRESHOLD:-0.20}"
PREWARM_REFRESH_BEFORE_MINUTES="${PREWARM_REFRESH_BEFORE_MINUTES:-30}"
PREWARM_INTERVAL_SECONDS="${PREWARM_INTERVAL_SECONDS:-60}"

ASSUME_YES=0
DRY_RUN=0

# ── 输出 ──────────────────────────────────────────────────────
c_reset=$'\033[0m'; c_red=$'\033[31m'; c_grn=$'\033[32m'
c_yel=$'\033[33m'; c_blu=$'\033[36m'; c_bold=$'\033[1m'

step()  { printf '\n%s%s══ %s%s\n' "$c_bold" "$c_blu" "$*" "$c_reset"; }
ok()    { printf '  %s✓%s %s\n' "$c_grn" "$c_reset" "$*"; }
warn()  { printf '  %s!%s %s\n' "$c_yel" "$c_reset" "$*"; }
fail()  { printf '  %s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; }
die()   { fail "$*"; exit 1; }

# 需要 root
[ "$(id -u)" -eq 0 ] || die "请用 root 运行（或 sudo bash deploy.sh）"

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo /tmp)"

# ── 辅助函数 ──────────────────────────────────────────────────
# 从本地仓库目录取文件；不在仓库里就从 RAW_BASE 下载
fetch_file() {
  local rel="$1" dest="$2"
  if [ -f "$SELF_DIR/$rel" ]; then
    install -m 0644 "$SELF_DIR/$rel" "$dest"
  else
    curl -fsSL "$RAW_BASE/$rel" -o "$dest" \
      || die "下载失败: $RAW_BASE/$rel（可用 RAW_BASE=... 指定）"
  fi
}

rand_hex() { openssl rand -hex "${1:-16}"; }

# 命令是否存在
have() { command -v "$1" >/dev/null 2>&1; }

# 是否已有某个 systemd 单元
unit_exists() { systemctl list-unit-files 2>/dev/null | grep -q "^${1}"; }

# 找一个空闲端口
free_port() {
  local p=8899
  while ss -ltn 2>/dev/null | grep -q ":$p "; do p=$((p+1)); done
  echo "$p"
}

# 探测某个 compose 项目的网桥网关（容器里看到的"宿主"地址）
detect_gateway() {
  local cname="$1"
  docker inspect "$cname" \
    --format '{{range $k,$v := .NetworkSettings.Networks}}{{$v.Gateway}}{{end}}' 2>/dev/null
}

# ── 参数解析 ──────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)     ASSUME_YES=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "未知参数: $1（-h 看帮助）" ;;
  esac
  shift
done

printf '%s\n' "${c_bold}dsfree2api + Turnstile 求解器 一键部署 v$VERSION${c_reset}"
[ "$DRY_RUN" -eq 1 ] && warn "DRY-RUN 模式：只检查，不安装"

# ── 1. 前置检查 ───────────────────────────────────────────────
step "1/6  前置检查"

. /etc/os-release 2>/dev/null || true
ok "系统: ${PRETTY_NAME:-未知}"
ok "架构: $(uname -m)"
[ "$DRY_RUN" -eq 1 ] || ok "安装目录: $BASE_DIR"

if ! have curl || ! have openssl; then
  warn "缺 curl/openssl，先装"
  [ "$DRY_RUN" -eq 1 ] || { apt-get update -qq && apt-get install -y -qq curl openssl ca-certificates; }
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) IS_ARM=0 ;;
  aarch64|arm64) IS_ARM=1; warn "ARM 架构：Chrome 不可用，将使用 Chromium" ;;
  *) die "不支持的架构: $ARCH" ;;
esac

# ── 2. 安装依赖 ───────────────────────────────────────────────
step "2/6  安装依赖"

if [ "$DRY_RUN" -eq 1 ]; then
  ok "[dry-run] 跳过安装"
else
  export DEBIAN_FRONTEND=noninteractive

  # 2.1 Docker
  if have docker && docker compose version >/dev/null 2>&1; then
    ok "Docker 已装: $(docker --version | cut -d, -f1)"
  else
    warn "安装 Docker ..."
    curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || die "Docker 安装失败"
    systemctl enable --now docker >/dev/null 2>&1 || true
    ok "Docker: $(docker --version | cut -d, -f1)"
  fi

  # 2.2 Xvfb + GUI 工具
  PKGS=""
  for p in xvfb xdotool scrot x11-utils fonts-noto-cjk git; do
    dpkg -s "$p" >/dev/null 2>&1 || PKGS="$PKGS $p"
  done
  if [ -n "$PKGS" ]; then
    warn "安装:$PKGS"
    apt-get update -qq
    apt-get install -y -qq $PKGS || die "apt 安装失败"
  fi
  ok "Xvfb / xdotool / scrot 就绪"

  # 2.3 浏览器
  if [ "$IS_ARM" -eq 0 ]; then
    if have google-chrome-stable; then
      ok "Chrome 已装: $(google-chrome-stable --version)"
    else
      warn "安装 Google Chrome ..."
      TMPD=$(mktemp -d)
      wget -q -O "$TMPD/chrome.deb" \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
        || die "Chrome 下载失败"
      apt-get install -y -qq "$TMPD/chrome.deb" || die "Chrome 安装失败"
      rm -rf "$TMPD"
      ok "Chrome: $(google-chrome-stable --version)"
    fi
  else
    if ! have chromium && ! have chromium-browser; then
      warn "安装 Chromium ..."
      apt-get install -y -qq chromium || apt-get install -y -qq chromium-browser || die "Chromium 安装失败"
    fi
    # SeleniumBase 只探测 google-chrome 系列路径，做个软链让它找到
    CHROME_BIN="$(command -v chromium || command -v chromium-browser)"
    [ -e /usr/bin/google-chrome ] || ln -sf "$CHROME_BIN" /usr/bin/google-chrome
    ok "Chromium: $CHROME_BIN（已软链到 /usr/bin/google-chrome）"
  fi

  # 2.4 seleniumbase
  if python3 -c "import seleniumbase" >/dev/null 2>&1; then
    ok "seleniumbase 已装: $(python3 -c 'import seleniumbase;print(seleniumbase.__version__)')"
  else
    warn "安装 seleniumbase ..."
    python3 -m pip install --break-system-packages -q -U seleniumbase \
      || die "seleniumbase 安装失败"
    ok "seleniumbase: $(python3 -c 'import seleniumbase;print(seleniumbase.__version__)')"
  fi
fi

# ── 3. 部署求解器 ─────────────────────────────────────────────
step "3/6  部署 Turnstile 求解器"

if [ "$DRY_RUN" -eq 1 ]; then
  ok "[dry-run] 跳过"
else
  mkdir -p "$SOLVER_DIR"
  fetch_file "solver/turnstile_server.py" "$SOLVER_DIR/turnstile_server.py"
  chmod 0644 "$SOLVER_DIR/turnstile_server.py"
  ok "求解器: $SOLVER_DIR/turnstile_server.py"

  # 3.1 生成 key
  KEYFILE=/root/.turnstile_solver_key
  if [ -s "$KEYFILE" ]; then
    ok "沿用已有求解器 key"
  else
    [ -n "$SOLVER_KEY" ] || SOLVER_KEY="$(rand_hex 16)"
    printf '%s\n' "$SOLVER_KEY" > "$KEYFILE"
    chmod 600 "$KEYFILE"
    ok "已生成求解器 key"
  fi
  SOLVER_KEY="$(cat "$KEYFILE")"
  printf 'API_KEY=%s\n' "$SOLVER_KEY" > /root/.turnstile_solver_key.env
  chmod 600 /root/.turnstile_solver_key.env

  SOLVER_PORT="$(free_port)"
  ok "求解器端口: $SOLVER_PORT"

  # 3.2 Xvfb —— 先查有没有现成的，别重复建（重复建会抢不到 :1 而崩溃重启）
  XVFB_UNIT=""
  EXISTING_XVFB="$(systemctl list-units --all --no-pager 2>/dev/null \
      | awk '/xvfb/ && /running/ {print $1; exit}')"
  if [ -n "$EXISTING_XVFB" ]; then
    XVFB_UNIT="$EXISTING_XVFB"
    ok "复用已有 Xvfb 单元: $XVFB_UNIT（不新建）"
  elif ps aux | grep -q "[X]vfb :1"; then
    ok "检测到裸跑的 Xvfb :1（非 systemd 托管），继续复用"
  else
    warn "没有 Xvfb，创建 xvfb.service"
    cat > /etc/systemd/system/xvfb.service <<'EOF'
[Unit]
Description=Xvfb Virtual Display :1
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :1 -screen 0 1440x900x24 -ac +extension GLX +render -noreset
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    XVFB_UNIT="xvfb.service"
    systemctl daemon-reload
    systemctl enable --now xvfb.service >/dev/null 2>&1 || true
    sleep 2
    if systemctl is-active --quiet xvfb.service; then
      ok "xvfb.service 已启动"
    else
      warn "xvfb.service 未起来，看 journalctl -u xvfb.service"
    fi
  fi

  # 3.3 求解器 systemd 单元
  cat > /etc/systemd/system/turnstile-solver.service <<EOF
[Unit]
Description=Turnstile Solver Service
After=network.target

[Service]
Type=simple
Environment=DISPLAY=:1
Environment=LISTEN_HOST=0.0.0.0
Environment=LISTEN_PORT=$SOLVER_PORT
Environment=MAIN_URL=$MAIN_URL
EnvironmentFile=-/root/.turnstile_solver_key.env
ExecStart=/usr/bin/python3 -u $SOLVER_DIR/turnstile_server.py
Restart=always
RestartSec=5
# Chrome 峰值约 1.7G。宁可被 OOM 杀掉自动重起，也不要卡在 swap 里假死
MemoryHigh=2G
MemoryMax=3G
MemorySwapMax=0
StandardOutput=append:/root/turnstile_server.log
StandardError=append:/root/turnstile_server.log

[Install]
WantedBy=multi-user.target
EOF
  ok "turnstile-solver.service 已写入"

  # 3.4 看门狗（兜住"进程活着但卡死"）
  fetch_file "solver/healthcheck.sh" /root/turnstile_healthcheck.sh
  chmod +x /root/turnstile_healthcheck.sh
  sed -i "s|^URL=.*|URL=\"http://127.0.0.1:${SOLVER_PORT}/health\"|" /root/turnstile_healthcheck.sh

  cat > /etc/systemd/system/turnstile-healthcheck.service <<'EOF'
[Unit]
Description=Turnstile solver healthcheck

[Service]
Type=oneshot
ExecStart=/root/turnstile_healthcheck.sh
EOF

  cat > /etc/systemd/system/turnstile-healthcheck.timer <<'EOF'
[Unit]
Description=Run turnstile solver healthcheck periodically

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=30s
Unit=turnstile-healthcheck.service

[Install]
WantedBy=timers.target
EOF
  ok "看门狗单元已写入（每 2 分钟）"

  systemctl daemon-reload
  pkill -f "turnstile_server.py" 2>/dev/null || true
  sleep 1
  systemctl enable --now turnstile-solver.service >/dev/null 2>&1
  systemctl enable --now turnstile-healthcheck.timer >/dev/null 2>&1
  sleep 4

  if systemctl is-active --quiet turnstile-solver.service; then
    ok "求解器已启动 (pid $(systemctl show turnstile-solver.service -p MainPID --value))"
  else
    die "求解器启动失败，看 journalctl -u turnstile-solver -n 50"
  fi

  if curl -sf --max-time 8 "http://127.0.0.1:${SOLVER_PORT}/health" >/dev/null; then
    ok "健康检查通过"
  else
    warn "health 无响应，稍后可能自愈；否则看 /root/turnstile_server.log"
  fi

  # 3.5 自检脚本（把端口烧进去，省得每次带参数）
  mkdir -p "$BASE_DIR/scripts"
  fetch_file "scripts/check.sh" "$BASE_DIR/scripts/check.sh"
  chmod +x "$BASE_DIR/scripts/check.sh"
  sed -i "s|^SOLVER_PORT=.*|SOLVER_PORT=\"\${SOLVER_PORT:-$SOLVER_PORT}\"|" \
    "$BASE_DIR/scripts/check.sh"
  ok "自检脚本: $BASE_DIR/scripts/check.sh"
fi

# ── 4. 部署 dsfree2api ─────────────────────────────────────────
step "4/6  部署 dsfree2api（含增强补丁）"

if [ "$DRY_RUN" -eq 1 ]; then
  ok "[dry-run] 跳过"
else
  mkdir -p "$BASE_DIR"

  # 4.1 拉源码
  if [ -d "$DSFREE_DIR/.git" ]; then
    ok "已有源码，拉取更新"
    git -C "$DSFREE_DIR" fetch --all -q || true
  else
    warn "克隆 $DSFREE_REPO ..."
    git clone -q "$DSFREE_REPO" "$DSFREE_DIR" || die "克隆失败"
  fi

  # 4.2 切到补丁对应的 commit（补丁不能跨版本打）
  CURRENT="$(git -C "$DSFREE_DIR" rev-parse HEAD)"
  if [ "$CURRENT" != "$DSFREE_COMMIT" ]; then
    warn "切换到 $DSFREE_COMMIT"
    git -C "$DSFREE_DIR" checkout -q "$DSFREE_COMMIT" 2>/dev/null \
      || die "checkout $DSFREE_COMMIT 失败（补丁必须打在这个版本上）"
  fi
  ok "源码版本: $(git -C "$DSFREE_DIR" log -1 --format='%h %s')"

  # 4.3 打补丁
  fetch_file "patches/dsfree2api-prewarm.patch" /tmp/dsfree2api-prewarm.patch
  if git -C "$DSFREE_DIR" apply --check /tmp/dsfree2api-prewarm.patch 2>/dev/null; then
    git -C "$DSFREE_DIR" apply /tmp/dsfree2api-prewarm.patch
    ok "补丁已应用（轮询 / 双基线预热 / role 修复）"
  elif git -C "$DSFREE_DIR" diff --quiet 2>/dev/null && \
       grep -q "StartPrewarm" "$DSFREE_DIR/cmd/dsfree2api/main.go" 2>/dev/null; then
    ok "补丁已在（跳过）"
  else
    # 可能已经打过；再试一次反向检测
    if git -C "$DSFREE_DIR" apply --reverse --check /tmp/dsfree2api-prewarm.patch 2>/dev/null; then
      ok "补丁已在（跳过）"
    else
      die "补丁应用失败。当前源码版本 $(git -C "$DSFREE_DIR" rev-parse --short HEAD)，
     补丁是针对 $DSFREE_COMMIT 生成的。上游改过代码了 → 把 DSFREE_COMMIT 换成
     补丁对应的版本，或重新生成补丁（见 README「补丁会随上游漂移」一节）。"
    fi
  fi
fi

if [ "$DRY_RUN" -eq 0 ]; then
  # 4.4 生成密钥
  [ -n "$API_KEY" ]        || API_KEY="sk-$(rand_hex 16)"
  [ -n "$ADMIN_PASSWORD" ] || ADMIN_PASSWORD="$(rand_hex 12)"
  ok "下游 API Key : $API_KEY"
  ok "管理台密码   : $ADMIN_PASSWORD"

  cd "$DSFREE_DIR"

  # 4.5 写 .env（compose 的环境变量优先级高于 config.toml）
  cat > .env <<EOF
ADMIN_PASSWORD=$ADMIN_PASSWORD
API_KEYS=$API_KEY
TURNSTILE_API_KEY=$SOLVER_KEY
PREWARM_ENABLED=true
PREWARM_INTERVAL_SECONDS=$PREWARM_INTERVAL_SECONDS
PREWARM_MIN_REMAINING=$PREWARM_MIN_REMAINING
PREWARM_THRESHOLD=$PREWARM_THRESHOLD
PREWARM_REFRESH_BEFORE_MINUTES=$PREWARM_REFRESH_BEFORE_MINUTES
PREWARM_START_DELAY_SECONDS=15
EOF
  chmod 600 .env
  ok ".env 已写入"

  # 4.6 生成 config.toml（先填占位网关，起来后再改成真实值）
  if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    ok "config.toml 已从示例生成"
  else
    ok "沿用已有 config.toml"
  fi

  python3 - "$DSFREE_DIR/config.toml" "127.0.0.1" "$SOLVER_PORT" \
           "$SOLVER_KEY" "$API_KEY" "$ADMIN_PASSWORD" "$SITEKEY" "$ACTION" <<'PYEOF'
import re, sys
path, gw, port, skey, apikey, adminpw, sitekey, action = sys.argv[1:9]
s = open(path, encoding='utf-8').read()

def set_in_section(text, section, key, literal):
    lines, out, in_sec, done = text.split('\n'), [], False, False
    for ln in lines:
        st = ln.strip()
        if st.startswith('[') and st.endswith(']'):
            in_sec = (st == '[%s]' % section)
        if in_sec and not done and re.match(r'^#?\s*%s\s*=' % re.escape(key), st):
            out.append('%s = %s' % (key, literal)); done = True; continue
        out.append(ln)
    if done:
        return '\n'.join(out)
    res, in_sec = [], False
    for ln in out:
        st = ln.strip()
        if st.startswith('[') and st.endswith(']'):
            if in_sec: res.append('%s = %s' % (key, literal))
            in_sec = (st == '[%s]' % section)
        res.append(ln)
    if in_sec: res.append('%s = %s' % (key, literal))
    return '\n'.join(res)

s = set_in_section(s, 'security',  'api_keys',  '["%s"]' % apikey)
s = set_in_section(s, 'admin',     'password',  '"%s"' % adminpw)
s = set_in_section(s, 'turnstile', 'enabled',   'true')
s = set_in_section(s, 'turnstile', 'provider',  '"api"')
s = set_in_section(s, 'turnstile', 'api_url',   '"http://%s:%s/turnstile/sync"' % (gw, port))
s = set_in_section(s, 'turnstile', 'api_key',   '"%s"' % skey)
s = set_in_section(s, 'turnstile', 'sitekey',   '"%s"' % sitekey)
s = set_in_section(s, 'turnstile', 'action',    '"%s"' % action)
open(path, 'w', encoding='utf-8').write(s)
PYEOF
  ok "config.toml 已配置"

  # 4.7 编译
  warn "编译镜像（约 1-2 分钟）..."
  docker compose build >/tmp/dsfree-build.log 2>&1 \
    || { tail -30 /tmp/dsfree-build.log; die "编译失败"; }
  ok "编译完成"

  # 4.8 首次启动 —— 为了创建 docker 网络
  docker compose up -d >/dev/null 2>&1 || true
  sleep 6

  # 4.9 探测网桥网关（容器里看到的"宿主"地址）
  CONTAINER="$(docker compose ps -q 2>/dev/null | head -1)"
  [ -n "$CONTAINER" ] || die "容器没起来，看 docker compose logs"
  CNAME="$(docker inspect "$CONTAINER" --format '{{.Name}}' | sed 's|^/||')"
  GW="$(detect_gateway "$CNAME")"
  [ -n "$GW" ] || die "探测不到网桥网关"
  ok "容器: $CNAME   网桥网关: $GW"

  # 4.10 把真实网关写进 config.toml，并修权限（否则管理台存不了配置）
  python3 - "$DSFREE_DIR/config.toml" "$GW" "$SOLVER_PORT" <<'PYEOF'
import re, sys
path, gw, port = sys.argv[1:4]
s = open(path, encoding='utf-8').read()
s = re.sub(r'^api_url\s*=\s*".*"$',
           'api_url = "http://%s:%s/turnstile/sync"' % (gw, port),
           s, count=1, flags=re.M)
open(path, 'w', encoding='utf-8').write(s)
PYEOF
  chown 10001:10001 config.toml 2>/dev/null || true
  ok "api_url 已指向 http://$GW:$SOLVER_PORT/turnstile/sync"

  # 4.11 重建容器让配置生效（restart 不重新解析 bind mount）
  docker compose up -d --force-recreate >/dev/null 2>&1
  sleep 8
  ok "容器已重建"

  # 4.12 容器 → 宿主 连通性
  if docker exec "$CNAME" wget -qO- --timeout=6 \
       "http://$GW:$SOLVER_PORT/health" >/dev/null 2>&1; then
    ok "容器 → 求解器 连通"
  else
    warn "容器连不上求解器，检查防火墙/网关"
  fi

  # 4.13 联网搜索代理（给不支持联网的模型加上搜索能力）
  PROXY_PORT="${SEARCH_PROXY_PORT:-8002}"
  fetch_file "solver/search_proxy.py" "$SOLVER_DIR/search_proxy.py"
  chmod 0644 "$SOLVER_DIR/search_proxy.py"

  cat > /etc/systemd/system/search-proxy.service <<EOF
[Unit]
Description=Web Search Proxy for dsfree2api
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
Environment=LISTEN_HOST=0.0.0.0
Environment=LISTEN_PORT=$PROXY_PORT
Environment=UPSTREAM_URL=http://127.0.0.1:8000
Environment=API_KEY=$API_KEY
Environment=AUTO_INJECT_TOOL=true
Environment=MULTI_HOP=false
# 搜索后端顺序（逗号分隔，靠前的先用）。可选：
#   bing / duckduckgo / wikipedia  ← 免费，无需 key
#   tavily / brave / serper        ← 需在下面填对应 API Key
Environment=SEARCH_BACKENDS=bing,duckduckgo,wikipedia
# 付费后端（可选，填了就自动优先用，质量更稳）：
#Environment=TAVILY_API_KEY=
#Environment=BRAVE_API_KEY=
#Environment=SERPER_API_KEY=
ExecStart=/usr/bin/python3 -u $SOLVER_DIR/search_proxy.py
Restart=always
RestartSec=5
StandardOutput=append:/root/search_proxy.log
StandardError=append:/root/search_proxy.log

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now search-proxy.service >/dev/null 2>&1
  sleep 3
  if curl -sf --max-time 6 "http://127.0.0.1:$PROXY_PORT/health" >/dev/null; then
    ok "搜索代理已启动: http://<IP>:$PROXY_PORT/v1"
  else
    warn "搜索代理未响应，看 journalctl -u search-proxy -n 50"
  fi
fi

# ── 5. 端到端验证 ─────────────────────────────────────────────
step "5/6  端到端验证"

if [ "$DRY_RUN" -eq 1 ]; then
  ok "[dry-run] 跳过"
else
  cd "$DSFREE_DIR"

  # 5.1 求解器
  if curl -sf --max-time 8 "http://127.0.0.1:$SOLVER_PORT/health" >/dev/null; then
    ok "求解器 health 正常"
  else
    fail "求解器无响应"
  fi

  # 5.2 管理台读配置
  curl -s -c /tmp/_dep_c.txt -X POST "http://127.0.0.1:8001/api/login" \
    -H 'Content-Type: application/json' \
    -d "{\"password\":\"$ADMIN_PASSWORD\"}" -o /dev/null 2>/dev/null || true
  CFG_JSON="$(curl -s -b /tmp/_dep_c.txt http://127.0.0.1:8001/api/config 2>/dev/null || echo '{}')"
  if printf '%s' "$CFG_JSON" | grep -q '"enabled":true'; then
    ok "管理台配置已生效（turnstile enabled）"
  else
    warn "管理台配置未确认，可手动打开 http://<IP>:8001 检查"
  fi

  # 5.3 真发一次对话（会触发 Turnstile 求解，首次约 25-30 秒）
  warn "发一次测试请求（首次要跑求解，约 30 秒）..."
  : > /root/turnstile_server.log 2>/dev/null || true
  RESP="$(curl -s --max-time 180 -X POST http://127.0.0.1:8000/v1/chat/completions \
      -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
      -d '{"model":"deepseek-v4-flash-de","messages":[{"role":"user","content":"say ok"}],"max_tokens":10}' \
      2>/dev/null || echo '{}')"

  if printf '%s' "$RESP" | grep -q '"content"'; then
    ok "对话成功: $(printf '%s' "$RESP" | python3 -c \
        'import json,sys;print(repr(json.load(sys.stdin)["choices"][0]["message"].get("content"))[:60])' 2>/dev/null || echo '')"
    if printf '%s' "$RESP" | python3 -c \
        'import json,sys;d=json.load(sys.stdin);assert d["choices"][0]["message"]["role"]=="assistant"' 2>/dev/null; then
      ok "role 字段正确（assistant）"
    else
      warn "role 字段异常，补丁可能没生效"
    fi
  else
    warn "对话未成功，看 docker compose logs"
  fi

  # 5.4 确认请求真的走了求解器
  if grep -q "POST /turnstile/sync" /root/turnstile_server.log 2>/dev/null; then
    ok "求解器被调用: $(grep -c 'POST /turnstile/sync' /root/turnstile_server.log) 次"
  else
    warn "求解器日志里没有调用记录（可能命中了 cookie 缓存，属正常）"
  fi
fi

# ── 6. 完成 ───────────────────────────────────────────────────
step "6/6  部署完成"

if [ "$DRY_RUN" -eq 1 ]; then
  ok "DRY-RUN 结束，未做任何修改"
  exit 0
fi

cat <<EOF

${c_bold}${c_grn}═══ 部署成功 ═══${c_reset}

${c_bold}API 地址${c_reset}   http://<你的VPS_IP>:8000/v1
${c_bold}管理台${c_reset}     http://<你的VPS_IP>:8001
${c_bold}搜索代理${c_reset}   http://<你的VPS_IP>:${SEARCH_PROXY_PORT:-8002}/v1
             ${c_grn}← 客户端 base_url 指这个，就自动有联网搜索${c_reset}
${c_bold}API Key${c_reset}    $API_KEY
${c_bold}管理台密码${c_reset} $ADMIN_PASSWORD

${c_bold}快速测试${c_reset}
  curl http://127.0.0.1:8000/v1/chat/completions \\
    -H "Authorization: Bearer $API_KEY" \\
    -H "Content-Type: application/json" \\
    -d '{"model":"deepseek-v4-flash-de","messages":[{"role":"user","content":"hi"}]}'

${c_bold}常用命令${c_reset}
  自检        bash $BASE_DIR/scripts/check.sh
  求解器日志  tail -f /root/turnstile_server.log
  容器日志    cd $DSFREE_DIR && docker compose logs -f
  重启求解器  systemctl restart turnstile-solver
  重启面板    cd $DSFREE_DIR && docker compose up -d --force-recreate

${c_bold}凭据已保存${c_reset}
  $DSFREE_DIR/.env                      (管理台密码 / API Key / 预热参数)
  /root/.turnstile_solver_key           (求解器 key)

${c_yel}注意${c_reset}
  · 启动后约 75 秒后台预热完成（3 站点各求解一次），之后请求约 1 秒返回
  · 改 config.toml 后必须 docker compose up -d --force-recreate
    （restart 不重新解析 bind mount，改动不生效）
  · 管理台默认对外暴露，建议用防火墙只放行你的 IP

EOF
