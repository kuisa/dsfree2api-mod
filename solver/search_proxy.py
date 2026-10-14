#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联网搜索代理 —— 给不支持联网的模型（如 dsfree2api 后面的 deepseek.de）加上搜索能力。

架构：
    客户端 → 本代理(:8002) → dsfree2api(:8000) → 上游站点
                ↓ 拦截 web_search 工具调用
                ↓ DuckDuckGo 搜索
                ↓ 把结果回灌，再请求一轮
                ↓ 把最终答案流式返回给客户端

客户端只要把 base_url 指到本代理即可，其他不用改。

运行: python3 search_proxy.py
依赖: 无（纯标准库）
"""

import os
import re
import sys
import json
import time
import gzip
import html as htmllib
import urllib.parse
import urllib.request
import urllib.error
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ================= 配置区域 =================
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8002"))

# 上游 dsfree2api 地址 + 下游 API Key
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.getenv("API_KEY", "")

# 代理自身的鉴权（留空 = 不校验，直接用上游的 key 也行）
PROXY_KEY = os.getenv("PROXY_KEY", "")

# 工具名（客户端如果自己带了这个名字的工具，就由代理接管）
TOOL_NAME = os.getenv("TOOL_NAME", "web_search")

# 客户端没带工具时，是否自动注入一个搜索工具（让所有客户端都无感获得搜索）
AUTO_INJECT_TOOL = os.getenv("AUTO_INJECT_TOOL", "true").lower() in ("1", "true", "yes", "on")

# 最多几轮工具调用（防死循环）
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "3"))
# 每次搜索取几条结果
SEARCH_RESULTS = int(os.getenv("SEARCH_RESULTS", "5"))
# 搜索/上游超时
SEARCH_TIMEOUT = int(os.getenv("SEARCH_TIMEOUT", "20"))
UPSTREAM_TIMEOUT = int(os.getenv("UPSTREAM_TIMEOUT", "300"))

# 上游免费站点对单条消息有约 10000 字符硬上限（无余额时）。超限会被 413 拒绝，
# 容器随即跨站重试（de→es→fr），单次请求从 2 秒拖到几十秒甚至 502。
# 这里在转发前把消息裁剪到上限以内；0 = 关闭裁剪（保持纯透传）。
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "9000"))
# 消息至少要留这么多字符（工具定义再多也不能把对话挤没）
MIN_MSG_CHARS = int(os.getenv("MIN_MSG_CHARS", "3000"))
# ===========================================

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] [PROXY] {msg}", flush=True)


# ── 消息裁剪：把请求压到上游字符上限以内 ──────────────────────
def _msg_chars(messages):
    """统计消息文本的字符数（上游按字符数判定超限）。"""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    t = part.get("text")
                    if isinstance(t, str):
                        total += len(t)
    return total


def _json_chars(obj):
    """对象序列化后的字符数（工具定义等非 content 字段用它计预算）。"""
    if not obj:
        return 0
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except Exception:
        return 0


def fit_tools(tools, budget):
    """工具定义超预算时逐级瘦身：折半描述 → 清空描述 → 丢弃尾部工具。
    清空描述后 name / 参数名 / 类型都还在，工具调用依然可用。
    返回 (tools, 是否裁剪过)。"""
    if not tools or _json_chars(tools) <= budget:
        return tools, False
    try:
        import copy
        out = copy.deepcopy(tools)
    except Exception:
        return tools, False

    def shrink(objs, min_len):
        for t in objs:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") or {}
            d = fn.get("description")
            if isinstance(d, str) and len(d) > min_len:
                fn["description"] = d[: max(min_len, len(d) // 2)]
            props = ((fn.get("parameters") or {}).get("properties")) or {}
            if isinstance(props, dict):
                for pv in props.values():
                    if isinstance(pv, dict):
                        pd = pv.get("description")
                        if isinstance(pd, str) and len(pd) > min_len:
                            pv["description"] = pd[: max(min_len, len(pd) // 2)]

    # 1) 逐轮折半描述
    for _ in range(4):
        if _json_chars(out) <= budget:
            return out, True
        shrink(out, 40)

    # 2) 清空所有描述（保留结构，工具调用仍可用）
    for t in out:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        if isinstance(fn.get("description"), str):
            fn["description"] = ""
        props = ((fn.get("parameters") or {}).get("properties")) or {}
        if isinstance(props, dict):
            for pv in props.values():
                if isinstance(pv, dict) and isinstance(pv.get("description"), str):
                    pv["description"] = ""
    if _json_chars(out) <= budget:
        return out, True

    # 2.5) 只留必填参数，去掉可选参数的 schema
    for t in out:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        params = fn.get("parameters")
        if not isinstance(params, dict):
            continue
        props = params.get("properties")
        req = params.get("required") or []
        if isinstance(props, dict) and req:
            params["properties"] = {k: v for k, v in props.items() if k in req}
    if _json_chars(out) <= budget:
        return out, True

    # 3) 仍超 → 从尾部丢弃，但 TOOL_NAME（web_search）永远优先保留
    keep, rest = [], []
    for t in out:
        name = ((t or {}).get("function") or {}).get("name") if isinstance(t, dict) else None
        (keep if name == TOOL_NAME else rest).append(t)
    for t in rest:
        if _json_chars(keep + [t]) > budget:
            continue          # 塞不下的跳过，但不影响已保留的
        keep.append(t)
    if not keep:
        keep = out[:1]        # 极端情况至少留一个
    return keep, True


def fit_messages(messages, limit=None):
    """把消息裁到 limit 字符以内：保留 system + 最新消息，从最旧的开始丢，
    仍超则从最长的一条开始截断。返回 (新消息列表, 是否裁剪过)。"""
    limit = MAX_INPUT_CHARS if limit is None else limit
    if limit <= 0 or not messages:
        return messages, False
    if _msg_chars(messages) <= limit:
        return messages, False

    msgs = list(messages)
    sys_msgs = [m for m in msgs if m.get("role") == "system"]
    rest = [m for m in msgs if m.get("role") != "system"]

    # 1) 丢最旧的非 system 消息（始终保留最后一条 = 当前提问）
    while len(rest) > 1 and _msg_chars(sys_msgs + rest) > limit:
        rest.pop(0)

    out = sys_msgs + rest

    # 2) 仍超 → 从最长的一条开始截断
    guard = 0
    while _msg_chars(out) > limit and guard < 50:
        guard += 1
        idx, best = -1, -1
        for i, m in enumerate(out):
            c = m.get("content")
            if isinstance(c, str) and len(c) > best:
                best, idx = len(c), i
        if idx < 0 or best <= 0:
            break
        excess = _msg_chars(out) - limit
        keep = max(200, best - excess - 60)
        out[idx]["content"] = out[idx]["content"][:keep] + "\n…（内容过长已截断）"

    return out, True


# ── HTTP 小工具 ───────────────────────────────────────────────
def http_request(url, data=None, headers=None, timeout=30, method=None):
    """返回 (status, headers, bytes)。不抛异常。"""
    hdrs = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        hdrs.update(headers)
    body = None
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
            return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        return e.code, dict(e.headers or {}), raw
    except Exception as e:
        return 0, {}, str(e).encode("utf-8")


# ── 搜索后端：DuckDuckGo（免费，无需 key）────────────────────
def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return htmllib.unescape(s).strip()


def _unwrap_ddg(href):
    """DDG 的结果链接是 //duckduckgo.com/l/?uddg=<编码后的真实URL>"""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        q = urllib.parse.urlparse(href).query
        real = urllib.parse.parse_qs(q).get("uddg")
        if real:
            return urllib.parse.unquote(real[0])
    except Exception:
        pass
    return href


