#!/usr/bin/env python3
"""实测 :8002 代理对各种客户端场景的兼容性"""
import json
import urllib.request

BASE = "http://127.0.0.1:8002/v1"
KEY = "kof97boss"
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def post(path, body, stream=False, timeout=180):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
    return raw


def get(path, timeout=20):
    req = urllib.request.Request(BASE + path, headers=H)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


print("=" * 62)
print("测试 1: GET /v1/models （模型列表能不能过）")
print("=" * 62)
try:
    d = json.loads(get("/models"))
    ids = [m.get("id") for m in (d.get("data") or [])]
    print(f"  ✅ 拿到 {len(ids)} 个模型")
    print(f"     {ids[:6]}")
except Exception as e:
    print(f"  ❌ 失败: {e}")

print()
print("=" * 62)
print("测试 2: 客户端自带工具（关键 —— 模型要调客户端的工具）")
print("=" * 62)
body = {
    "model": "deepseek-v4-flash-de",
    "messages": [{"role": "user", "content": "现在几点了？用工具查。"}],
    "max_tokens": 300,
    "tools": [{
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前时间",
            "parameters": {"type": "object", "properties": {}},
        },
    }],
}
try:
    d = json.loads(post("/chat/completions", body))
    ch = (d.get("choices") or [{}])[0]
    m = ch.get("message") or {}
    tc = m.get("tool_calls")
    print(f"  finish_reason: {ch.get('finish_reason')}")
    if tc:
        print(f"  ✅ 返回了 tool_calls（客户端能接手）")
        for c in tc:
            fn = c.get("function") or {}
            print(f"     - id={c.get('id')}  name={fn.get('name')}  args={fn.get('arguments')}")
    else:
        print(f"  ⚠️ 没有 tool_calls，模型直接答了：")
        print(f"     {(m.get('content') or '')[:200]}")
except Exception as e:
    print(f"  ❌ 失败: {e}")

print()
print("=" * 62)
print("测试 3: 客户端自带工具 + 流式")
print("=" * 62)
body["stream"] = True
try:
    raw = post("/chat/completions", body)
    frames, tool_frames, content = [], 0, []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        p = line[6:]
        if p == "[DONE]":
            continue
        try:
            d = json.loads(p)
        except Exception:
            continue
        frames.append(d)
        for c in d.get("choices", []):
            dl = c.get("delta") or {}
            if dl.get("tool_calls"):
                tool_frames += 1
            if dl.get("content"):
                content.append(dl["content"])
    print(f"  SSE 帧数: {len(frames)}   含 tool_calls 的帧: {tool_frames}")
    if tool_frames:
        print("  ✅ 流式下 tool_calls 也能传出来")
    else:
        print("  ⚠️ 流式下没有 tool_calls")
    if content:
        print(f"     正文: {''.join(content)[:150]}")
except Exception as e:
    print(f"  ❌ 失败: {e}")

print()
print("=" * 62)
print("测试 4: 普通问答（不带工具）—— 会不会被强行塞搜索")
print("=" * 62)
body2 = {
    "model": "deepseek-v4-flash-de",
    "messages": [{"role": "user", "content": "用一句话解释什么是递归。"}],
    "max_tokens": 200,
}
try:
    d = json.loads(post("/chat/completions", body2))
    m = (d.get("choices") or [{}])[0].get("message") or {}
    print(f"  ✅ {m.get('content')}")
except Exception as e:
    print(f"  ❌ 失败: {e}")
