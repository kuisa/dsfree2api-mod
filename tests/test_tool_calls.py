#!/usr/bin/env python3
"""用真实 agent 规模的工具集复现：工具调用会不会变成文本"""
import json
import os
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE", "http://127.0.0.1:8002/v1")
H = {"Authorization": f"Bearer {os.environ.get('API_KEY', 'your-apikey')}",
     "Content-Type": "application/json"}


def mk(name, desc, props, req):
    return {"type": "function",
            "function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": props,
                                        "required": req}}}


# 模拟真实 agent 的工具集（8 个，schema 有嵌套）
TOOLS = [
    mk("terminal", "在本地执行 shell 命令", {"command": {"type": "string"}}, ["command"]),
    mk("read_file", "读取文件内容", {"path": {"type": "string"}, "limit": {"type": "integer"}}, ["path"]),
    mk("write_file", "写入文件", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    mk("search_files", "在文件里搜索内容", {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
    mk("web_fetch", "抓取网页内容", {"url": {"type": "string"}}, ["url"]),
    mk("git_status", "查看 git 状态", {"repo": {"type": "string"}}, []),
    mk("list_dir", "列目录", {"path": {"type": "string"}}, ["path"]),
    mk("run_tests", "跑测试", {"target": {"type": "string"}}, ["target"]),
]

CASES = [
    ("短问 + 单命令", [{"role": "user", "content": "帮我看看 /tmp 下有哪些文件"}]),
    ("要求检查仓库", [{"role": "user",
                  "content": "https://github.com/kuisa/dsfree2api-mod\n这个我更新好了，你帮我看看"}]),
    ("带系统提示", [{"role": "system", "content": "你是一个运维助手，需要执行命令时调用 terminal 工具。"},
                {"role": "user", "content": "检查一下磁盘占用"}]),
    ("长历史", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！有什么可以帮你的？"},
        {"role": "user", "content": "我想检查一个项目"},
        {"role": "assistant", "content": "好的，请把项目路径或仓库地址给我。"},
        {"role": "user", "content": "https://github.com/kuisa/dsfree2api-mod 帮我看看这个仓库"},
    ]),
]

bad = 0
for i, (name, msgs) in enumerate(CASES, 1):
    body = {"model": "deepseek-v4-flash-de", "max_tokens": 500,
            "messages": msgs, "tools": TOOLS}
    req = urllib.request.Request(BASE + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=H, method="POST")
    print("=" * 66)
    print(f"用例 {i}: {name}")
    print("=" * 66)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        print(f"  请求失败: {e}\n")
        continue
    ch = (d.get("choices") or [{}])[0]
    m = ch.get("message") or {}
    tc = m.get("tool_calls") or []
    content = m.get("content") or ""
    print(f"  finish_reason = {ch.get('finish_reason')}")
    if tc:
        print(f"  ✅ 结构化 tool_calls（{len(tc)} 个）")
        for c in tc:
            print(f"     {c['function']['name']}({c['function']['arguments'][:100]})")
    else:
        bad += 1
        print(f"  ❌ 没有 tool_calls —— 正文里可能藏着文本形式的调用：")
        print("  " + "-" * 62)
        print("    " + "\n    ".join(content.splitlines()[:18]))
        print("  " + "-" * 62)
    print()

print("=" * 66)
print(f"结果: {len(CASES)} 个用例，{len(CASES)-bad} 个返回结构化 tool_calls，{bad} 个失败")
print("=" * 66)
