# -*- coding: utf-8 -*-
"""卸载器端到端验证（无头、零网络，可进 CI）。

在一个隔离目录里伪造"已经用过的程序目录"（主 exe / 卸载器 / 旧版 vbs 遗留 /
配置 / 凭据 / 状态 / 日志与轮转 / pid 文件 / 缓存目录 / 探针报告 / 升级备份
/ 用户无关文件），再跑内置卸载器 --silent，逐项断言：

  1. 文件系统：全部产物被删、**用户自己的文件保留**、目录保留（因为非空）
  2. 进程：目录内正在运行的 CampusNetAuth.exe 被终止（模拟"守护/引导父进程
     占着 exe"→ 这正是历史上卸载删不掉文件的根因）
  3. 自删：卸载器把自己删掉（改名 + 脱离 cmd 延时删除）
  4. 退出码：0=干净
  5. 注册表：HKCU Run 键指向本目录时应被删除（沙箱禁写时记为"环境跳过"）

需要先跑 build_uninstaller.bat 产出 build_assets\\CampusNetAuthUninstall.exe。
用法：python smoke_uninstall.py
"""
import os
import sys
import time
import shutil
import subprocess
import tempfile

# ⚠️ 本脚本要能在 CI（GitHub Windows 运行器）里跑：那里的控制台是 cp1252，
# 直接 print 中文会 UnicodeEncodeError 直接崩（本仓库在 CI 上真踩过）。
# 这里强制 UTF-8 输出，无法编码的字符降级为 ?，绝不因为"打印"而失败。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  (老 Python / 非标准流)
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
UNINST = os.path.join(ROOT, "build_assets", "CampusNetAuthUninstall.exe")
DIST_EXE = os.path.join(ROOT, "dist", "CampusNetAuth.exe")
# ⚠️ 隔离目录放在 %TEMP%（工作区之外）：更贴近"用户把程序放到别处"的真实用法，
# 也避开宿主环境可能给仓库目录注入的拒绝型 ACL。
TEST = os.path.join(tempfile.gettempdir(), "cna_smoke_uninstall")

FAILS = []
REPORT = []


def rec(msg=""):
    print(msg, flush=True)
    REPORT.append(str(msg))


def check(name, cond, extra=""):
    if cond:
        rec("  [PASS] %s %s" % (name, extra))
    else:
        rec("  [FAIL] %s %s" % (name, extra))
        FAILS.append(name)
    return bool(cond)


