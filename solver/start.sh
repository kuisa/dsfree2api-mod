#!/usr/bin/env bash
# 启动 Turnstile 求解服务（dsfree2api 的「方式 1：求解服务 API」后端）
# 用法: bash /root/start_turnstile_solver.sh     # 启动/重启
#       pkill -f turnstile_server.py            # 停止
#       tail -f /root/turnstile_server.log      # 看日志
set -u

# 如果已经用 systemd 托管，就交给 systemd，避免和它抢进程/端口
if systemctl list-unit-files 2>/dev/null | grep -q '^turnstile-solver.service'; then
  echo "检测到 systemd 已托管 turnstile-solver.service，改用它："
  systemctl restart turnstile-solver.service
  sleep 5
  echo "状态: $(systemctl is-active turnstile-solver.service)"
  curl -s --max-time 5 http://127.0.0.1:8899/health || echo "启动失败，看 journalctl -u turnstile-solver -n 50"
  echo
  exit 0
fi

KEY_FILE=/root/.turnstile_solver_key
export API_KEY="$(cat "$KEY_FILE" 2>/dev/null || true)"

# 必须用 /usr/bin/python3（装了 seleniumbase 4.53.7）
export DISPLAY="${DISPLAY:-${BROWSER_DISPLAY:-:1}}"
export LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
export LISTEN_PORT="${LISTEN_PORT:-8899}"
export MAIN_URL="${MAIN_URL:-https://deepseek.de}"
# 面板校验 token 用的 UA（[upstream].user_agent）。若 verify 失败再打开：
# export FORCE_UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

pkill -f "turnstile_server.py" 2>/dev/null && { echo "已停掉旧进程，等待退出..."; sleep 2; }

nohup /usr/bin/python3 -u /root/turnstile_server.py \
  > /root/turnstile_server.log 2>&1 &
echo "已启动 pid=$!"

sleep 3
echo "--- health ---"
curl -s --max-time 5 "http://127.0.0.1:${LISTEN_PORT}/health" || echo "启动失败，看日志: /root/turnstile_server.log"
echo
echo "--- 监听 ---"
ss -ltn | grep ":${LISTEN_PORT}" || echo "端口未监听"
