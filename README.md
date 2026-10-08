# Turnstile Solver + dsfree2api 一键部署

给 [dsfree2api](https://github.com/nyoungo/dsfree2api) 配一个**自建的 Cloudflare Turnstile 求解器**，
并附上三个增强补丁（站点轮询 / 双基线后台预热 / 非流式 `role` 修复）。

不需要买第三方打码服务 —— 用真浏览器过验证，本地跑，100% 可控。

## 一键部署

在干净的 Debian 12/13 或 Ubuntu 22.04+ VPS 上：

```bash
curl -fsSL https://raw.githubusercontent.com/kuisa/dsfree2api-mod/main/deploy.sh -o deploy.sh
bash deploy.sh
```

或者克隆下来跑（会优先用仓库里的文件，不走网络）：

```bash
git clone https://github.com/kuisa/dsfree2api-mod.git
cd dsfree2api-mod
bash deploy.sh
```

**非交互（适合批量 / 脚本化）**：

```bash
API_KEY=sk-你的下游key ADMIN_PASSWORD=你的管理台密码 bash deploy.sh -y
```

**只检查不安装**：

```bash
bash deploy.sh --dry-run
```

部署完成后会打印 API 地址、API Key、管理台密码。

## 脚本做了什么

| 步骤 | 内容 |
|---|---|
| 1 | 前置检查（系统 / 架构 / root） |
| 2 | 装依赖：Docker、Xvfb、xdotool、scrot、Chrome、seleniumbase |
| 3 | 部署求解器 + systemd 托管 + 看门狗 + 内存硬顶 |
| 4 | 部署 dsfree2api（固定 commit + 打补丁 + 自动探测网桥网关接线） |
| 5 | 端到端验证（真发一次对话，确认走了求解器） |
| 6 | 打印凭据和常用命令 |

**幂等**：重复执行不会重复安装，会复用已有的 Docker / Xvfb / 密钥。

## 仓库结构

```
.
├── deploy.sh                      # 一键部署脚本
├── solver/
│   ├── turnstile_server.py        # 求解器本体（单文件）
│   ├── healthcheck.sh             # 看门狗（兜住"进程活着但卡死"）
│   └── start.sh                   # 手动启动（systemd 环境下会自动转交）
├── scripts/
│   ├── check.sh                   # 8 项自检
│   └── proxmox-add-swap.sh        # PVE 宿主上给容器加 swap（可选）
├── patches/
│   └── dsfree2api-prewarm.patch   # dsfree2api 增强补丁
└── docs/
    └── SETUP.zh-CN.md             # 完整教学手册（原理 + 手把手 + 七个坑）
```

## 求解器协议

dsfree2api 的「方式 1：求解服务 API」是**单个同步 POST**，不是 CapSolver 的两段式：

```
POST {api_url}
Content-Type: application/json
Authorization: Bearer {api_key}

{"url":"https://deepseek.de","sitekey":"0x4AAA...","action":"chat","cdata":"","timeoutSeconds":90}
```

应答必须满足（源码三条判断，任一条不过就算失败）：

```json
{"errorId":0,"status":"ready","solution":{"token":"1.xxx"}}
```

- `errorId != 0` → 失败
- `status` 非空且 ≠ `"ready"` → 失败
- `solution.token` 为空 → 失败

求解器同时兼容两段式（`createTask`/`getTaskResult`）和任意路径直出，方便接别的面板。

## 三个增强补丁

### 1. 站点轮询

原版总是优先用你请求的那个站点，直到它额度烧干才 failover。实测 `de` 被打 21 次、`es` 4 次、
`fr` **0 次**。补丁加了 `rotateCandidates()`，让每次请求从不同站点开始。

### 2. 双基线后台预热（客户端无感）

| 场景 | 基线 | 行为 |
|---|---|---|
| 工作时 | 额度 `PREWARM_MIN_REMAINING`（默认 10000）**或**剩余比例 `PREWARM_THRESHOLD`（默认 0.20，即用满 80%）—— 谁先到算谁 | 请求时用**缓存**额度判断（零延迟），低额站点排最后；同时唤醒后台立刻换访客身份重置额度 |
| 闲置时 | 时效 `PREWARM_REFRESH_BEFORE_MINUTES`（默认 30） | 会话存活满 `cookie_ttl - 30min`（默认 150 分钟）主动刷新，不等过期 |

实测效果：三站从 `0/0/29925` 后台预热成 `30000/30000/30000`；请求从 25 秒降到 **1 秒**。

### 3. 非流式应答 `role` 为空修复

`buildCompletion()` 漏了赋 `Role`，非流式应答返回 `"role": ""`，
严格的 OpenAI 客户端（Cline/Cursor/Continue）会直接报错 —— 表现就是"一调用工具就歇菜"。

## 联网搜索代理（可选但推荐）

**背景**：上游站点（deepseek.de）**关掉了联网搜索**。从站点配置里挖出来的：

```json
"allowWebSearchTool": false,      ← 站点管理员关的，服务端控制
"webToggleDefaultOn": false
```

网关改不了这个。但**模型配合工具调用** —— 给它一个 `web_search` 工具定义，它会主动要求搜索。

所以附了一个搜索代理，把「谁去搜」这一步补上：

```
客户端 → 搜索代理(:8002) → dsfree2api(:8000) → 上游站点
            ↓ 拦截 web_search 工具调用
            ↓ Bing / DuckDuckGo / Wikipedia 搜索
            ↓ 回灌结果，去掉工具再请求一轮（真流式）
```

**客户端只要把 base_url 指到 `:8002`，就自动有联网搜索**，不用改任何其他配置。

### 搜索后端：用环境变量换，不用改代码

```bash
sudo systemctl edit search-proxy      # 或者直接改 /etc/systemd/system/search-proxy.service
# 加一行：
Environment=SEARCH_BACKENDS=tavily,bing,duckduckgo,wikipedia
sudo systemctl daemon-reload && sudo systemctl restart search-proxy
```

逗号分隔，**靠前的先用**，失败自动降级到下一个。

| 后端名 | 要不要 key | 说明 |
|---|---|---|
| `bing` | 免费 | 默认主力，中文结果好 |
| `duckduckgo` | 免费 | 备用。**必须用 GET**，POST 会被反爬拦成 202 |
| `wikipedia` | 免费 | 兜底，只覆盖百科类 |
| `tavily` | 要 `TAVILY_API_KEY` | 质量最好，免费 1000 次/月 |
| `brave` | 要 `BRAVE_API_KEY` | 免费 2000 次/月 |
| `serper` | 要 `SERPER_API_KEY` | Google 结果，免费 2500 次 |

付费后端**没配 key 会自动跳过**，所以你可以把 `tavily` 放在最前面 —— 有 key 就用它，
没 key 自动落到 `bing`，不用改配置。

要填 key 就在同一个 unit 里加：

```ini
Environment=TAVILY_API_KEY=tvly-xxxxxxxx
```

验证当前生效的后端：

```bash
curl -s http://127.0.0.1:8002/health
# {"backends": ["bing", "duckduckgo", "wikipedia"], ...}
```

启动日志里也会打印：

```
[PROXY]   搜索后端  : bing → duckduckgo → wikipedia
```

> **注意**：如果你的出口 IP 是 WARP / 数据中心 IP，DuckDuckGo 和多数公共 SearXNG
> 实例会拦（返回 "anomaly" / "not a bot" 页面）。Bing 目前不拦。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LISTEN_PORT` | `8002` | 代理端口 |
| `UPSTREAM_URL` | `http://127.0.0.1:8000` | 上游 dsfree2api |
| `API_KEY` | — | 调上游用的 key（部署脚本自动填） |
| `TOOL_NAME` | `web_search` | 工具名 |
| `AUTO_INJECT_TOOL` | `true` | 客户端没带工具时自动注入，实现无感 |
| `MULTI_HOP` | `false` | 是否允许多轮连续搜索。开启后最终答案会被上游缓冲 |
| `MAX_ROUNDS` | `3` | 最多几轮工具调用 |
| `SEARCH_RESULTS` | `5` | 每次取几条结果 |

### 实测

```
[PROXY] 第1轮搜索 '上海今天天气' → 5 条（bing）
[PROXY] 127.0.0.1 "POST /v1/chat/completions HTTP/1.1" 200 -

上海今天（2026年10月8日）：晴 [4]，约 17°C [4]，西南风小于3级 [4]，
空气质量优（AQI 32）[4]，湿度 70% [4]
```

流式正常（52 帧纯增量，无重复），全程约 3 秒。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_KEY` | 自动生成 | 下游调用 API 的 key |
| `ADMIN_PASSWORD` | 自动生成 | 管理台密码 |
| `SOLVER_KEY` | 自动生成 | 求解器自身 key |
| `SITEKEY` / `ACTION` | 见脚本 | 目标站点的 Turnstile 参数 |
| `BASE_DIR` | `/opt/turnstile-stack` | 安装目录 |
| `DSFREE_COMMIT` | `e44e960` | 补丁对应的版本（v0.6.3，别乱改）。**这个版本才含三个工具调用修复**（prose 前缀 JSON、DSML 标记、流式参数），老版本（≤v0.6.0）复杂工具调用会退化成纯文本 |
| `PREWARM_MIN_REMAINING` | `10000` | 额度绝对值基线（设 0 = 关闭这条，只用比例） |
| `PREWARM_THRESHOLD` | `0.20` | 剩余比例基线：剩余降到 limit 的 20%（= 用满 80%）就刷新。**站点额度是 10000 还是 30000 都适用** |
| `PREWARM_REFRESH_BEFORE_MINUTES` | `30` | 时效提前量 |
| `RAW_BASE` | 见脚本 | 脚本不在仓库里时的下载地址 |

## 自检与运维

```bash
bash /opt/turnstile-stack/scripts/check.sh   # 8 项自检

systemctl status turnstile-solver            # 求解器
systemctl status turnstile-healthcheck.timer # 看门狗
tail -f /root/turnstile_server.log           # 求解器日志
cd /opt/turnstile-stack/dsfree2api && docker compose logs -f
```

## 改 API Key / 管理台密码

**必须两处一起改**，否则搜索代理会拿旧 key 去调上游，返回 401。

```bash
cd /opt/turnstile-stack/dsfree2api

# ① 改 .env（compose 读这个，优先级高于 config.toml）
nano .env
#   API_KEYS=你的新key
#   ADMIN_PASSWORD=你的新密码

# ② config.toml 改成同样的值（管理台页面显示的是这个）
nano config.toml
#   [security] api_keys = ["你的新key"]
#   [admin]    password = "你的新密码"

# ③ 重建容器（restart 不重新解析 bind mount，改了不生效）
docker compose up -d --force-recreate

# ④ 同步 key 到搜索代理（这步最容易漏！）
KEY=$(grep '^API_KEYS=' /opt/turnstile-stack/dsfree2api/.env | cut -d= -f2-)
sed -i "s|^Environment=API_KEY=.*|Environment=API_KEY=$KEY|" \
  /etc/systemd/system/search-proxy.service
systemctl daemon-reload
systemctl restart search-proxy

# ⑤ 验证
curl -s -X POST http://127.0.0.1:8001/api/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"你的新密码"}'
# 期望 {"ok":true}

curl -s --max-time 180 -X POST http://127.0.0.1:8002/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash-de","messages":[{"role":"user","content":"说 ok"}],"max_tokens":20}'
```

**顺序很重要**：先重建容器（让 `:8000` 认新 key）→ 再改搜索代理 → 再重启代理。
反过来会让搜索代理拿新 key 去调还在用旧 key 的 `:8000`，照样 401。

> `/root/.turnstile_solver_key` 是求解器**内部**的 key，跟这两个不是一回事，不用改。

## 四个必踩的坑

1. **容器内不能填 `127.0.0.1`** —— 那是容器自己。必须用网桥网关 IP（脚本会自动探测）。
2. **改完 `config.toml` 必须 `docker compose up -d --force-recreate`** ——
   文件级 bind mount 用原子替换编辑会换 inode，`restart` 看不到新内容。
3. **`swapon`/`swapoff` 在 LXC 容器里被内核禁止** —— swap 只能在 PVE 宿主上改。
   容器内用 `MemorySwapMax=0` 让服务永不换出（脚本已配）。
4. **别重复建 Xvfb 单元** —— 机器上已有 Xvfb 时再建一个会抢不到 `:1` 无限崩溃重启。
   脚本会先探测再决定。

完整原理和手把手教学见 **[docs/SETUP.zh-CN.md](docs/SETUP.zh-CN.md)**。

## 环境要求

- Debian 12/13 或 Ubuntu 22.04+，**x86_64 优先**（ARM 用 Chromium，过验证率略低）
- 2 核 / 2GB 内存起步（Chrome 峰值约 1.7GB）
- 能访问 `dl.google.com` 和 `github.com`

## 许可

脚本和求解器代码可自由使用。`patches/` 下的补丁是对 dsfree2api 的修改，
请遵守原项目的许可。
