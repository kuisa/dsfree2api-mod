#!/usr/bin/env bash
# Turnstile 求解器健康检查：连续失败 2 次就重启服务。
# 用于兜住"进程活着但卡死（D 状态）"的情况 —— 这时 systemd 的 Restart=always 不会触发。
set -u
URL="http://127.0.0.1:8899/health"
STATE="/run/turnstile_healthcheck.fails"
UNIT="turnstile-solver.service"
TIMEOUT=12

if curl -sf --max-time "$TIMEOUT" "$URL" >/dev/null 2>&1; then
    echo 0 > "$STATE" 2>/dev/null || true
    exit 0
fi

n=$(cat "$STATE" 2>/dev/null || echo 0)
case "$n" in ''|*[!0-9]*) n=0 ;; esac
n=$((n + 1))
echo "$n" > "$STATE" 2>/dev/null || true
echo "$(date '+%F %T') health 失败第 $n 次"

if [ "$n" -ge 2 ]; then
    echo "$(date '+%F %T') 连续 $n 次失败，重启 $UNIT"
    systemctl restart "$UNIT"
    sleep 5
    curl -sf --max-time "$TIMEOUT" "$URL" >/dev/null 2>&1 \
      && echo "$(date '+%F %T') 重启后恢复 ✓" \
      || echo "$(date '+%F %T') 重启后仍不通 ✗"
    echo 0 > "$STATE" 2>/dev/null || true
fi