def _decode_bing_url(href):
    """Bing 把真实 URL 放在 u=a1<base64> 里"""
    href = htmllib.unescape(href or "")
    m = re.search(r"[?&]u=a1([A-Za-z0-9_\-=%]+)", href)
    if m:
        b64 = m.group(1)
        try:
            import base64
            pad = "=" * (-len(b64) % 4)
            return base64.urlsafe_b64decode(b64 + pad).decode("utf-8", "ignore")
        except Exception:
            pass
    return href


def search_bing(query, n):
    """主后端：Bing。实测从这个 WARP 出口可直连（GET，不能用 POST）。"""
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "setlang": "zh-CN", "mkt": "zh-CN"})
    status, _, raw = http_request(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        timeout=SEARCH_TIMEOUT,
    )
    if status != 200:
        return []
    page = raw.decode("utf-8", "ignore")
    out = []
    items = re.findall(r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', page, re.S)
    for it in items:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', it, re.S)
        if not m:
            continue
        link = _decode_bing_url(m.group(1))
        title = _clean(m.group(2))
        sn = (re.search(r'<p class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', it, re.S)
              or re.search(r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', it, re.S)
              or re.search(r'<p[^>]*>(.*?)</p>', it, re.S))
        snippet = _clean(sn.group(1)) if sn else ""
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "snippet": snippet})
        if len(out) >= n:
            break
    return out


