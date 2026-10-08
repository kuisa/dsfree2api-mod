#!/usr/bin/env python3
"""模拟真实客户端（Cline/Cursor 那种）的完整工具调用往返"""
import json
import os
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8002/v1")
H = {"Authorization": f"Bearer {os.environ.get('API_KEY', 'your-apikey')}",
     "Content-Type": "application/json"}


def chat(body, timeout=180):
    req = urllib.request.Request(BASE + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}]

msgs = [{"role": "user", "content": "帮我查下武汉的天气，然后告诉我适不适合跑步。"}]

print("第 1 步：客户端发问（带自己的 get_weather 工具）")
r1 = chat({"model": "deepseek-v4-flash-de", "messages": msgs,
           "tools": TOOLS, "max_tokens": 400})
ch1 = (r1.get("choices") or [{}])[0]
m1 = ch1.get("message") or {}
tc = m1.get("tool_calls") or []
print(f"  finish_reason = {ch1.get('finish_reason')}")
if not tc:
    print("  ⚠️ 模型没调工具，直接答了：")
    print("   ", (m1.get("content") or "")[:300])
    raise SystemExit(0)
for c in tc:
    print(f"  → 要求调用: {c['function']['name']}({c['function']['arguments']})")

print()
print("第 2 步：客户端执行工具（这里假装查到结果），把结果回传")
msgs.append({"role": "assistant", "content": m1.get("content") or "", "tool_calls": tc})
msgs.append({
    "role": "tool",
    "tool_call_id": tc[0].get("id") or "call_001",
    "content": "武汉今天晴，气温 18~27℃，东南风 2 级，湿度 55%，空气质量良，AQI 62。",
})
r2 = chat({"model": "deepseek-v4-flash-de", "messages": msgs,
           "tools": TOOLS, "max_tokens": 400})
ch2 = (r2.get("choices") or [{}])[0]
m2 = ch2.get("message") or {}
print(f"  finish_reason = {ch2.get('finish_reason')}")
print(f"  最终答案：{m2.get('content')}")
