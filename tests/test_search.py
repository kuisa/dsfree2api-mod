#!/usr/bin/env python3
# 单独测试 search_proxy.py 的搜索后端
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("sp", "/root/search_proxy.py")
m = importlib.util.module_from_spec(spec)
sys.modules["sp"] = m
spec.loader.exec_module(m)

QUERIES = ["北京天气", "DeepSeek V4 发布", "Python 3.13 新特性"]

for q in QUERIES:
    print("=" * 60)
    print(f"查询: {q!r}")
    for name, fn in m.BACKENDS:
        try:
            r = fn(q, 3)
        except Exception as e:
            print(f"  {name:12s} 异常: {e}")
            continue
        print(f"  {name:12s} → {len(r)} 条")
        for x in r[:2]:
            print(f"      · {x['title'][:58]}")
            print(f"        {x['url'][:72]}")
            if x["snippet"]:
                print(f"        {x['snippet'][:72]}")
    r, backend = m.web_search(q, 3)
    print(f"  >>> 最终采用: {backend}  ({len(r)} 条)")
    print()
