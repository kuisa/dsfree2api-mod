#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Turnstile 求解服务 —— 包装本地 SeleniumBase 抓取脚本

适配 dsfree2api 的「方式 1：求解服务 API」协议（internal/turnstile/solver.go）：
    单个同步 POST 到你配置的 api_url（不追加任何路径），
    Header: Authorization: Bearer <api_key>
    Body  : {"url","sitekey","action","cdata","timeoutSeconds"}
    应答  : {"errorId":0,"status":"ready","solution":{"token":"1.xxx"}}

同时兼容 CapSolver 风格的两段式（createTask / getTaskResult）和任意路径直出。

运行: python3 turnstile_server.py
依赖: seleniumbase
"""

import os
import re
import json
import time
import uuid
import queue
import random
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# ================= 配置区域 =================
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8899"))

# 面板里填的 API Key。留空 = 不校验。
API_KEY = os.getenv("API_KEY", "")

# 抓 token 的目标站；请求体里带 url 时以请求体为准
DEFAULT_URL = os.getenv("MAIN_URL", "https://deepseek.de")
PROXY_URL = os.getenv("PROXY", "") or None
LOCALE = os.getenv("LOCALE", "ja")

# 强制浏览器 UA。留空 = 用浏览器真实 UA。
# dsfree2api 校验 token 时用的是 [upstream].user_agent，两者不一致时
# 若 verify 失败，把这里设成面板那个 UA 字符串再试。
FORCE_UA = os.getenv("FORCE_UA", "")

TOKEN_TTL = 180        # 秒，缓存有效期（Turnstile token 本身约 300s）
TOKEN_WAIT = 25        # 秒，单次尝试内轮询等 token 出现的上限
SOLVE_ATTEMPTS = 2     # 单次请求内的尝试次数（面板自己还会重试 5 次）
# ===========================================

# ---------- 浏览器环境 ----------
os.environ.setdefault(
    "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
# 面板只注入 BROWSER_DISPLAY，不注入 DISPLAY；SSH 手起时也没有，必须兜底
os.environ.setdefault("DISPLAY", os.environ.get("BROWSER_DISPLAY") or ":1")
if "XAUTHORITY" not in os.environ and os.path.exists("/home/headless/.Xauthority"):
    os.environ["XAUTHORITY"] = "/home/headless/.Xauthority"

from seleniumbase import SB  # noqa: E402


# ---------- 求解核心 ----------
class TurnstileSolver:
    """串行执行浏览器抓取，按目标 URL 缓存 token。"""

    def __init__(self):
        self._lock = threading.Lock()   # 保证同一时刻只有一个浏览器在跑
        self._cache = {}                # url -> {"token","ua","ts"}

    def log(self, msg):
        print(f"[{time.strftime('%H:%M:%S')}] [SOLVER] {msg}", flush=True)

    def _cache_get(self, url):
        c = self._cache.get(url)
        if c and (time.time() - c["ts"]) < TOKEN_TTL:
            return c["token"], c["ua"]
        return None

    # ---- 人类行为模拟 ----
    @staticmethod
    def _human_wait(min_s=2.0, max_s=4.0):
        time.sleep(random.uniform(min_s, max_s))

    @staticmethod
    def _move_mouse(sb):
        """轻微鼠标移动预热（不点击，避免误触页面元素）"""
        try:
            for _ in range(3):
                sb.move_to_element("body", timeout=5)
                time.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    @staticmethod
    def _get_ua(sb):
        try:
            return sb.execute_script("return navigator.userAgent")
        except Exception:
            return None

    def _grab(self, url):
        """跑一次浏览器，返回 (token, user_agent)。"""
        kwargs = dict(
            uc=True,
            headed=True,
            headless=False,
            xvfb=False,          # 用系统 Xvfb :1；换无 Xvfb 的机器时改 True
            locale=LOCALE,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage,--window-position=0,0,--start-maximized",
            proxy=PROXY_URL if PROXY_URL else None,
        )
        if FORCE_UA:
            kwargs["agent"] = FORCE_UA

        with SB(**kwargs) as sb:
            sb.uc_open_with_reconnect(url, reconnect_time=10)
            self._human_wait()
            self._move_mouse(sb)

            try:
                sb.uc_gui_click_captcha()
                sb.uc_gui_handle_captcha()
            except Exception as e:
                self.log(f"captcha 交互异常（继续轮询 token）: {e}")

            # 轮询等 token 落进隐藏 input，而不是死等固定秒数
            token = ""
            deadline = time.time() + TOKEN_WAIT
            while time.time() < deadline:
                try:
                    v = sb.get_attribute('input[name="cf-turnstile-response"]', "value") or ""
                except Exception:
                    v = ""
                if v.strip():
                    token = v.strip()
                    break
                time.sleep(1.0)

            return token, self._get_ua(sb)

    def solve(self, url=None):
        """返回 (token, ua)；失败抛异常。"""
        url = (url or DEFAULT_URL).strip() or DEFAULT_URL
        with self._lock:
            hit = self._cache_get(url)
            if hit:
                self.log(f"命中缓存 {url}")
                return hit

            self.log(f"开始求解 {url}")
            last_err = None
            for attempt in range(1, SOLVE_ATTEMPTS + 1):
                t0 = time.time()
                try:
                    token, ua = self._grab(url)
                    if token:
                        self._cache[url] = {"token": token, "ua": ua, "ts": time.time()}
                        self.log(f"成功 (第 {attempt} 次，耗时 {time.time()-t0:.1f}s)，token 长度 {len(token)}")
                        return token, ua
                    last_err = f"空 token（等待 {TOKEN_WAIT}s 内未出现）"
                    self.log(f"第 {attempt} 次失败: {last_err}")
                except Exception as e:
                    last_err = str(e)
                    self.log(f"第 {attempt} 次失败: {e}")
                    traceback.print_exc()
                if attempt < SOLVE_ATTEMPTS:
                    time.sleep(random.uniform(2, 4))

            raise RuntimeError(f"求解失败: {last_err}")


# ---------- 任务队列（createTask / getTaskResult 两段式用） ----------
class TaskStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}

    def create(self, url):
        tid = uuid.uuid4().hex
        with self._lock:
            self._tasks[tid] = {"url": url, "status": "processing",
                                "solution": None, "error": None, "ts": time.time()}
        return tid

    def set_ready(self, tid, token, ua):
        with self._lock:
            if tid in self._tasks:
                self._tasks[tid].update({
                    "status": "ready",
                    "solution": {"token": token, "userAgent": ua, "gRecaptchaResponse": token},
                })

    def set_error(self, tid, err):
        with self._lock:
            if tid in self._tasks:
                self._tasks[tid].update({"status": "failed", "error": err})

    def get(self, tid):
        with self._lock:
            return self._tasks.get(tid)

    def gc(self, max_age=600):
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._tasks.items() if now - v["ts"] > max_age]:
                self._tasks.pop(k, None)


SOLVER = TurnstileSolver()
STORE = TaskStore()
WORK_Q = queue.Queue()


def worker_loop():
    while True:
        tid = WORK_Q.get()
        try:
            task = STORE.get(tid) or {}
            token, ua = SOLVER.solve(task.get("url"))
            STORE.set_ready(tid, token, ua)
        except Exception as e:
            STORE.set_error(tid, str(e))
        finally:
            WORK_Q.task_done()
            STORE.gc()


# ---------- HTTP 处理 ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "TurnstileSolver/1.0"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] [HTTP] {self.address_string()} {fmt % args}", flush=True)

    # ---- 工具 ----
    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            out = {}
            for kv in raw.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    out[k] = v
            return out

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _auth_ok(self, data):
        if not API_KEY:
            return True
        auth = self.headers.get("Authorization") or ""
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        key = (bearer or data.get("clientKey") or data.get("api_key")
               or data.get("key") or self.headers.get("X-API-Key") or "")
        return key == API_KEY

    @staticmethod
    def _target_url(data):
        return (data.get("url") or data.get("websiteURL")
                or data.get("website_url") or "").strip() or DEFAULT_URL

    # ---- 路由 ----
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        data = self._read_body()
        if not data and "?" in self.path:
            data = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

        if path in ("/", "/health", "/ping"):
            self._json(200, {"status": "ok", "service": "turnstile-solver"})
            return

        if not self._auth_ok(data):
            self._json(401, {"errorId": 1, "errorCode": "ERROR_KEY_DENIED",
                             "errorDescription": "invalid api key"})
            return

        # --- 两段式: getTaskResult ---
        if path.endswith("getTaskResult"):
            task = STORE.get(data.get("taskId") or "")
            if not task:
                self._json(200, {"errorId": 1, "errorCode": "ERROR_NO_SUCH_TASK",
                                 "errorDescription": "task not found"})
            elif task["status"] == "ready":
                self._json(200, {"errorId": 0, "status": "ready", "solution": task["solution"]})
            elif task["status"] == "failed":
                self._json(200, {"errorId": 1, "status": "failed",
                                 "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                                 "errorDescription": task["error"] or "solve failed"})
            else:
                self._json(200, {"errorId": 0, "status": "processing"})
            return

        # --- 两段式: createTask ---
        if path.endswith("createTask"):
            tid = STORE.create(self._target_url(data))
            WORK_Q.put(tid)
            self._json(200, {"errorId": 0, "taskId": tid})
            return

        # --- 同步直出（dsfree2api 走的就是这条）---
        t0 = time.time()
        try:
            token, ua = SOLVER.solve(self._target_url(data))
            self._json(200, {
                "errorId": 0,
                "status": "ready",
                "solution": {"token": token, "userAgent": ua, "gRecaptchaResponse": token},
                "token": token,
                "userAgent": ua,
                "elapsedTime": round(time.time() - t0, 3),
            })
        except Exception as e:
            self._json(200, {"errorId": 1, "status": "failed",
                             "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                             "errorDescription": str(e)})


def main():
    for _ in range(2):
        threading.Thread(target=worker_loop, daemon=True).start()

    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[{time.strftime('%H:%M:%S')}] [SERVER] listening on "
          f"http://{LISTEN_HOST}:{LISTEN_PORT}  api_key={'set' if API_KEY else 'none'}"
          f"  force_ua={'set' if FORCE_UA else 'off'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[SERVER] shutting down", flush=True)
        srv.shutdown()


if __name__ == "__main__":
    main()