def search_ddg(query, n):
    """备用：DuckDuckGo HTML 版（必须 GET；POST 会被反爬拦成 202 anomaly）"""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query, "kl": "cn-zh"})
    status, _, raw = http_request(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        timeout=SEARCH_TIMEOUT,
    )
    if status != 200:
        return []
    page = raw.decode("utf-8", "ignore")
    out = []
    # 注意：实际 class 是 "links_main links_deep result__body"，不能带 class=" 前缀匹配
    for b in re.split(r"result__body", page)[1:]:
        m = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        link = _unwrap_ddg(htmllib.unescape(m.group(1)))
        title = _clean(m.group(2))
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b, re.S)
        snippet = _clean(sn.group(1)) if sn else ""
        if title and link.startswith("http"):
            out.append({"title": title, "url": link, "snippet": snippet})
        if len(out) >= n:
            break
    return out


def search_wikipedia(query, n):
    """最后兜底：Wikipedia 搜索 API（稳定，但只覆盖百科类内容）"""
    url = "https://zh.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query,
         "format": "json", "utf8": "1", "srlimit": str(n)})
    status, _, raw = http_request(url, headers={"Accept": "application/json"},
                                  timeout=SEARCH_TIMEOUT)
    if status != 200:
        return []
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    out = []
    for r in (data.get("query", {}).get("search") or [])[:n]:
        title = r.get("title") or ""
        if not title:
            continue
        out.append({
            "title": title,
            "url": "https://zh.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            "snippet": _clean(r.get("snippet") or ""),
        })
    return out


def search_tavily(query, n):
    """Tavily API（需 TAVILY_API_KEY，免费额度 1000 次/月，质量最好）"""
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        return []
    status, _, raw = http_request(
        "https://api.tavily.com/search",
        data={"api_key": key, "query": query, "max_results": n,
              "search_depth": "basic", "include_answer": False},
        headers={"Content-Type": "application/json"},
        timeout=SEARCH_TIMEOUT,
    )
    if status != 200:
        log(f"tavily http {status}: {raw[:150].decode('utf-8', 'ignore')}")
        return []
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    return [{"title": r.get("title") or "", "url": r.get("url") or "",
             "snippet": r.get("content") or ""}
            for r in (data.get("results") or [])[:n] if r.get("url")]


def search_brave(query, n):
    """Brave Search API（需 BRAVE_API_KEY，免费 2000 次/月）"""
    key = os.getenv("BRAVE_API_KEY", "").strip()
    if not key:
        return []
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": str(n)})
    status, _, raw = http_request(
        url,
        headers={"Accept": "application/json", "X-Subscription-Token": key},
        timeout=SEARCH_TIMEOUT,
    )
    if status != 200:
        log(f"brave http {status}: {raw[:150].decode('utf-8', 'ignore')}")
        return []
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    out = []
    for r in ((data.get("web") or {}).get("results") or [])[:n]:
        if not r.get("url"):
            continue
        out.append({"title": r.get("title") or "", "url": r["url"],
                    "snippet": r.get("description") or ""})
    return out


