#!/usr/bin/env bash
# Turnstile 栈自检 —— 8 项检查
# 用法: bash check.sh
# 可用环境变量覆盖: SOLVER_PORT / DSFREE_CONTAINER / SOLVER_LOG
set -u

SOLVER_PORT="${SOLVER_PORT:-8899}"
SOLVER_LOG="${SOLVER_LOG:-/root/turnstile_server.log}"
CONTAINER="${DSFREE_CONTAINER:-$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i dsfree2api | head -1)}"

ok()   { echo "  OK"; }
bad()  { echo "  ✗ $*"; }

echo "== 1. Xvfb =="
ps aux | grep -q "[X]vfb" && ok || bad "未运行"

echo "== 2. 求解器进程 =="
pgrep -f turnstile_server.py >/dev/null && ok || bad "未运行"

echo "== 3. 求解器端口 ($SOLVER_PORT) =="
ss -ltn 2>/dev/null | grep -q ":$SOLVER_PORT" && ok || bad "未监听"

echo "== 4. health =="
curl -s --max-time 6 "http://127.0.0.1:$SOLVER_PORT/health" || bad "无响应"
echo

echo "== 5. 容器状态 =="
if [ -n "$CONTAINER" ]; then
  docker ps --filter "name=$CONTAINER" --format '  {{.Names}}  {{.Status}}'
else
  bad "找不到 dsfree2api 容器"
fi

if [ -n "$CONTAINER" ]; then
  echo "== 6. 容器 -> 宿主求解器 =="
  GW="$(docker inspect "$CONTAINER" \
        --format '{{range $k,$v := .NetworkSettings.Networks}}{{$v.Gateway}}{{end}}' 2>/dev/null)"
  echo "  网桥网关=$GW"
  docker exec "$CONTAINER" wget -qO- --timeout=6 "http://$GW:$SOLVER_PORT/health" \
    || bad "不通（检查 api_url 是否用了 127.0.0.1）"
  echo

  echo "== 7. 面板 turnstile 配置 =="
  docker exec "$CONTAINER" sed -n '/\[turnstile\]/,/^\[/p' /app/config.toml 2>/dev/null \
    | grep -E "enabled|api_url|provider" | sed 's/^/  /'

  echo "== 8. config.toml 容器内可写（管理台保存用）=="
  docker exec "$CONTAINER" sh -c \
    'test -w /app/config.toml && echo "  OK 可写" || echo "  ✗ 不可写 → chown 10001:10001 config.toml"'
fi

echo
echo "== 附：求解器日志尾部 =="
tail -5 "$SOLVER_LOG" 2>/dev/null | sed 's/^/  /' || echo "  (无日志)"