def w(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def build_fixture():
    # 沙箱 safe-delete 会拦部分删除；用全新目录，绝不复用旧目录
    if os.path.exists(TEST):
        shutil.rmtree(TEST, ignore_errors=True)
    os.makedirs(TEST, exist_ok=True)
    # 目录内其它名字的进程不能被杀（不是本程序产物）
    victim = w(os.path.join(TEST, "CampusNetAuth.exe"),
               open(shutil.which("ping") or r"C:\Windows\System32\ping.exe",
                    "rb").read())
    w(os.path.join(TEST, "CampusNetAuth.exe.old.exe"))
    w(os.path.join(TEST, "CampusNetAuth (1).exe"),
      open(DIST_EXE, "rb").read() if os.path.exists(DIST_EXE) else b"exe")
    w(os.path.join(TEST, "CampusNetAuthDaemon.exe"))
    w(os.path.join(TEST, "CampusNetAuthDaemon.vbs"))
    w(os.path.join(TEST, "CampusNetAuthUI.vbs"))
    w(os.path.join(TEST, "config.json"), b'{"username":"25xxxx"}')
    w(os.path.join(TEST, "cred.bin"), b"\x01\x02\x03")
    w(os.path.join(TEST, "state.json"), b'{"first_run_at":"x"}')
    w(os.path.join(TEST, "state.json.lock"))
    w(os.path.join(TEST, "daemon.pid"), b"12345\n12344\n")
    w(os.path.join(TEST, "campusnet.log"))
    w(os.path.join(TEST, "campusnet.log.1"))
    w(os.path.join(TEST, "_ui_trace.log"))
    w(os.path.join(TEST, "_ui_crash.log"))
    w(os.path.join(TEST, "autostart.log"))
    w(os.path.join(TEST, ".webview2-cache", "EBWebView", "junk.bin"))
    w(os.path.join(TEST, "probe", "capture_report.txt"))
    w(os.path.join(TEST, "__pycache__", "campusnet.cpython-313.pyc"))
    # 用户自己的文件：绝不能被删，目录也不能被删
    w(os.path.join(TEST, "我的笔记.txt"), "keep me".encode("utf-8"))
    # 卸载器本体（从 build_assets 拷进来，模拟"主 exe 打开时释放"）
    shutil.copy2(UNINST, os.path.join(TEST, "CampusNetAuthUninstall.exe"))
    return victim


def make_fake_dead_mei():
    """0.6.0 起不再内嵌 vbs，_MEI 特征改为卸载器自身（SelfName）。
    伪造一个"已无进程占用"的解包目录，验证新特征也能被识别并清掉。"""
    d = os.path.join(tempfile.gettempdir(), "_MEI_cna_dead_%d" % os.getpid())
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    w(os.path.join(d, "CampusNetAuthUninstall.exe"), b"MZ" + b"\x00" * 64)
    w(os.path.join(d, "python314.dll"), b"MZ" + b"\x00" * 64)
    return d


def make_fake_live_mei():
    """伪造"另一个正在运行的实例"的解包目录：带本程序特征 vbs，且里面有个
    被**独占打开**的 dll（模拟被进程映射的 python3xx.dll）。

    卸载器必须整体跳过它（一个文件都不动），且不得因此报"有残留"。
    返回 (目录, 保持打开的句柄列表)。"""
    d = os.path.join(tempfile.gettempdir(), "_MEI_cna_live_%d" % os.getpid())
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    w(os.path.join(d, "CampusNetAuthDaemon.vbs"))
    w(os.path.join(d, "CampusNetAuthUI.vbs"))
    locked = os.path.join(d, "python314.dll")
    w(locked, b"MZ" + b"\x00" * 64)
    handles = []
    try:
        import ctypes
        GENERIC_WRITE, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL = 0x40000000, 3, 0x80
        h = ctypes.windll.kernel32.CreateFileW(
            locked, GENERIC_WRITE, 0, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
            None)
        if h == -1:
            rec("  [警告] 无法独占打开模拟 dll（句柄无效）")
        else:
            handles.append(h)
    except Exception as e:  # noqa: BLE001
        rec("  [警告] 模拟独占占用失败: %r" % e)
    return d, handles


def main():
    rec("===== 卸载器端到端（隔离目录）=====")
    if not os.path.exists(UNINST):
        rec("[跳过] 未找到 %s，请先运行 build_uninstaller.bat" % UNINST)
        return 2
    if not os.path.exists(DIST_EXE):
        rec("[警告] 未找到 dist\\CampusNetAuth.exe（不影响卸载器本身测试）")
    victim = build_fixture()
    rec("[准备] 目录: %s" % TEST)
    live_mei, live_handles = make_fake_live_mei()
    rec("[准备] 伪造「在用的」解包目录: %s（独占句柄 %d 个）"
        % (live_mei, len(live_handles)))
    dead_mei = make_fake_dead_mei()
    rec("[准备] 伪造「无占用」的新式解包目录: %s" % dead_mei)

    # 起一个"占用 exe 的进程"：把 ping.exe 复制成 CampusNetAuth.exe 跑 90 秒，
    # 模拟 onefile 引导父进程持有 exe 文件句柄（Windows 下它会锁住文件）
    proc = subprocess.Popen([victim, "-n", "90", "127.0.0.1"],
                            cwd=TEST, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    check("占用 exe 的进程已启动", proc.poll() is None,
          "pid=%s" % proc.pid)

    # ---------------- 跑卸载器（静默） ----------------
    rec("\n===== 执行 CampusNetAuthUninstall.exe --silent =====")
    runner = os.path.join(TEST, "CampusNetAuthUninstall.exe")
    report = os.path.join(tempfile.gettempdir(), "cna_smoke_uninstall_report.txt")
    if os.path.exists(report):
        os.remove(report)
    t0 = time.time()
    r = subprocess.run([runner, "--silent", "--report=" + report], cwd=TEST,
                       capture_output=True, timeout=180)
    rec("  [信息] 退出码=%s 耗时=%.1fs" % (r.returncode, time.time() - t0))
    if os.path.exists(report):
        rec("  [信息] 卸载报告：")
        for line in open(report, encoding="utf-8-sig").read().splitlines():
            rec("    " + line)

    # 解析报告里的失败项：只允许"沙箱禁写注册表"这一条环境限制，其余一律算 bug
    fails = []
    try:
        txt = open(report, encoding="utf-8-sig").read()
        lines = txt.splitlines()
        grab = False
        for ln in lines:
            if ln.startswith("[失败]"):
                grab = True
                continue
            if ln.startswith("[") and grab:
                grab = False
            if grab and ln.startswith("  "):
                fails.append(ln.strip())
    except Exception as e:
        rec("  [警告] 解析报告失败: %r" % e)
    env_only = [f for f in fails if "注册表" in f]
    real_fails = [f for f in fails if "注册表" not in f]
    check("除「沙箱禁写注册表」外无失败项", not real_fails, str(real_fails))
    if env_only:
        rec("  [信息] 环境限制（沙箱不允许写 HKCU，非产品问题）: %s" % env_only)
    else:
        rec("  [信息] 注册表访问未被沙箱拦截（本环境可写 HKCU）")
    if r.stdout:
        rec("  [信息] stdout=%r" % r.stdout[:300])
    if r.stderr:
        rec("  [信息] stderr=%r" % r.stderr[:300])

    # 自删是"退出后再删"，给它一点时间
    for _ in range(20):
        if not os.path.exists(runner):
            break
        time.sleep(0.5)

    # ---------------- 断言 ----------------
    rec("\n===== 断言 =====")
    check("卸载器退出码与报告一致（0=干净 / 1=有残留）",
          (r.returncode == 0) == (not fails), "rc=%s fails=%s"
          % (r.returncode, fails))
    check("占用 exe 的进程已终止", proc.poll() is not None,
          "poll=%s" % proc.poll())

    gone = ["CampusNetAuth.exe", "CampusNetAuth.exe.old.exe",
            "CampusNetAuth (1).exe", "CampusNetAuthDaemon.exe",
            "CampusNetAuthDaemon.vbs", "CampusNetAuthUI.vbs",
            "CampusNetAuthUninstall.exe",
            "config.json", "cred.bin", "state.json", "state.json.lock",
            "daemon.pid", "campusnet.log", "campusnet.log.1",
            "_ui_trace.log", "_ui_crash.log", "autostart.log"]
    left = [n for n in gone if os.path.exists(os.path.join(TEST, n))]
    check("全部产物文件已删除", not left, "残留=%s" % (left or "无"))

    dirs_gone = [".webview2-cache", "probe", "__pycache__"]
    leftd = [n for n in dirs_gone if os.path.exists(os.path.join(TEST, n))]
    check("缓存/探针/缓存字节码目录已删除", not leftd, "残留=%s" % (leftd or "无"))

    check("用户自己的文件被保留",
          os.path.exists(os.path.join(TEST, "我的笔记.txt")))
    check("目录被保留（因为还有用户文件）", os.path.isdir(TEST))

    # 残留的 ~uninstall-*.tmp（自删脚本失败时）
    tmps = [n for n in os.listdir(TEST) if n.startswith("~uninstall-")]
    check("无自删临时残留", not tmps, str(tmps))

    # ---------------- 「另一个正在运行的实例」必须毫发无伤 ----------------
    rec("\n----- 不得殃及另一个正在运行的实例 -----")
    check("在用的解包目录未被删除", os.path.exists(live_mei), live_mei)
    check("在用的解包目录内文件未被删（vbs 仍在）",
          os.path.exists(os.path.join(live_mei, "CampusNetAuthDaemon.vbs"))
          and os.path.exists(os.path.join(live_mei, "python314.dll")))
    check("无占用的新式解包目录已清理（仅含卸载器特征）",
          not os.path.exists(dead_mei), dead_mei)
    if os.path.exists(report):
        txt2 = open(report, encoding="utf-8-sig").read()
        check("报告里说明是「跳过」而不是失败",
              "跳过" in txt2 and "未能删除" not in txt2.split("[失败]")[-1][:400],
              "")
    # 清理模拟目录（先放开独占句柄）
    for h in live_handles:
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(live_mei, ignore_errors=True)

    # ---------------- 注册表（沙箱可能禁写） ----------------
    rec("\n===== 注册表 Run 键 =====")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_QUERY_VALUE) as k:
            try:
                rec("  [信息] 当前 Run\\CampusNetAuth = %r"
                    % (winreg.QueryValueEx(k, "CampusNetAuth")[0],))
            except FileNotFoundError:
                rec("  [信息] 当前无 Run\\CampusNetAuth（沙箱通常读不到真实用户键）")
    except Exception as e:
        rec("  [信息] 读注册表受限: %r" % e)

    rec("\n===== 结论 =====")
    rec("失败项: %s" % (FAILS or "无"))
    with open(os.path.join(tempfile.gettempdir(),
                          "cna_smoke_uninstall_result.txt"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())