def search_serper(query, n):
    """Serper.dev（Google 结果，需 SERPER_API_KEY，免费 2500 次）"""
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        return []
    status, _, raw = http_request(
        "https://google.serper.dev/search",
        data={"q": query, "num": n},
        headers={"Content-Type": "application/json", "X-API-KEY": key},
        timeout=SEARCH_TIMEOUT,
    )
    if status != 200:
        log(f"serper http {status}: {raw[:150].decode('utf-8', 'ignore')}")
        return []
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return []
    return [{"title": r.get("title") or "", "url": r.get("link") or "",
             "snippet": r.get("snippet") or ""}
            for r in (data.get("organic") or [])[:n] if r.get("link")]


ALL_BACKENDS = {
    "bing": search_bing,
    "duckduckgo": search_ddg,
    "wikipedia": search_wikipedia,
    "tavily": search_tavily,
    "brave": search_brave,
    "serper": search_serper,
}

# 顺序和启用由 SEARCH_BACKENDS 控制（逗号分隔，靠前的先用）。
# 付费后端（tavily/brave/serper）没配对应 API Key 时会自动返回空、跳过。
# 例：SEARCH_BACKENDS=tavily,bing,duckduckgo,wikipedia
_sel = os.getenv("SEARCH_BACKENDS", "bing,duckduckgo,wikipedia")
BACKENDS = []
for _name in _sel.split(","):
    _name = _name.strip().lower()
    if _name in ALL_BACKENDS:
        BACKENDS.append((_name, ALL_BACKENDS[_name]))
    elif _name:
        log(f"警告: SEARCH_BACKENDS 里有未知后端 {_name!r}，已忽略")
if not BACKENDS:
    BACKENDS = [("bing", search_bing), ("duckduckgo", search_ddg)]


def web_search(query, n=None):
    """按 SEARCH_BACKENDS 的顺序试各后端，返回 (结果列表, 用的后端名)"""
    n = n or SEARCH_RESULTS
    for name, fn in BACKENDS:
        try:
            r = fn(query, n)
            if r:
                return r, name
            log(f"搜索后端 {name} 无结果: {query!r}")
        except Exception as e:
            log(f"搜索后端 {name} 异常: {e}")
    return [], "none"


def format_results(results, query):
    """把搜索结果拼成给模型看的文本"""
    if not results:
        return (f"搜索「{query}」没有返回任何结果。"
                f"请基于你已有的知识回答，并说明这可能是过时信息。")
    lines = [f"以下是「{query}」的搜索结果（来自 DuckDuckGo，按相关度排序）：", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append(f"   来源: {r['url']}")
        lines.append("")
    lines.append("请基于以上搜索结果回答用户，并在回答中标注信息来源编号（如 [1]）。")
    return "\n".join(lines)


# ── 搜索工具定义 ──────────────────────────────────────────────
SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": ("联网搜索，获取实时信息。当问题涉及最新消息、当前时间、"
                        "实时数据（天气/股价/汇率）、近期事件，或你不确定的事实时，"
                        "必须调用本工具，不要凭记忆回答。"),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，尽量具体"}
            },
            "required": ["query"],
        },
    },
}


def _tool_names(tools):
    out = []
    for t in tools or []:
        if isinstance(t, dict):
            fn = t.get("function") or {}
            if fn.get("name"):
                out.append(fn["name"])
    return out


# ── 上游调用 ──────────────────────────────────────────────────
def upstream_headers():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = "Bearer " + API_KEY
    return h


def call_upstream(body):
    """非流式调用，返回解析后的 dict；失败抛异常。"""
    status, _, raw = http_request(
        UPSTREAM_URL + "/v1/chat/completions",
        data=body, headers=upstream_headers(), timeout=UPSTREAM_TIMEOUT,
    )
    if status != 200:
        raise RuntimeError(f"上游 HTTP {status}: {raw[:300].decode('utf-8','ignore')}")
    return json.loads(raw.decode("utf-8", "ignore"))


def iter_upstream_stream(body):
    """流式调用，逐块 yield 原始字节。"""
    hdrs = upstream_headers()
    hdrs["Accept"] = "text/event-stream"
    req = urllib.request.Request(
        UPSTREAM_URL + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"), headers=hdrs,
    )
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as r:
        while True:
            chunk = r.read(1)
            if not chunk:
                break
            buf = bytearray(chunk)
            # 尽量一次多读一点，降低 syscall 次数
            try:
                more = r.read1(65536)
                if more:
                    buf.extend(more)
            except Exception:
                pass
            yield bytes(buf)


