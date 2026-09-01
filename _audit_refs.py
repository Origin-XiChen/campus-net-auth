"""仓库一致性自查：README/注释里引用的文件是否存在、注释是否与实现矛盾。

只读审计，不改任何文件。输出 _audit_report.txt
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORT = []


def rec(m=""):
    print(m)
    REPORT.append(str(m))


# ---------- 1. README 里引用的文件是否都存在 ----------
rec("===== 1. README 文件引用检查 =====")
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
refs = set()
for m in re.finditer(r"[A-Za-z0-9_\\/\.]+\.(?:py|bat|vbs|exe|json|txt|md|js|log)",
                     readme):
    refs.add(m.group(0).replace("/", os.sep).replace("\\", os.sep))
missing, ok = [], []
for r in sorted(refs):
    if r.startswith("."):
        continue
    cand = os.path.join(ROOT, r)
    if os.path.exists(cand):
        ok.append(r)
    elif os.sep not in r and os.path.exists(os.path.join(ROOT, "probe", r)):
        ok.append(r + " (probe/)")
    else:
        missing.append(r)
rec("  存在的引用 (%d): %s" % (len(ok), ", ".join(ok)))
rec("  [缺失] %s" % (missing or "无"))

# ---------- 2. bat 脚本引用的文件 ----------
rec("\n===== 2. bat 脚本引用检查 =====")
for name in sorted(f for f in os.listdir(ROOT) if f.endswith(".bat")):
    txt = open(os.path.join(ROOT, name), encoding="utf-8", errors="replace").read()
    tgt = re.findall(r"([A-Za-z0-9_\\]+\.(?:py|exe|vbs))", txt)
    bad = [t for t in set(tgt)
           if not os.path.exists(os.path.join(ROOT, t))
           and not os.path.exists(os.path.join(ROOT, os.path.basename(t)))
           and "python" not in t.lower()]
    rec("  %-16s 引用=%s %s" % (name, sorted(set(tgt)),
                                ("[缺失] " + str(bad)) if bad else "OK"))

# ---------- 3. 陈旧关键词扫描 ----------
rec("\n===== 3. 陈旧/可疑注释扫描 =====")
KEYWORDS = ["命名副本", "占位", "无实际监听", "463B", "18.8MB", "18800694",
            "计划任务", "旧版", "TODO", "FIXME", "XXX", "临时", "调试"]
for name in sorted(f for f in os.listdir(ROOT)
                   if f.endswith(".py") and not f.startswith("smoke")):
    path = os.path.join(ROOT, name)
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except Exception:
        continue
    hits = []
    for i, ln in enumerate(lines, 1):
        for k in KEYWORDS:
            if k in ln:
                hits.append((i, k, ln.strip()[:110]))
                break
    if hits:
        rec("  --- %s (%d 处) ---" % (name, len(hits)))
        for i, k, txt in hits[:12]:
            rec("    L%-5d [%s] %s" % (i, k, txt))

# ---------- 4. 注释里写死的端口号/大小/路径是否与常量一致 ----------
rec("\n===== 4. 注释与常量一致性 =====")
cn = open(os.path.join(ROOT, "campusnet.py"), encoding="utf-8").read()
port = re.search(r"SINGLETON_PORT\s*=\s*(\d+)", cn)
rec("  SINGLETON_PORT = %s（README/注释中出现的 47667 引用）" %
    (port.group(1) if port else "?"))
for f in ("README.md", "campusnet.py", "desktop.py", "gui_server.py", "ui.py"):
    txt = open(os.path.join(ROOT, f), encoding="utf-8", errors="replace").read()
    n47667 = txt.count("47667")
    if n47667:
        rec("  %-14s 出现 47667 ×%d" % (f, n47667))
# 端口是否还被别处硬编码成不同数字
ports = set(re.findall(r"(?<![\d.])4766\d(?![\d])", cn))
rec("  campusnet.py 中 4766x 端口常量集合: %s" % sorted(ports))

with open(os.path.join(ROOT, "_audit_report.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(REPORT))
rec("\n报告: _audit_report.txt")
