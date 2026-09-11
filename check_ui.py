#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内嵌前端静态校验（零依赖，改过 gui_server.py 的 HTML/JS 后跑一遍）。

检查项：
  1. 提取 INDEX_HTML 里所有 <script> 块，交给 `node --check` 验语法；
  2. HTML 标签配对（先剥注释与 script/style 内容，否则 `<!--` 会被当成标签名）；
  3. id 唯一性；
  4. JS 里 `$('#x')` / `getElementById('x')` 引用的 id 是否都真实存在。
     ——这条最值钱：拼错 id 在浏览器里是静默失效，不报错、也不容易发现。

用法：
    python check_ui.py                 # 只做结构与语法检查
    python check_ui.py --node <node>   # 指定 node 可执行文件

退出码 0 = 全部通过。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "gui_server.py")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr", "!doctype"}


def load_html():
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'INDEX_HTML\s*=\s*r"""(.*?)"""', src, re.S)
    if not m:
        sys.exit("FAIL 未在 gui_server.py 中找到 INDEX_HTML")
    return m.group(1)


def check_tags(html):
    scan = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    scan = re.sub(r"<script\b.*?</script>", "", scan, flags=re.S | re.I)
    scan = re.sub(r"<style\b.*?</style>", "", scan, flags=re.S | re.I)
    stack, bad = [], []
    for m in re.finditer(r"<(/?)([a-zA-Z][\w-]*)([^>]*?)(/?)>", scan):
        closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(4)
        if name in VOID or selfclose:
            continue
        if not closing:
            stack.append((name, m.start()))
        elif not stack:
            bad.append("多余的闭合 </%s> @%d" % (name, m.start()))
        elif stack[-1][0] != name:
            bad.append("配对错误：期望 </%s>，实际 </%s> @%d"
                       % (stack[-1][0], name, m.start()))
            stack.pop()
        else:
            stack.pop()
    bad += ["未闭合 <%s> @%d" % (n, p) for n, p in stack]
    return bad


def check_ids(html, js):
    ids = re.findall(r'\bid\s*=\s*"([^"]+)"', html)
    dupe = sorted({i for i in ids if ids.count(i) > 1})
    refs = set(re.findall(r"""[$]\(\s*['"]#([\w-]+)['"]\s*\)""", js))
    refs |= set(re.findall(r"""getElementById\(\s*['"]([\w-]+)['"]\s*\)""", js))
    return len(ids), dupe, sorted(refs - set(ids))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=None, help="node 可执行文件路径")
    args = ap.parse_args()

    html = load_html()
    scripts = re.findall(r"<script[^>]*>(.+?)</script>", html, re.S)
    if not scripts:
        sys.exit("FAIL 未找到 <script> 块")
    js = "\n;\n".join(scripts)
    print("提取 script 块 %d 个，共 %d 字符" % (len(scripts), len(js)))

    fails = []

    bad = check_tags(html)
    print("标签配对: %s" % ("OK" if not bad else "FAIL"))
    for b in bad[:12]:
        print("   " + b)
    fails += bad

    n_ids, dupe, missing = check_ids(html, js)
    print("id 唯一性: 共 %d 个，重复 %s" % (n_ids, dupe or "无"))
    print("JS 引用 id: 缺失 %s" % (missing or "无"))
    if dupe:
        fails.append("重复 id: %s" % dupe)
    if missing:
        fails.append("JS 引用了不存在的 id: %s" % missing)

    node = args.node or shutil.which("node")
    if not node:
        print("node --check: 跳过（未找到 node，可用 --node 指定）")
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            tmp = f.name
        try:
            r = subprocess.run([node, "--check", tmp],
                               capture_output=True, text=True)
            ok = r.returncode == 0
            print("node --check: %s" % ("OK" if ok else "FAIL"))
            if not ok:
                print("   " + (r.stderr or "").strip()[:800])
                fails.append("node --check 失败")
        finally:
            os.remove(tmp)

    print("\n结论: %s" % ("全部通过" if not fails else "发现 %d 个问题" % len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