# ── 把非流式结果回放成 SSE ────────────────────────────────────
def replay_as_stream(resp):
    """上游返回的是完整 JSON，这里拆成 SSE 块，让客户端以为是流式。"""
    cid = resp.get("id") or ("chatcmpl-" + os.urandom(8).hex())
    model = resp.get("model") or ""
    created = resp.get("created") or int(time.time())
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}

    def frame(delta, finish=None):
        return {
            "id": cid, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    yield frame({"role": "assistant"})
    content = msg.get("content") or ""
    if content:
        # 按 ~40 字切块，看起来更像流式
        for i in range(0, len(content), 40):
            yield frame({"content": content[i:i + 40]})
    if msg.get("tool_calls"):
        yield frame({"tool_calls": msg["tool_calls"]})
    yield frame({}, choice.get("finish_reason") or "stop")


def sse(payload):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


DONE = b"data: [DONE]\n\n"


# 是否允许连续多轮搜索。开启后最终答案会被上游缓冲（因为带工具），
# 关闭时搜索完就立刻去掉工具、走真流式。
MULTI_HOP = os.getenv("MULTI_HOP", "false").lower() in ("1", "true", "yes", "on")


class Handler(BaseHTTPRequestHandler):
    server_version = "SearchProxy/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    # ── 工具 ─────────────────────────────────────────────────
    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8", "ignore") or "{}")
        except Exception:
            return {}

    def _auth_ok(self):
        if not PROXY_KEY:
            return True
        got = (self.headers.get("Authorization") or "")
        got = got[7:].strip() if got.lower().startswith("bearer ") else got.strip()
        return got == PROXY_KEY

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _write_sse(self, payload):
        try:
            self.wfile.write(sse(payload))
            self.wfile.flush()
            return True
        except Exception:
            return False

    # ── 响应 ─────────────────────────────────────────────────
    def _respond(self, resp, want_stream):
        """有完整结果了：流式就回放成 SSE，否则原样 JSON。"""
        if not want_stream:
            return self._send_json(200, resp)
        self._sse_start()
        for frame in replay_as_stream(resp):
            if not self._write_sse(frame):
                return
        try:
            self.wfile.write(DONE)
            self.wfile.flush()
        except Exception:
            pass

    def _stream_from(self, body):
        """真流式：把上游的 SSE 原样透传给客户端。"""
        self._sse_start()
        try:
            for chunk in iter_upstream_stream(body):
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:
            log(f"流式转发异常: {e}")
            try:
                self.wfile.write(sse({"error": {"message": str(e), "type": "proxy_error"}}))
                self.wfile.write(DONE)
                self.wfile.flush()
            except Exception:
                pass

    def _query_of(self, call):
        args = (call.get("function") or {}).get("arguments") or "{}"
        try:
            return (json.loads(args).get("query") or "").strip()
        except Exception:
            m = re.search(r'"query"\s*:\s*"([^"]*)"', args)
            return (m.group(1) if m else "").strip()

    # ── 主逻辑 ───────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", "/ping", ""):
            return self._send_json(200, {"status": "ok", "service": "search-proxy",
                                         "upstream": UPSTREAM_URL,
                                         "tool": TOOL_NAME,
                                         "auto_inject": AUTO_INJECT_TOOL,
                                         "multi_hop": MULTI_HOP,
                                         "backends": [n for n, _ in BACKENDS]})
        if not self._auth_ok():
            return self._send_json(401, {"error": {"message": "invalid api key"}})
        # 其余 GET 透传（如 /v1/models）
        return self._passthrough_get()

    def _passthrough_get(self):
        status, hdrs, raw = http_request(
            UPSTREAM_URL + self.path, headers=upstream_headers(), timeout=30)
        self.send_response(status or 502)
        ct = hdrs.get("Content-Type") or "application/json"
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if not path.endswith("/chat/completions"):
            return self._send_json(404, {"error": {"message": "not found: " + path}})
        if not self._auth_ok():
            return self._send_json(401, {"error": {"message": "invalid api key"}})
        self.handle_chat()

    def handle_chat(self):
        body = self._read_json()
        if not body:
            return self._send_json(400, {"error": {"message": "empty body"}})

        want_stream = bool(body.get("stream"))
        messages = list(body.get("messages") or [])
        tools = list(body.get("tools") or [])

        # 上游按「整个 prompt」计字符：messages + 工具定义都要算进预算。
        # 只裁消息、不管工具，总量仍会顶过 10000 → 413。
        _tools_in = len(tools)
        _tools_before = _json_chars(tools)
        tools, _t_trimmed = fit_tools(tools, max(0, MAX_INPUT_CHARS - MIN_MSG_CHARS))
        _tools_chars = _json_chars(tools)
        _msg_budget = max(800, MAX_INPUT_CHARS - _tools_chars)
        _before = _msg_chars(messages)
        messages, _m_trimmed = fit_messages(messages, limit=_msg_budget)
        if _m_trimmed or _t_trimmed:
            log(f"载荷裁剪：消息 {_before}→{_msg_chars(messages)}"
                f"，工具 {_tools_in}→{len(tools)} 个 / {_tools_before}→{_tools_chars} 字符"
                f"（上限 {MAX_INPUT_CHARS}，消息预算 {_msg_budget}）")
        body["messages"] = messages
        if "tools" in body:
            body["tools"] = tools

        if AUTO_INJECT_TOOL and TOOL_NAME not in _tool_names(tools):
            tools = tools + [SEARCH_TOOL]

        # 客户端没要搜索能力 → 纯透传，不做任何缓冲
        if TOOL_NAME not in _tool_names(tools):
            return self._stream_from(body) if want_stream else self._respond(
                call_upstream(body), False)

        work = dict(body)
        work["messages"] = messages
        work["tools"] = tools
        work["stream"] = False          # 检测轮一律非流式（上游带工具时本来也缓冲）

        for rnd in range(MAX_ROUNDS):
            try:
                resp = call_upstream(work)
            except Exception as e:
                log(f"上游调用失败: {e}")
                return self._send_json(502, {"error": {"message": str(e), "type": "upstream_error"}})

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            search_calls = [c for c in calls
                            if (c.get("function") or {}).get("name") == TOOL_NAME]

            if not search_calls:
                # 没有搜索请求：可能是普通回答，也可能是模型要调客户端自己的工具
                return self._respond(resp, want_stream)

            # 执行搜索
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": calls})
            for c in search_calls:
                q = self._query_of(c)
                results, backend = web_search(q)
                log(f"第{rnd+1}轮搜索 {q!r} → {len(results)} 条（{backend}）")
                messages.append({"role": "tool",
                                 "tool_call_id": c.get("id") or f"call_{rnd+1}",
                                 "content": format_results(results, q)})
            work["messages"] = messages

            if not MULTI_HOP:
                # 搜索完就去掉工具 → 上游不再缓冲 → 最终答案真流式
                work.pop("tools", None)
                if want_stream:
                    work["stream"] = True      # ← 必须显式打开，否则上游仍返回非流式
                    return self._stream_from(work)
                work["stream"] = False
                try:
                    return self._respond(call_upstream(work), False)
                except Exception as e:
                    return self._send_json(502, {"error": {"message": str(e)}})

        # 多轮模式用尽轮次 → 最后一轮不带工具
        work.pop("tools", None)
        if want_stream:
            work["stream"] = True
            return self._stream_from(work)
        try:
            return self._respond(call_upstream(work), False)
        except Exception as e:
            return self._send_json(502, {"error": {"message": str(e)}})


def main():
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    srv.daemon_threads = True
    log(f"联网搜索代理启动 http://{LISTEN_HOST}:{LISTEN_PORT}")
    log(f"  上游      : {UPSTREAM_URL}")
    log(f"  工具名    : {TOOL_NAME}  (自动注入: {AUTO_INJECT_TOOL}, 多轮: {MULTI_HOP})")
    log(f"  搜索后端  : {' → '.join(n for n, _ in BACKENDS)}")
    log(f"  客户端把 base_url 指到 http://<本机IP>:{LISTEN_PORT}/v1 即可")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("关闭")
        srv.shutdown()


if __name__ == "__main__":
    main()
