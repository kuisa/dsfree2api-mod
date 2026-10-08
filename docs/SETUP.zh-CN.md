# dsfree2api + 自建 Turnstile 求解器 · 完整部署教学流程

> 从一台裸 Debian 13 VPS，到「面板能自动过 Cloudflare Turnstile」的完整过程。
> 全程实测环境：Debian 13 (trixie) x86_64 / Python 3.13.5 / Chrome 152 / seleniumbase 4.53.7
> 写完日期：2026-10-07

---

## 目录

1. [这套东西在干什么](#1-这套东西在干什么)
2. [先搞懂原理，否则一定卡住](#2-先搞懂原理否则一定卡住)
3. [环境准备](#3-环境准备)
4. [部署 dsfree2api](#4-部署-dsfree2api)
5. [部署 Turnstile 求解器](#5-部署-turnstile-求解器)
6. [两边对接](#6-两边对接)
7. [四步验证](#7-四步验证)
8. [故障排查表](#8-故障排查表)
9. [开机自启（systemd）](#9-开机自启systemd)
10. [附录 A：求解器协议详解](#附录-a求解器协议详解)
11. [附录 B：七个必踩的坑](#附录-b七个必踩的坑)
12. [附录 C：三个增强补丁（轮询 / 预换 cookie / 后台预热）](#附录-c三个增强补丁轮询--预换-cookie--后台预热)

---

## 1. 这套东西在干什么

### 架构图

```
 你的客户端 / 各种 AI 客户端
        │  POST /v1/chat/completions  (OpenAI 格式)
        ▼
 ┌──────────────────────────────────────────────┐
 │  dsfree2api  容器  :8000   (API)             │
 │              容器  :8001   (管理台)          │
 │                                              │
 │  收到请求 → 发现要过 Turnstile → 发 HTTP 请求 │
 └──────────────────┬───────────────────────────┘
                    │  POST http://172.20.0.1:8899/turnstile/sync
                    │  {"url","sitekey","action",...}
                    ▼
 ┌──────────────────────────────────────────────┐
 │  turnstile_server.py   宿主  :8899            │
 │                                              │
 │  收到 → 起一个有头 Chrome → 访问目标站       │
 │       → 点 Turnstile → 读 token → 返回        │
 └──────────────────┬───────────────────────────┘
                    │  有头 Chrome 渲染在 Xvfb :1
                    ▼
              Xvfb :1  (虚拟显示器)

 拿到 token 后：dsfree2api 把 token 提交给目标站 → 换到 cookie → 正常聊天
```

### 名词解释

| 名词 | 是什么 |
|---|---|
| **dsfree2api** | Go 写的 OpenAI 兼容网关（[github.com/nyoungo/dsfree2api](https://github.com/nyoungo/dsfree2api)），把上游免费站点的对话能力包装成标准 API |
| **Turnstile** | Cloudflare 的人机验证（类似验证码），上游站点用它挡机器人 |
| **求解器** | 我们自己写的服务：用真浏览器点掉 Turnstile，把 token 交出去 |
| **Xvfb** | 虚拟显示器。服务器没屏幕，Chrome 有头模式需要一块"假屏幕"才能跑 |
| **token** | Turnstile 生成的凭证，形如 `1.xxxxx.yyyyy.zzzzz`，**一次性、约 5 分钟过期** |

---

## 2. 先搞懂原理，否则一定卡住

### 2.1 为什么必须用"有头"浏览器

Cloudflare Turnstile 会检查浏览器指纹。无头（headless）模式的指纹特征很明显，过验证率极低。

所以流程是：**虚拟显示器（Xvfb）→ 在上面开一个"看起来正常"的 Chrome → 用模拟鼠标点击去点验证框**。

这就是为什么后面要装 `xdotool`、`scrot` 这些 GUI 工具，也是为什么脚本里要设 `DISPLAY=:1`。

### 2.2 为什么求解器要和面板"出口 IP 一致"

Turnstile 的 token 是**绑定出口 IP** 的。如果：
- 求解器从 A 网络出口拿到 token
- 面板从 B 网络出口提交 token

→ 大概率被判定无效。

**所以：面板走什么网络，求解器就得走什么网络。** 面板用代理，求解器也要配同一个代理（脚本里的 `PROXY` 环境变量）。

本文档的配置两边都是**直连**，天然一致。

### 2.3 面板在容器里，求解器在宿主上 —— 这是最容易死的地方

这是**新手 90% 会卡住的地方**，先讲透：

```
┌─ 宿主 (你的 VPS) ─────────────────────────┐
│  网卡 docker0      172.17.0.1             │
│  网桥 br-xxxx      172.20.0.1  ← 网关     │
│                                           │
│  ┌─ 容器 dsfree2api ──────────────────┐   │
│  │  容器自己的 IP: 172.20.0.2         │   │
│  │                                    │   │
│  │  在容器里访问 127.0.0.1            │   │
│  │  = 访问【容器自己】，不是宿主！    │   │
│  └────────────────────────────────────┘   │
└───────────────────────────────────────────┘
```

- ❌ 面板里填 `http://127.0.0.1:8899` → 容器去连自己，**Connection refused**
- ✅ 面板里填 `http://172.20.0.1:8899` → 这是网桥网关 = 宿主，**通**

**这个 IP 不是固定的**，取决于 docker 给这个 compose 项目分配的子网。部署时必须自己查一遍（第 6 节有命令）。

---

## 3. 环境准备

> 以下命令全部用 root 执行。如果你用普通用户，前面加 `sudo`。

### 3.1 装 Docker

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker --version          # 应输出 Docker version 2x.x.x
docker compose version    # 应输出 Docker Compose version v2.x.x
```

### 3.2 装 Xvfb + GUI 工具

```bash
apt update
apt install -y xvfb xdotool scrot x11-utils fonts-noto-cjk curl wget git openssl
```

各包作用：

| 包 | 用途 |
|---|---|
| `xvfb` | 虚拟显示器 |
| `xdotool` | 模拟鼠标点击（点验证框靠它） |
| `scrot` | 截图（SeleniumBase 内部会用到） |
| `fonts-noto-cjk` | 中文字体，防止页面渲染异常 |
| `openssl` | 后面生成 API Key 用 |

### 3.3 装 Google Chrome

```bash
cd /tmp
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
apt install -y ./google-chrome-stable_current_amd64.deb
google-chrome-stable --version    # 应输出 Google Chrome 1xx.x.x.x
```

> **ARM 机器（aarch64）注意**：Google 不提供 ARM 版 Chrome。ARM 上改装 Chromium：
> ```bash
> apt install -y chromium
> ```
> 但 SeleniumBase 的自动探测**只找 `google-chrome` 系列，不找 chromium**（已核对源码
> `core/browser_launcher.py`），所以还要做个软链接让它能被找到：
> ```bash
> ln -sf /usr/bin/chromium /usr/bin/google-chrome
> /usr/bin/google-chrome --version    # 确认能跑
> ```
> ARM 上过验证率略低于 x86_64，但可用。**能用 x86_64 就用 x86_64。**

### 3.4 装 Python 依赖

Debian 13 有 PEP 668 保护，直接 `pip install` 会报 `externally-managed-environment`。用：

```bash
python3 -m pip install --break-system-packages -U seleniumbase
python3 -c "import seleniumbase; print(seleniumbase.__version__)"
```

应输出 `4.30.0` 或更高（实测 4.53.7 可用）。

> **为什么要 `--break-system-packages`**：这是系统级安装，装到 `/usr/local/lib/python3.x/dist-packages`。
> 也可以用 venv，但那样启动脚本里的解释器路径要跟着改。**教学场景建议直接用系统级**，少一层坑。

### 3.5 启动 Xvfb

```bash
Xvfb :1 -screen 0 1440x900x24 -ac +extension GLX +render -noreset &
```

参数逐个解释：

| 参数 | 含义 |
|---|---|
| `:1` | 显示器编号，对应 `DISPLAY=:1` |
| `-screen 0 1440x900x24` | 屏幕 0，分辨率 1440x900，24 位色 |
| `-ac` | **关闭访问控制** → 不需要 `XAUTHORITY`，省一个坑 |
| `+extension GLX +render` | 开 GLX/render 扩展，Chrome 渲染需要 |
| `-noreset` | 客户端断开后不重置，避免 Chrome 反复重连 |

验证：

```bash
ps aux | grep [X]vfb          # 应看到 Xvfb :1 进程
ls /tmp/.X11-unix/            # 应看到 X1 这个 socket 文件
```

> 生产环境请用第 9 节的 systemd 托管，别用 `&` 挂在 SSH 里。

### 3.6 （可选）装 noVNC 看实时画面

调试时能"看见"Chrome 在干什么，强烈推荐：

```bash
apt install -y x11vnc novnc websockify

x11vnc -display :1 -forever -shared -rfbport 5901 -localhost -nopw &
websockify --web=/usr/share/novnc 6080 127.0.0.1:5901 &
```

然后 SSH 端口转发到本地看：

```bash
# 在你自己的电脑上执行
ssh -L 6080:127.0.0.1:6080 root@你的VPS_IP
# 浏览器打开 http://127.0.0.1:6080/vnc.html
```

> 生产环境建议给 x11vnc 设密码（`-rfbauth`），不要裸奔在公网。

---

## 4. 部署 dsfree2api

### 4.1 拉代码

```bash
cd /root
git clone https://github.com/nyoungo/dsfree2api.git
cd dsfree2api
```

### 4.2 生成配置

```bash
cp config.example.toml config.toml
```

### 4.3 必改的两处 —— **两个文件都要改！**

> ⚠️ **这是最容易白干的地方**：环境变量优先级**高于** `config.toml`。
> 只改 `config.toml` 里的密码/API Key，**重启后会被 `docker-compose.yml` 里的环境变量覆盖回去**，
> 表现就是"我明明改了，怎么还是不对"。**两个文件必须改成一致。**

#### 文件一：`config.toml`

```toml
[security]
# 下游调用你 API 时用的 key。建议改成自己的，别用默认的。
api_keys = ["你自己的强APIKey"]

[admin]
enabled = true
password = "你的管理台密码"
```

#### 文件二：`docker-compose.yml`

同一个密码/Key 要在这里再写一遍（或者用下面推荐的 `.env` 方式）：

```yaml
    environment:
      HOST: 0.0.0.0
      PORT: 8000
      ADMIN_HOST: 0.0.0.0
      ADMIN_PASSWORD: ${ADMIN_PASSWORD:-你的管理台密码}   # ← 改成和 config.toml 一致
      API_KEYS: ${API_KEYS:-你自己的强APIKey}             # ← 改成和 config.toml 一致
      TURNSTILE_API_KEY: ${TURNSTILE_API_KEY:-}          # 留空即可，见下表
      PROXY_URL: ${PROXY_URL:-}
      PROXY_FALLBACK_URLS: ${PROXY_FALLBACK_URLS:-}
      PROXY_SLOW_START_SECONDS: ${PROXY_SLOW_START_SECONDS:-8}
```

#### 环境变量覆盖规则（读源码 `applyEnv()` 得到的准确语义）

| 环境变量 | 覆盖行为 | 你要注意什么 |
|---|---|---|
| `ADMIN_PASSWORD` | **只要设置了就覆盖，连空值也覆盖** | compose 里必须写对，否则 config.toml 的密码永远无效 |
| `API_KEYS` | 只要设置了就覆盖 | 同上 |
| `ADMIN_HOST` | 只要设置了就覆盖 | compose 写死 `0.0.0.0`，config 里的 `127.0.0.1` 不起作用 |
| `HOST` / `PORT` | 只要设置了就覆盖 | 同上 |
| `TURNSTILE_ENABLED` | 只要设置了就覆盖 | 一般别设，用 config.toml |
| `TURNSTILE_API_KEY` | **仅非空时覆盖** | 留空则 config.toml 的 `api_key` 生效 ✓ |
| `TURNSTILE_PROVIDER` | 仅非空时覆盖 | 同上 |
| `TURNSTILE_BROWSER_PATH` | 仅非空时覆盖 | 同上 |
| `DATA_DIR` | 仅非空时覆盖 | 同上 |

**结论**：`ADMIN_PASSWORD` / `API_KEYS` / `ADMIN_HOST` / `HOST` / `PORT` 这五个**必须在 compose 里写对**；
`TURNSTILE_*` 留空就行，交给 config.toml 管。

#### 推荐做法：用 `.env` 文件，只维护一份

在 `docker-compose.yml` 同级建 `.env`（compose 会自动读取），两个文件就不用各写一遍：

```bash
cd /root/dsfree2api
cat > .env <<'EOF'
ADMIN_PASSWORD=你的管理台密码
API_KEYS=你自己的强APIKey
EOF
chmod 600 .env
```

然后 `config.toml` 里也填成一样的值（管理台界面里显示的是生效值，可以拿来核对）。

**关于管理台暴露**：compose 里设了 `ADMIN_HOST: 0.0.0.0`，所以管理台 `:8001` 是对外开放的。
**必须改掉默认密码**，或者用防火墙只放行你自己的 IP：

```bash
# 只允许某个 IP 访问管理台
iptables -A INPUT -p tcp --dport 8001 ! -s 你的IP -j DROP
```

### 4.4 修 config.toml 写入权限（否则管理台存不了东西）

**症状**：在管理台点"保存配置"、开关模型，任何改动都提示 `write config.toml denied`。

**原因**：容器以非 root 用户运行（`uid=10001(app)`），而 `config.toml` 是从宿主挂进去的，
属主是 `root:root` 且权限 `0644` —— 容器用户**只能读不能写**。
管理台保存是**原地截断重写**这个文件（`internal/config/config.go` 的 `SaveTo()`），所以直接失败。

**自查**：

```bash
# 宿主侧
ls -la /root/dsfree2api/config.toml
# 容器侧（应显示"可写"）
docker exec dsfree2api-dsfree2api-1 sh -c 'test -w /app/config.toml && echo 可写 || echo 不可写'
```

**修复**（把属主改成容器用户）：

```bash
chown 10001:10001 /root/dsfree2api/config.toml
```

**验证**：

```bash
docker exec dsfree2api-dsfree2api-1 sh -c 'test -w /app/config.toml && echo "可写 ✓"'

# 走真实的 UI 保存路径测一遍
curl -s -c /tmp/c -X POST http://127.0.0.1:8001/api/login \
  -H 'Content-Type: application/json' -d '{"password":"你的管理台密码"}'
curl -s -b /tmp/c http://127.0.0.1:8001/api/config -o /tmp/cfg.json
curl -s -b /tmp/c -X PUT http://127.0.0.1:8001/api/config \
  -H 'Content-Type: application/json' --data-binary @/tmp/cfg.json
# 期望输出 {"ok":true}
```

> **注意**：管理台保存是**原地重写**（不是临时文件+改名），所以属主会保持不变，
> `chown` 一次就长期有效。
>
> 但如果你用编辑器"原子替换"的方式（新建临时文件再改名）编辑过 `config.toml`，
> 属主会变回 `root:root`，**需要重新 chown**。
>
> 另外 `/app` 目录本身也不可写，所以它不会生成 `config.toml.bak` 备份 ——
> 代码里这步的错误被忽略（`_ = os.WriteFile(...)`），**不影响保存**。
> 想让它生成备份，就得改 Dockerfile 或把整个 `/app` 目录挂进去，一般没必要。

### 4.5 先别急着填 Turnstile

`[turnstile]` 这一节**先保持 `enabled = false`**。等求解器部署好、验证能拿到 token 了，再回来打开。
否则面板每次请求都会因为找不到求解器而失败，日志会很乱。

### 4.6 启动

```bash
docker compose up -d
docker compose logs -f --tail 20
```

看到这两行就是成功：

```
msg="OpenAI-compatible API listening" addr=0.0.0.0:8000 models=6 admin=true
msg="web console listening" addr=0.0.0.0:8001
```

按 `Ctrl+C` 退出日志（容器不会停）。

### 4.7 确认容器的网络信息（第 6 节要用）

```bash
docker inspect dsfree2api-dsfree2api-1 \
  --format '{{range $k,$v := .NetworkSettings.Networks}}网桥={{$k}} 容器IP={{$v.IPAddress}} 网关={{$v.Gateway}}{{end}}'
```

记下 **网关** 那个 IP（本文档示例是 `172.20.0.1`，你的可能不同）。

---

## 5. 部署 Turnstile 求解器

### 5.1 保存脚本

把下面内容保存为 `/root/turnstile_server.py`：

```python
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
```

### 5.2 生成 API Key

```bash
openssl rand -hex 16 > /root/.turnstile_solver_key
chmod 600 /root/.turnstile_solver_key
cat /root/.turnstile_solver_key      # 记下这个值，第 6 节要填进面板
```

### 5.3 保存启动脚本

保存为 `/root/start_turnstile_solver.sh`：

```bash
#!/usr/bin/env bash
# 启动 Turnstile 求解服务（dsfree2api 的「方式 1：求解服务 API」后端）
# 用法: bash /root/start_turnstile_solver.sh     # 启动/重启
#       pkill -f turnstile_server.py            # 停止
#       tail -f /root/turnstile_server.log      # 看日志
set -u

KEY_FILE=/root/.turnstile_solver_key
export API_KEY="$(cat "$KEY_FILE" 2>/dev/null || true)"

# 必须用装了 seleniumbase 的解释器
export DISPLAY="${DISPLAY:-${BROWSER_DISPLAY:-:1}}"
export LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
export LISTEN_PORT="${LISTEN_PORT:-8899}"
export MAIN_URL="${MAIN_URL:-https://deepseek.de}"
# 面板校验 token 用的 UA（[upstream].user_agent）。若 verify 失败再打开：
# export FORCE_UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

pkill -f "turnstile_server.py" 2>/dev/null && { echo "已停掉旧进程，等待退出..."; sleep 2; }

nohup python3 -u /root/turnstile_server.py \
  > /root/turnstile_server.log 2>&1 &
echo "已启动 pid=$!"

sleep 3
echo "--- health ---"
curl -s --max-time 5 "http://127.0.0.1:${LISTEN_PORT}/health" || echo "启动失败，看日志: /root/turnstile_server.log"
echo
echo "--- 监听 ---"
ss -ltn | grep ":${LISTEN_PORT}" || echo "端口未监听"
```

### 5.4 启动

```bash
chmod +x /root/start_turnstile_solver.sh
bash /root/start_turnstile_solver.sh
```

期望输出：

```
已启动 pid=12345
--- health ---
{"status": "ok", "service": "turnstile-solver"}
--- 监听 ---
LISTEN 0      5            0.0.0.0:8899       0.0.0.0:*
```

### 5.5 单独测一次求解（不经过面板）

```bash
time curl -s --max-time 180 -X POST http://127.0.0.1:8899/turnstile/sync \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(cat /root/.turnstile_solver_key)" \
  -d '{"url":"https://deepseek.de","sitekey":"0x4AAAAAADlLZ3ljqZP6cQwq","action":"chat","cdata":"","timeoutSeconds":90}' \
  | python3 -m json.tool | head -20
```

**成功的样子**（约 25 秒）：

```json
{
    "errorId": 0,
    "status": "ready",
    "solution": {
        "token": "1.E4wde_SiSMRaX6B35mEyD3...",
        "userAgent": "Mozilla/5.0 (X11; Linux x86_64) ... Chrome/152.0.0.0 ..."
    },
    "elapsedTime": 25.376
}
```

看日志确认：

```bash
tail -20 /root/turnstile_server.log
```

```
[17:55:55] [SOLVER] 开始求解 https://deepseek.de
[17:56:19] [SOLVER] 成功 (第 1 次，耗时 23.6s)，token 长度 752
[17:56:19] [HTTP] 127.0.0.1 "POST /turnstile/sync HTTP/1.1" 200 -
```

**这一步过了再往下走。** 如果这里拿不到 token，面板那边一定不通，先看第 8 节。

---

## 6. 两边对接

### 6.1 查容器能看到的网关 IP

```bash
docker inspect dsfree2api-dsfree2api-1 \
  --format '{{range $k,$v := .NetworkSettings.Networks}}网关={{$v.Gateway}}{{end}}'
```

假设输出 `网关=172.20.0.1`。

### 6.2 先验证容器 → 宿主通不通

```bash
docker exec dsfree2api-dsfree2api-1 wget -qO- --timeout=5 http://172.20.0.1:8899/health
```

应输出 `{"status": "ok", "service": "turnstile-solver"}`。

再确认反例（教学时演示一下，印象深）：

```bash
docker exec dsfree2api-dsfree2api-1 wget -qO- --timeout=3 http://127.0.0.1:8899/health
# wget: can't connect to remote host (127.0.0.1): Connection refused
```

### 6.3 改 config.toml

```bash
cd /root/dsfree2api
cp config.toml "config.toml.bak.$(date +%Y%m%d-%H%M%S)"    # 先备份
```

编辑 `[turnstile]` 段：

```toml
[turnstile]
enabled = true                    # ← 原来是 false，这是总开关
provider = "api"                  # 三选一：api / browser / manual
api_url = "http://172.20.0.1:8899/turnstile/sync"
api_key = "把你第 5.2 步生成的 key 填这里"
sitekey = "0x4AAAAAADlLZ3ljqZP6cQwq"
action = "chat"
timeout_seconds = 90
cookie_ttl_seconds = 10800        # cookie 缓存 3 小时
retries = 5
retry_backoff_seconds = 1.5
```

> `api_url` 的路径部分（`/turnstile/sync`）**随便写什么都行**。
> 面板是原样打这个地址，不追加路径；求解器对所有路径都当"同步直出"处理。

### 6.4 重建容器（关键！）

```bash
cd /root/dsfree2api
docker compose up -d --force-recreate
```

> **为什么不能用 `docker compose restart`**：
> `config.toml` 是**文件级 bind mount**。用编辑器保存（原子替换）会换掉文件 inode，
> 容器仍然指向旧 inode，看到的是旧内容。`--force-recreate` 会重建容器、重新解析挂载。
>
> 验证容器里看到的是不是新内容：
> ```bash
> docker exec dsfree2api-dsfree2api-1 sed -n '/\[turnstile\]/,/^\[/p' /app/config.toml
> ```

---

## 7. 四步验证

### 第 1 步：确认配置加载了

```bash
# 登录管理台（密码是 config.toml 里的 [admin].password）
curl -s -c /tmp/c -X POST http://127.0.0.1:8001/api/login \
  -H 'Content-Type: application/json' -d '{"password":"你的管理台密码"}'

# 读配置
curl -s -b /tmp/c http://127.0.0.1:8001/api/config | python3 -m json.tool | grep -A9 turnstile
```

期望看到：

```json
"enabled": true,
"provider": "api",
"api_url": "http://172.20.0.1:8899/turnstile/sync",
"api_key": "你的key",
```

### 第 2 步：真发一次对话

```bash
time curl -s --max-time 240 -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer 你的api_keys" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash-de","messages":[{"role":"user","content":"say hi in 3 words"}],"max_tokens":20,"stream":false}'
```

期望（总耗时约 25 秒，第一次要等求解）：

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "deepseek-v4-flash-de",
  "choices": [{"index": 0, "message": {"role": "", "content": "Hi there friend"}, "finish_reason": "stop"}],
  "usage": {"prompt_tokens": 38, "completion_tokens": 3, "total_tokens": 41}
}
```

### 第 3 步：确认请求真的走了求解器

```bash
tail -10 /root/turnstile_server.log
```

**必须看到来自容器 IP（`172.20.0.2`）的请求**，不是 `127.0.0.1`：

```
[17:55:55] [SOLVER] 开始求解 https://deepseek.de
[17:56:19] [SOLVER] 成功 (第 1 次，耗时 23.6s)，token 长度 752
[17:56:19] [HTTP] 172.20.0.2 "POST /turnstile/sync HTTP/1.1" 200 -
```

### 第 4 步：确认 cookie 缓存生效

```bash
curl -s -b /tmp/c http://127.0.0.1:8001/api/overview | python3 -m json.tool | grep -A20 '"turnstile"'
```

期望：

```
site=de  valid=true  cookie_count=3  remaining_s=10780    ← 约 3 小时
history: de ok=True 23906ms
```

**到这里全流程就通了。** 之后再发对话，3 小时内都直接复用 cookie，不再起浏览器。

---

## 8. 故障排查表

### 8.1 求解器起不来

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: No module named 'seleniumbase'` | 用错解释器（默认 `python3` 指向别的 venv） | 启动脚本里用绝对路径 `python3` 或 `/usr/bin/python3`，先 `which python3` 确认 |
| `launched a headed browser without having a XServer running` | `DISPLAY` 没设 | 确认 Xvfb 在跑；脚本里已有 `setdefault("DISPLAY", ...)`，检查是否被覆盖 |
| `Connection refused` / `Cannot open display :1` | Xvfb 没起 | `Xvfb :1 -screen 0 1440x900x24 -ac +extension GLX +render -noreset &` |
| `DevToolsActivePort file doesn't exist` | 缺 `--no-sandbox`（root 运行） | 脚本已带 `--no-sandbox`，检查 `chromium_arg` 是否被改 |
| 端口被占 | 旧进程没退 | `pkill -f turnstile_server.py`，`ss -ltn \| grep 8899` 确认释放 |
| `systemctl is-active xvfb.service` 永远是 `activating` | 重复建了 Xvfb 单元，抢不到 `:1` | 见 [坑 6](#坑-6systemd-里重复建-xvfb-单元)：删掉多余的单元 |
| `Unit xvfb.service not found` | 求解器 unit 里写死了 `Requires=xvfb.service` | 删掉那行，只保留 `After=network.target` |

### 8.2 求解失败 / 拿不到 token

| 现象 | 原因 | 解决 |
|---|---|---|
| 日志 `空 token（等待 25s 内未出现）` | 页面没渲染完 / 出口 IP 被 Cloudflare 拉黑 | 加大 `TOKEN_WAIT`；换出口 IP（配 `PROXY`） |
| 反复 `第 N 次失败` | 目标站改了 DOM 或 sitekey | 用 noVNC 看着跑，确认验证框长什么样 |
| 能拿到 token 但面板报 verify 失败 | UA 不一致 或 出口 IP 不一致 | 打开 `FORCE_UA` 设成 `[upstream].user_agent` 同值；检查 `PROXY` |
| 浏览器一开就崩 | 内存不足 | Chrome 约需 500MB-1GB；`free -m` 看一下，或减少并发 |

### 8.3 面板连不上求解器

| 现象 | 原因 | 解决 |
|---|---|---|
| 容器日志报 `connection refused` | `api_url` 填了 `127.0.0.1` | 改成网桥网关 IP（第 6.1 步） |
| 容器日志报 `connection timed out` | 宿主防火墙挡了 | `iptables -L INPUT -n` 检查 |
| HTTP `401` | 两边 key 不一致 | `cat /root/.turnstile_solver_key` 和 `config.toml` 的 `api_key` 对比 |
| HTTP `404` | 走错分支 | 求解器对任意路径都直出，404 说明连到了别的服务，检查端口 |
| `solver http 4xx/5xx` | 求解器内部报错 | 看 `/root/turnstile_server.log` |

### 8.4 改了 config.toml 不生效

**99% 是 bind mount inode 问题**。用：

```bash
docker compose up -d --force-recreate
```

验证：

```bash
docker exec dsfree2api-dsfree2api-1 sed -n '/\[turnstile\]/,/^\[/p' /app/config.toml
```

如果容器里看到的还是旧内容，说明宿主的编辑方式换了 inode —— 用 `--force-recreate` 一定能解决。
（`docker compose restart` **不行**，它不重建容器。）

### 8.4.1 管理台提示 `write config.toml denied`

容器用户 `app(10001)` 对挂进去的 `config.toml` 没有写权限。

```bash
# 自查
docker exec dsfree2api-dsfree2api-1 sh -c 'test -w /app/config.toml && echo 可写 || echo 不可写'
# 修复
chown 10001:10001 /root/dsfree2api/config.toml
```

详见 [4.4 修 config.toml 写入权限](#44-修-configtoml-写入权限否则管理台存不了东西)。

### 8.4.2 管理台改了密码/Key，重启后又变回去

环境变量覆盖了 `config.toml`。`ADMIN_PASSWORD` / `API_KEYS` / `ADMIN_HOST` / `HOST` / `PORT`
只要在 `docker-compose.yml` 里设了（哪怕是默认值）就**永远优先**。
必须改 `docker-compose.yml` 或建 `.env`。详见 [4.3](#43-必改的两处--两个文件都要改)。

### 8.5 快速自检脚本

```bash
cat > /root/check_turnstile.sh <<'EOF'
#!/usr/bin/env bash
echo "== 1. Xvfb ==";        ps aux | grep -q "[X]vfb" && echo "  OK" || echo "  ✗ 未运行"
echo "== 2. 求解器进程 ==";  pgrep -f turnstile_server.py >/dev/null && echo "  OK" || echo "  ✗ 未运行"
echo "== 3. 求解器端口 ==";  ss -ltn | grep -q ":8899" && echo "  OK" || echo "  ✗ 未监听"
echo "== 4. health ==";      curl -s --max-time 5 http://127.0.0.1:8899/health || echo "  ✗ 无响应"
echo; echo "== 5. 容器状态 =="; docker ps --filter name=dsfree2api --format '  {{.Names}} {{.Status}}'
echo "== 6. 容器->宿主 =="
GW=$(docker inspect dsfree2api-dsfree2api-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$v.Gateway}}{{end}}')
echo "  网关=$GW"
docker exec dsfree2api-dsfree2api-1 wget -qO- --timeout=5 "http://$GW:8899/health" || echo "  ✗ 不通"
echo; echo "== 7. 面板 turnstile 配置 =="
docker exec dsfree2api-dsfree2api-1 sed -n '/\[turnstile\]/,/^\[/p' /app/config.toml | grep -E "enabled|api_url|provider"
echo; echo "== 8. config.toml 容器内可写（管理台保存用）=="
docker exec dsfree2api-dsfree2api-1 sh -c 'test -w /app/config.toml && echo "  OK 可写" || echo "  ✗ 不可写 → chown 10001:10001 config.toml"'
EOF
chmod +x /root/check_turnstile.sh
bash /root/check_turnstile.sh
```

---

## 9. 开机自启（systemd）

nohup 起的进程**重启机器就没了**。生产环境用 systemd 托管。

### 9.1 先确认 Xvfb 由谁托管（关键，别急着建 unit）

**先查，再决定。** 很多机器（尤其是已经跑过浏览器自动化的）**本来就有**一个 Xvfb 单元：

```bash
systemctl list-units --all --no-pager | grep -i xvfb
```

会出现两种结果：

**情况 A：已经有单元了**（比如 `xvfb-browser.service  loaded active running`）

→ **直接用现成的，不要新建。** 跳过 9.1，直接做 9.2。

> ⚠️ **这里是最容易踩的坑**：如果你在已有 Xvfb 的机器上再建一个 `xvfb.service`，
> 它会因为抢不到 `:1` 而疯狂崩溃重启，日志刷屏：
> ```
> Fatal server error:
> (EE) Server is already active for display 1
>         If this server is no longer running, remove /tmp/.X1-lock
> ```
> 而且 `systemctl is-active` 会一直显示 `activating`（在无限重启），看着像"卡住了"。
> 如果你已经建了，删掉它：
> ```bash
> systemctl stop xvfb.service; systemctl disable xvfb.service
> rm -f /etc/systemd/system/xvfb.service
> systemctl daemon-reload
> ```

**情况 B：一个都没有**

→ 才需要自己建。保存为 `/etc/systemd/system/xvfb.service`：

```ini
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
```

### 9.2 求解器

保存为 `/etc/systemd/system/turnstile-solver.service`：

```ini
[Unit]
Description=Turnstile Solver Service
After=network.target
# 不要写 Requires=xvfb.service：Xvfb 的单元名因机器而异
# （有的叫 xvfb-browser.service），写死会导致 "Unit xvfb.service not found" 起不来。
# Xvfb 本来就在跑，求解器只需要 DISPLAY=:1 指向它即可。

[Service]
Type=simple
Environment=DISPLAY=:1
Environment=LISTEN_HOST=0.0.0.0
Environment=LISTEN_PORT=8899
Environment=MAIN_URL=https://deepseek.de
EnvironmentFile=-/root/.turnstile_solver_key.env
ExecStart=/usr/bin/python3 -u /root/turnstile_server.py
Restart=always
RestartSec=5
StandardOutput=append:/root/turnstile_server.log
StandardError=append:/root/turnstile_server.log

[Install]
WantedBy=multi-user.target
```

创建环境变量文件（systemd 不能直接读裸文件）：

```bash
echo "API_KEY=$(cat /root/.turnstile_solver_key)" > /root/.turnstile_solver_key.env
chmod 600 /root/.turnstile_solver_key.env
```

### 9.3 启用

```bash
pkill -f turnstile_server.py     # 先停掉 nohup 起的
systemctl daemon-reload

# 只启用求解器。Xvfb 只有在 9.1 情况 B（本来没有）时才需要 enable。
systemctl enable --now turnstile-solver.service

systemctl status turnstile-solver.service --no-pager
journalctl -u turnstile-solver -f        # 实时日志
```

### 9.4 常用操作

```bash
systemctl restart turnstile-solver     # 重启求解器
systemctl is-enabled turnstile-solver  # 确认开机自启
systemctl is-active turnstile-solver   # 确认在跑
```

### 9.5 验证（必须做，别省）

```bash
# 1) 状态应为 active / enabled
systemctl is-active turnstile-solver.service
systemctl is-enabled turnstile-solver.service

# 2) 端口在听
ss -ltn | grep 8899

# 3) 健康检查
curl -s http://127.0.0.1:8899/health

# 4) 扛重启：重启后应自动回来（Restart=always + enabled 生效）
systemctl restart turnstile-solver.service
sleep 8
systemctl is-active turnstile-solver.service
curl -s http://127.0.0.1:8899/health
```

**最后一步别漏 —— 强制走一次真实求解**，确认整条链路没被 systemd 改动搞坏：

```bash
# 重启面板容器清掉内存里的 cookie 缓存
docker restart dsfree2api-dsfree2api-1
sleep 10

: > /root/turnstile_server.log      # 清空日志
curl -s --max-time 240 -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer 你的api_keys" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash-de","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'

cat /root/turnstile_server.log      # 必须出现来自容器 IP 的 POST
```

期望（实测约 28 秒）：

```
[18:37:22] [SOLVER] 开始求解 https://deepseek.de
[18:37:49] [SOLVER] 成功 (第 1 次，耗时 27.1s)，token 长度 752
[18:37:49] [HTTP] 172.20.0.2 "POST /turnstile/sync HTTP/1.1" 200 -
```

> 如果不重启容器就直接测，会 2 秒返回且**日志为空** —— 那是面板复用了 3 小时内的
> cookie 缓存，**没走求解器**。这不代表失败，但也不构成验证。想验证就必须先清缓存。

### 9.6 加健康检查看门狗（**强烈建议，别省**）

#### 为什么必须加

求解器会**静默挂死**：进程还活着，但卡在内核 `D` 状态（不可中断 I/O），端口还在 LISTEN
但不接受连接。这时：

- `systemctl is-active` → 仍然显示 **active**（进程没退出）
- `Restart=always` → **不会触发**（它只管进程退出，不管进程假活）

结果就是面板一直超时，你以为服务在跑，其实早就死了。

**实测症状**（用 `ss` 看，`LISTEN` 后面那个数字不是 0 就是有连接堆积）：

```
LISTEN 1      5      0.0.0.0:8899      ← Recv-Q = 1，连接没人接
```

进程状态：

```bash
ps -o pid,stat,etime,cmd -p <PID>
#   PID STAT     ELAPSED CMD
# 2005009 Dsl     09:11:50 /usr/bin/python3 -u /root/turnstile_server.py
#         ^^^ D = 不可中断睡眠
```

内核栈：

```bash
cat /proc/<PID>/wchan
# folio_wait_bit_common      ← 在等内存页（swap 换入）
```

**诱因**：内存压力大时 Chrome + Python 被换出到 swap，swap 快满（实测 512M 里用了 510M），
需要换回来时卡住。LXC 容器里 `load average` 显示的是**宿主机**负载，不代表容器自身。

#### 关于 swap：容器内改不了，别白费功夫

**LXC 容器里无法管理 swap**，`swapon` 和 `swapoff` 都会被内核拒绝，即使有 `CAP_SYS_ADMIN`：

```bash
swapon /swapfile   # swapon failed: Operation not permitted
swapoff -a         # swapoff: Not superuser
```

`zram` / `zswap` 同样要 `swapon`，一样不行。**swap 限额只能在 Proxmox 宿主上改**：

```bash
# 在 PVE 宿主上
pct list                              # 找 hostname 对应的 CTID
pct set <CTID> --swap 8192            # 改成 8192MB（= RAM 大小）
pct reboot <CTID>
```

宿主 swap 本身不够的话得先在宿主建（ZFS 根上要用 zvol，不能用普通 swapfile）。

**如果你是租的 VPS、拿不到 PVE 宿主权限**（很常见），那就只能在容器内做这些兜底：

1. **让求解器彻底不碰 swap** —— `MemorySwapMax=0`。这样它永远不会被换出，
   也就永远不会卡在 `folio_wait_bit_common`。内存超限时被 OOM 杀掉，
   systemd 5 秒后拉起来，比假死好得多。
2. **加看门狗**（9.6 前面那节），兜住其他原因导致的假死。
3. **降内存压力**：`docker stats --no-stream` 看哪个容器最占；实测 `jlesage/firefox`
   这类手动浏览器容器常占 300MB RSS + 140MB swap，不用就停掉。

> 一个反直觉的现象：可能 **RAM 很空但 swap 是满的**（实测 8G 只用 1.2G，swap 却 510/512）。
> 因为那些页是早先内存紧张时被换出去的，之后没人访问就一直躺在 swap 里 ——
> 内核不会主动把它们换回来。正常情况下 `swapoff -a && swapon -a` 就能清空，
> 但容器内执行不了。


#### 装看门狗

**第一步：健康检查脚本** `/root/turnstile_healthcheck.sh`

```bash
cat > /root/turnstile_healthcheck.sh <<'SCRIPT'
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
SCRIPT
chmod +x /root/turnstile_healthcheck.sh
```

**第二步：两个单元**（`/etc/systemd/system/` 下）

`turnstile-healthcheck.service`：

```ini
[Unit]
Description=Turnstile solver healthcheck

[Service]
Type=oneshot
ExecStart=/root/turnstile_healthcheck.sh
```

`turnstile-healthcheck.timer`：

```ini
[Unit]
Description=Run turnstile solver healthcheck periodically

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=30s
Unit=turnstile-healthcheck.service

[Install]
WantedBy=timers.target
```

**第三步：启用**

```bash
systemctl daemon-reload
systemctl enable --now turnstile-healthcheck.timer
systemctl list-timers | grep turnstile
```

**第四步：实测故障恢复**（别只看它"启用了"，要真的验证能救活）

```bash
# 模拟故障
systemctl stop turnstile-solver.service
sleep 2
curl -s --max-time 5 http://127.0.0.1:8899/health || echo "无响应（预期）"

# 第一次：只记数
bash /root/turnstile_healthcheck.sh
# 2026-10-08 03:54:30 health 失败第 1 次

# 第二次：触发重启
bash /root/turnstile_healthcheck.sh
# 2026-10-08 03:54:30 health 失败第 2 次
# 2026-10-08 03:54:30 连续 2 次失败，重启 turnstile-solver.service
# 2026-10-08 03:54:36 重启后恢复 ✓

systemctl is-active turnstile-solver.service   # active
curl -s http://127.0.0.1:8899/health           # ok
```

#### 同时给求解器加内存硬顶

宁可被 OOM 杀掉（systemd 会自动拉起来），也不要卡在 swap 里假死。
在 `turnstile-solver.service` 的 `[Service]` 段加：

```ini
# Chrome 实测峰值约 1.7G，给足余量但设硬顶
MemoryHigh=2G
MemoryMax=3G
MemorySwapMax=256M
```

改完 `systemctl daemon-reload && systemctl restart turnstile-solver.service`，
用 `systemctl show turnstile-solver.service -p MemoryMax -p MemorySwapMax` 确认生效。


---

## 附录 A：求解器协议详解

给想自己用别的语言实现的人。源码位置：`dsfree2api/internal/turnstile/solver.go` 的 `solveTokenAPI()`。

### 重要：不是 CapSolver 的两段式

**不要**去实现 `createTask` + `getTaskResult`。dsfree2api 只发**一个同步 POST**，
直接打你配置的 `api_url` 原样地址，**不追加任何路径**。

### 请求

```
POST {api_url}
Content-Type: application/json
Authorization: Bearer {api_key}

{
  "url": "https://deepseek.de",
  "sitekey": "0x4AAAAAADlLZ3ljqZP6cQwq",
  "action": "chat",
  "cdata": "",
  "timeoutSeconds": 90
}
```

### 应答

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": { "token": "1.xxxxx.yyyyy.zzzzz" }
}
```

### 源码里的三条判定（任一条不过就算失败）

```go
if out.ErrorID != 0 || (out.Status != "" && out.Status != "ready") {
    // 失败
} else if out.Solution.Token == "" {
    // 失败：求解器没返回 token
}
```

1. `errorId != 0` → 失败
2. `status` 非空且 ≠ `"ready"` → 失败
3. `solution.token` 为空 → 失败

### 其他行为

| 项 | 说明 |
|---|---|
| `solution.userAgent` | **被忽略**。面板校验时用的是 `[upstream].user_agent` |
| HTTP 超时 | `timeout_seconds + 30` 秒（默认 120s），求解器必须在这个预算内返回 |
| 重试 | 面板自己重试 `retries`(5) 次，退避 `retry_backoff_seconds`(1.5) |
| 哪些 HTTP 码会重试 | 408 / 429 / 502 / 503 / 504 会重试；其他 4xx 直接失败（不浪费重试） |
| 请求走的网络 | 走面板配置的代理（`[proxy].url`），所以求解器的出口 IP 要和它一致 |

### 完整调用链

```
refreshLocked()
  → solveToken()              按 provider 选方式
  → solveTokenAPI()           POST 到你的 api_url，拿 token
  → GET  site.BaseURL + "/"   预热页面（模拟浏览器行为）
  → verifyToken()             POST token 到 site.AJAXURL
  → 缓存 cookie（cookie_ttl_seconds = 3 小时）
```

---

## 附录 B：七个必踩的坑

按踩坑概率排序。教学时建议**逐个演示反例**，印象最深。

### 坑 1：容器里填 `127.0.0.1`（概率最高）

容器内的 `127.0.0.1` 是容器自己，不是宿主。必须用**网桥网关 IP**。

```bash
docker inspect dsfree2api-dsfree2api-1 \
  --format '{{range $k,$v := .NetworkSettings.Networks}}网关={{$v.Gateway}}{{end}}'
```

### 坑 2：改完 config.toml 不生效

文件级 bind mount + 原子替换编辑 = 换 inode = 容器看到旧文件。
必须 `docker compose up -d --force-recreate`，`restart` 不行。

### 坑 3：`DISPLAY` 没设

headed 浏览器需要 X 显示器。SSH 里手起服务时 `DISPLAY` 是空的。

```python
os.environ.setdefault("DISPLAY", os.environ.get("BROWSER_DISPLAY") or ":1")
```

### 坑 4：死等固定秒数拿到空 token

`uc_gui_click_captcha()` 后 `time.sleep(6)` 常常不够 widget 出 token，第一次尝试白跑。

**实测对比**：

| 方案 | 单次耗时 |
|---|---|
| `time.sleep(6)` 死等 | 68 秒（两次尝试，第一次空手而归） |
| 轮询隐藏 input（25s 上限） | **23 秒**（一次成功） |

```python
while time.time() < deadline:
    v = sb.get_attribute('input[name="cf-turnstile-response"]', "value") or ""
    if v.strip():
        token = v.strip(); break
    time.sleep(1.0)
```

### 坑 5：鉴权头认错

dsfree2api 发的是标准 `Authorization: Bearer` 头（Key 跟在后面），
**不是**把 `clientKey` 放在 body 里。自己实现求解器时必须认这个头。

### 坑 6：systemd 里重复建 Xvfb 单元

机器上**已经有** Xvfb 单元（`xvfb-browser.service` 之类）时，再建一个 `xvfb.service`
会抢不到 `:1`，进入无限崩溃重启：

```
(EE) Fatal server error:
(EE) Server is already active for display 1
```

症状是 `systemctl is-active xvfb.service` 永远显示 `activating`，日志每 3 秒刷一次。

**建 unit 之前先查：**
```bash
systemctl list-units --all --no-pager | grep -i xvfb
```

已经有就别建。同理，求解器的 `[Unit]` 里**不要**写 `Requires=xvfb.service`
（单元名因机器而异，写死会报 `Unit xvfb.service not found` 起不来）。

### 坑 7：服务"假活"——进程活着但卡死

进程卡在 `D` 状态时，`systemctl is-active` 显示 `active`，`Restart=always` 也不会触发，
面板一直超时却看不出服务有问题。

**两个特征**（出现任一就说明假死）：

```bash
ss -ltnp | grep 8899     # LISTEN 后面不是 0（有连接堆积，没人接）
ps -o stat= -p <PID>     # 以 D 开头（不可中断睡眠）
```

**必须靠外部看门狗解决** —— 见 [9.6 加健康检查看门狗](#96-加健康检查看门狗强烈建议别省)。
systemd 自身没有 HTTP 健康检查能力，`Restart=always` 只管进程退出，管不了假活。

---

## 附录 C：三个增强补丁（轮询 / 预换 cookie / 后台预热）

> 这一节是**对 dsfree2api 源码的改造**，不是原项目自带的功能。
> 补丁文件：`dsfree2api-prewarm.patch`（4 个文件，+309 行）

### C.1 为什么需要

dsfree2api 原版的行为有三个痛点：

1. **总是优先用你请求的那个站点**。你请求 `deepseek-v4-flash-de`，它就死磕 de，
   直到 de 的额度烧干才 failover。实测 `de` 被打了 21 次、`es` 4 次、`fr` **0 次**。
2. **额度只在报错后才补救**。`RotateGuest` 只在请求撞上 `quota exhausted` 之后才触发，
   所以每个"额度用尽"都要先浪费一次失败请求。
3. **会话只在请求时才刷新**。Turnstile 凭证和访客身份都是懒加载的，
   第一个请求要现付一次求解成本（约 25 秒）。

### C.2 改造内容

| 文件 | 改动 |
|---|---|
| `internal/turnstile/solver.go` | 新增 `SessionExpiresAt()`：报出会话剩余时效（原来只能查"有没有"） |
| `internal/upstream/client.go` | `rr` 计数器 + `rotateCandidates()` 轮询；`quotaRemaining` 额度缓存 + `sortLowQuotaLast()` 请求时路由；`prewarmTrigger` 即时唤醒通道 |
| `internal/upstream/prewarm.go` | **新增**：后台预热循环（额度基线 + 时效基线 + 请求触发） |
| `cmd/dsfree2api/main.go` | 启动预热、退出时优雅停止 |
| `docker-compose.yml` | 暴露 6 个 `PREWARM_*` 环境变量 |

**轮询（需求 1 的一半）** —— 在候选列表排序前插入一次旋转：

```go
candidates = c.rotateCandidates(candidates)   // 每次请求从不同站点开始
candidates = c.sortQuotaCoolLast(candidates)  // 额度冷却的仍然排最后
candidates = c.sortLowQuotaLast(candidates)   // 跌到基线的排最后
```

**请求时额度判断（需求 1 的另一半）** —— `sortLowQuotaLast()` 只读内存缓存，
不发起网络请求，所以不给热路径加延迟；同时把被换下的站点推给后台：

```go
if n, seen := c.quotaRemainingFor(m.Site); seen && n <= c.minRemaining {
    low = append(low, id)
    c.notifyLowQuota(m.Site)   // 立刻唤醒后台刷新，不等下一轮巡检
    continue
}
```

```go
func (c *Client) rotateCandidates(ids []string) []string {
	if len(ids) < 2 {
		return ids
	}
	off := int(atomic.AddUint64(&c.rr, 1) % uint64(len(ids)))
	out := make([]string, 0, len(ids))
	out = append(out, ids[off:]...)
	out = append(out, ids[:off]...)
	return out
}
```

**后台预热（需求 2 + 3）** —— `prewarm.go` 每 `PREWARM_INTERVAL_SECONDS` 跑一轮，
对每个启用的站点依次：

1. 没有可用会话 → 主动 `refreshSession`（跑一次 Turnstile 求解），把凭证预热好
2. 查余额 `FetchBalance`
3. 剩余比例低于 `PREWARM_THRESHOLD` → **提前** `RotateGuest` 换访客身份，
   并 `clearQuotaCool` 解除冷却

它和真实请求共用同一个并发闸门（`acquire`/`release`），不会把站点打爆。

### C.3 两条基线与环境变量

设计目标是**客户端无感**，靠两条基线分别覆盖工作与闲置两种场景：

| 场景 | 基线 | 触发行为 |
|---|---|---|
| **工作时** | 额度 `PREWARM_MIN_REMAINING`（默认 10000，即用满 20000） | 请求进来时用**缓存**额度判断，跌到基线的站点被排到最后（请求落到健康镜像上），同时立刻唤醒后台换访客身份、把额度重置回满 |
| **闲置时** | 时效 `PREWARM_REFRESH_BEFORE_MINUTES`（默认 30） | 会话存活满 `cookie_ttl_seconds - 30min`（默认 180-30 = **150 分钟**）就主动刷新，不等过期后被第一个请求现付求解成本 |

```yaml
PREWARM_ENABLED: "true"                  # 总开关
PREWARM_INTERVAL_SECONDS: "60"           # 后台巡检间隔
PREWARM_MIN_REMAINING: "10000"           # 额度绝对值基线（设 0 = 只用比例）
PREWARM_THRESHOLD: "0.20"                # 剩余比例基线：剩余降到 20%（用满 80%）就刷新，
                                         # 站点额度是 10000 还是 30000 都适用。
                                         # 与上面那条是「或」关系，谁先到算谁。
PREWARM_REFRESH_BEFORE_MINUTES: "30"     # 时效基线提前量（闲置时）
PREWARM_START_DELAY_SECONDS: "15"        # 启动后延迟，避免和冷启动抢资源
```

**关键设计：请求路径不做网络查询。** 额度值缓存在内存里（由后台巡检和请求后的余额探测回填），
`sortLowQuotaLast()` 只读缓存 —— 所以"请求时判断额度"是**零延迟**的，不会给热路径加一次
balance 往返。这就是"无感"能成立的原因。

### C.4 应用补丁

```bash
cd /root/dsfree2api
git apply /root/dsfree2api-prewarm.patch      # 打补丁
docker compose build                          # 重新编译
docker compose up -d --force-recreate         # 部署
```

> 上游更新后如果冲突，用 `git apply --3way` 或手动改那 3 个文件。

### C.5 实测结果

**① 启动预热（无需任何请求，纯后台）**

```
prewarm started interval=1m0s min_remaining=10000 refresh_before=30m0s start_delay=15s
prewarm: no session, refreshing site=de
prewarm: session refreshed site=de                     ← 25.4s
prewarm: balance site=de remaining=30000 limit=30000 ratio=1.000 low=false
prewarm: session refreshed site=es                     ← 26.3s
prewarm: balance site=es remaining=30000 limit=30000 ratio=1.000 low=false
prewarm: session refreshed site=fr                     ← 25.6s
prewarm: balance site=fr remaining=30000 limit=30000 ratio=1.000 low=false
```

三个站点从 `0 / 0 / 29925` 全部变成 **30000 满额**。

**② 工作时额度基线 —— 请求触发即时切换**

测试值 `PREWARM_MIN_REMAINING=30000`（必然触发）、`PREWARM_INTERVAL_SECONDS=3600`（隔离出请求触发路径）：

```
prewarm: request-time trigger, refreshing now site=fr      ← 请求进来，发现 fr 在基线
prewarm: quota at baseline, rotating visitor identity site=fr remaining=30000 baseline=30000
prewarm: rotated visitor identity site=fr
prewarm: quota restored site=fr remaining=30000 limit=30000
prewarm: request-time trigger, refreshing now site=de
prewarm: rotated visitor identity site=de
prewarm: quota restored site=de remaining=30000 limit=30000
```

请求本身 **1.27 秒返回 200**，切换与刷新完全在后台完成 —— 这就是"客户无感"。

**③ 闲置时时效基线 —— 到期前主动刷新**

测试值 `PREWARM_REFRESH_BEFORE_MINUTES=200`（大于 180 分钟 TTL，必然触发）：

```
prewarm started interval=30s min_remaining=10000 refresh_before=3h20m0s start_delay=5s
prewarm: session nearing TTL, refreshing site=de left=2h58m36s
prewarm: session refreshed site=de
prewarm: session nearing TTL, refreshing site=es left=2h59m2s
prewarm: session refreshed site=es
prewarm: session nearing TTL, refreshing site=fr left=2h59m27s
prewarm: session refreshed site=fr
```

生产值 `refresh_before=30m` 时即为**存活满 150 分钟自动刷新**。

**④ 轮询分布**

连发 6 个请求 → `de=2  es=1  fr=3`，0 错误，全部约 1 秒返回。

> 不是精确 2/2/2 是因为 `alternates()` 遍历 Go map，顺序本身随机，
> 在轮询之上又叠了一层随机 —— 反而更均匀。关键是**没有站点被压垮**
> （改造前是 `de=21  es=4  fr=0`）。

**⑤ 请求速度**

预热后所有请求 **约 1 秒**返回（原来第一个请求要 25 秒，因为要现跑求解）。

### C.6 注意事项

- 预热会对**所有启用的站点**都做一次求解，启动时约 75 秒（3 站 × 25s）。
  站点越多启动越久，可以用 `PREWARM_START_DELAY_SECONDS` 控制开始时机。
- **基线别设得太激进**。额度基线设成"用满就换"（比如 30000）会让每个请求都触发换身份 ——
  同一个出口 IP 短时间造太多访客身份，上游可能不认账（实测第一次轮换有时无效，第二次才成功）。
  默认 10000（用满 20000）留了 1 万 token 的缓冲，比较稳。
- 时效基线的提前量要**小于** `cookie_ttl_seconds`，否则每轮巡检都会刷新（等于持续求解）。
- 会话刷新会复用求解器的 token 缓存（180 秒内），所以短时间内重复刷新很快（0.3 秒），
  不是每次都 25 秒。这是有意的 —— 目的只是拿新 cookie，token 能过校验即可。
- 额度缓存由「后台巡检」+「请求后的余额探测」共同回填，所以 `PREWARM_INTERVAL_SECONDS`
  决定请求时路由判断的**新鲜度**。设太长（比如 3600）会让缓存过期，路由判断退化成只靠轮询。

### C.7 顺带修掉的一个 bug：非流式应答 `role` 为空

**症状**：非流式请求返回的 `message.role` 是**空字符串**而不是 `"assistant"`。

```json
"message": { "role": "", "content": "Hi there friend" }
```

**后果**：严格的 OpenAI 客户端（Cline / Cursor / Continue 等）会因为 role 非法直接报错 ——
表现就是"一调用工具就歇菜"。

**根因**（`internal/api/chat.go` 的 `buildCompletion()`）：

```go
message := openai.ChoiceMessage{Content: body}   // Role 从来没赋值
```

而 `ChoiceMessage.Role` 没有 `omitempty`：

```go
type ChoiceMessage struct {
	Role      string     `json:"role"`      // ← 零值 "" 会原样输出
	Content   string     `json:"content"`
	ToolCalls []ToolCall `json:"tool_calls,omitempty"`
}
```

流式路径（`chat.go` 首个 chunk）是设了 `Role: "assistant"` 的，所以**只有非流式请求中招**。

**修复**：

```go
message := openai.ChoiceMessage{Role: "assistant", Content: body}
```

**验证**：修复后 `role` 为 `"assistant"`，非流式 + 工具、非流式普通、流式 + 工具、
完整工具往返、超大参数工具调用五种场景全部通过。

---

## 附：本文档对应的实测环境

| 项 | 值 |
|---|---|
| 系统 | Debian GNU/Linux 13 (trixie) x86_64 |
| Python | 3.13.5 |
| seleniumbase | 4.53.7 |
| Google Chrome | 152.0.7977.82 |
| Docker | Engine 29.x + Compose v5.x |
| dsfree2api | v0.6.0 (commit 4cc4504) |
| Xvfb | `:1 -screen 0 1440x900x24 -ac +extension GLX +render -noreset` |
| 单次求解耗时 | 约 23-25 秒 |
| cookie 缓存 | 3 小时 |
