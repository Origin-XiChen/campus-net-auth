#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整体回归冒烟：静态校验 + 单元 + exe 后端 API 链路 + 组件状态 + 健康检查 + 退出清理。

一次性脚本，跑完即删。结果写入 _smoke_result.txt
"""
import os
import re
import sys
import ast
import json
import time
import shutil
import ctypes
import socket
import tempfile
import subprocess
import urllib.parse
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

REPORT = []
FAILS = []
BACKUP_RUN = None
TOKEN = ""  # GUI 服务的会话令牌（启动后从 _ui_trace.log 的 URL 里解析）


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


# ============================ 0. 静态校验 ============================
def part_static():
    rec("\n===== 0. 静态校验 =====")
    for f in ("campusnet.py", "desktop.py", "gui_server.py", "ui.py",
              "diagnose_probe.py"):
        p = os.path.join(ROOT, f)
        try:
            ast.parse(open(p, encoding="utf-8").read())
            rec("  [PASS] AST %s" % f)
        except Exception as e:
            check("AST %s" % f, False, repr(e))

    src = open(os.path.join(ROOT, "gui_server.py"), encoding="utf-8").read()
    m = re.search(r"<script>(.*?)</script>", src, re.S)
    js_file = os.path.join(tempfile.gettempdir(), "_smoke_ui_check.js")
    with open(js_file, "w", encoding="utf-8") as f:
        f.write(m.group(1))
    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
    r = subprocess.run([node, "--check", js_file], capture_output=True, text=True)
    check("前端 JS 语法", r.returncode == 0,
          (r.stderr.strip().splitlines() or [""])[-1][:160])

    for tag in ("div", "section", "button", "script", "style"):
        o = len(re.findall(r"<%s[\s>]" % tag, src))
        c = len(re.findall(r"</%s>" % tag, src))
        check("HTML <%s> 配对" % tag, o == c, "%d/%d" % (o, c))
    ids = re.findall(r'id="([^"]+)"', src)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    check("HTML id 唯一", not dup, "%d 个 id %s" % (len(ids), dup or ""))


# ============================ 1. 单元 ============================
def part_unit(cn, dk):
    rec("\n===== 1. 新增逻辑单元复测 =====")
    # _cleanup_stale_mei：仅在打包态(frozen)生效，扫描 %TEMP% 顶层 _MEI*
    import glob
    tmp = os.environ.get("TEMP") or tempfile.gettempdir()
    now = time.time()
    tag = "smoke%d" % int(now)
    made = []

    def mk(name, age=None):
        d = os.path.join(tmp, "_MEI" + name + tag)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "x.dll"), "w") as f:
            f.write("x")
        if age:
            os.utime(d, (now - age, now - age))
        made.append(d)
        return d

    d_old = mk("old", age=7200)      # 2 小时前 → 应清理
    d_new = mk("new", age=60)        # 1 分钟前 → 保留
    d_cur = mk("cur", age=7200)      # 假装是当前进程目录 → 保留

    saved_mei = getattr(sys, "_MEIPASS", None)
    saved_frozen = getattr(sys, "frozen", False)
    sys.frozen = True
    sys._MEIPASS = d_cur
    try:
        n = cn._cleanup_stale_mei(max_age=3600)
    finally:
        sys.frozen = saved_frozen
        if saved_mei is None:
            try:
                del sys._MEIPASS
            except Exception:
                pass
        else:
            sys._MEIPASS = saved_mei
    check("清扫返回计数 >=1", n >= 1, "n=%d" % n)
    check("旧目录已清理", not os.path.exists(d_old))
    check("新目录保留", os.path.exists(d_new))
    check("当前进程目录保留", os.path.exists(d_cur))

    # 被占用目录：CWD 落在目录内 → 改名探测必失败 → 跳过（防误删运行中实例）
    d_hold = mk("hold", age=7200)
    cwd0 = os.getcwd()
    os.chdir(d_hold)
    sys.frozen = True
    try:
        cn._cleanup_stale_mei(max_age=3600)
    except Exception as e:
        rec("  [信息] 占用场景异常: %r" % e)
    finally:
        sys.frozen = saved_frozen
        os.chdir(cwd0)
    check("被占用目录不动", os.path.exists(d_hold))

    for d in made:
        shutil.rmtree(d, ignore_errors=True)
    for junk in glob.glob(os.path.join(tmp, "*._gcprobe")):
        shutil.rmtree(junk, ignore_errors=True)

    # _own_webview2_alive
    marker = r"D:\app\.webview2-cache"
    t1 = "Node,CommandLine\r\nMSI,msedgewebview2.exe --user-data-dir=C:\\other\r\n"
    t2 = ("Node,CommandLine\r\nMSI,msedgewebview2.exe --user-data-dir="
          + marker + "\r\n")
    check("其它 WebView2 应用不算", dk._own_webview2_alive(t1, marker) is False)
    check("本程序 WebView2 算存活", dk._own_webview2_alive(t2, marker) is True)
    check("无进程返回 False", dk._own_webview2_alive("", marker) is False)


# ============================ HTTP ============================
def http(url, method="GET", body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("X-CNA-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"__raw__": raw}


def get_text(url, timeout=20):
    req = urllib.request.Request(url)
    if TOKEN:
        req.add_header("X-CNA-Token", TOKEN)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def raw_post(base, path, body=None, token=None, host_hdr=None, timeout=30):
    """裸 socket POST，返回原始响应字节（用于断言响应段数与状态码）。"""
    prt = urllib.parse.urlparse(base).port
    payload = json.dumps(body or {}).encode()
    lines = ["POST %s HTTP/1.1" % path,
             "Host: %s" % (host_hdr or ("127.0.0.1:%d" % prt)),
             "Content-Type: application/json",
             "Content-Length: %d" % len(payload),
             "Connection: close"]
    if token:
        lines.append("X-CNA-Token: " + token)
    s = socket.create_connection(("127.0.0.1", prt), timeout=timeout)
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return buf


# ============================ 主流程 ============================
def main():
    import winreg
    import campusnet as cn
    import desktop as dk

    part_static()
    part_unit(cn, dk)

    RUN_KEY = cn.RUN_KEY
    TASK_NAME = cn.TASK_NAME

    def reg_read():
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_QUERY_VALUE) as k:
                return winreg.QueryValueEx(k, TASK_NAME)[0]
        except Exception:
            return None

    def reg_set(val):
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                if val is None:
                    try:
                        winreg.DeleteValue(k, TASK_NAME)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(k, TASK_NAME, 0, winreg.REG_SZ, val)
            return True
        except Exception as e:
            rec("  [警告] 注册表写入受限: %r" % e)
            return False

    global BACKUP_RUN
    BACKUP = reg_read()
    BACKUP_RUN = BACKUP
    rec("\n[备份] HKCU Run\\%s = %r" % (TASK_NAME, BACKUP))

    src_exe = os.path.join(ROOT, "dist", "CampusNetAuth.exe")
    if not os.path.exists(src_exe):
        check("dist 交付 exe 存在", False, src_exe)
        return 2
    VERIFY = os.path.join(tempfile.gettempdir(),
                          "cna_smoke_%d" % int(time.time()))
    os.makedirs(VERIFY, exist_ok=True)
    shutil.copy2(src_exe, os.path.join(VERIFY, "CampusNetAuth.exe"))
    # 守护角色需要凭据才不会 rc=1 退出
    with open(os.path.join(VERIFY, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"portal_host": "172.16.54.18", "username": "smoke_test"}, f)
    try:
        cn.CredentialStore(path=os.path.join(VERIFY, "cred.bin")).save("smoke-pwd")
    except Exception as e:
        rec("  [警告] 写测试凭据失败: %r" % e)
    rec("\n[准备] 验证目录: %s" % VERIFY)

    user32 = ctypes.windll.user32

    def find_hwnd():
        out = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                         ctypes.c_void_p)

        def cb(hwnd, _):
            cls = ctypes.create_unicode_buffer(256)
            title = ctypes.create_unicode_buffer(512)
            user32.GetClassNameW(hwnd, cls, 256)
            user32.GetWindowTextW(hwnd, title, 512)
            if (title.value == "CampusNetAuth · 校园网无感认证"
                    and cls.value.startswith("WindowsForms10.")):
                out.append(hwnd)
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return out[0] if out else 0

    def mei_set():
        t = os.environ.get("TEMP", tempfile.gettempdir())
        return set(p for p in os.listdir(t) if p.upper().startswith("_MEI"))

    exe = os.path.join(VERIFY, "CampusNetAuth.exe")
    trace = os.path.join(VERIFY, "_ui_trace.log")

    # 基线：确保没有真实守护占用单例端口
    if cn.daemon_running():
        rec("[准备] 已有守护在跑，先优雅停止")
        cn.stop_daemon()
        time.sleep(2)

    # ========== 2. exe 后端 API 链路 ==========
    rec("\n===== 2. 最新 exe：后端 API 链路 =====")
    mei_before = mei_set()
    p = subprocess.Popen([exe], cwd=VERIFY)
    url = None
    for _ in range(120):
        if os.path.exists(trace):
            try:
                with open(trace, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "gui server url:" in line:
                            url = line.split("gui server url:", 1)[1].strip()
            except Exception:
                pass
        if url:
            break
        time.sleep(1)
    if not check("后端地址就绪", bool(url), url or ""):
        p.kill()
        return 3
    rec("  [信息] %s（启动耗时已含 onefile 解压）" % url)
    # URL 上携带会话 token（gui_server 鉴权），API 调用需拆出 base 与 token
    global TOKEN
    BASE = url.split("?", 1)[0]
    TOKEN = urllib.parse.parse_qs(
        urllib.parse.urlparse(url).query).get("token", [""])[0]
    check("服务 URL 携带会话 token", bool(TOKEN))

    html = get_text(url)
    check("GET / 返回内嵌 UI", "CampusNetAuth · 校园网无感认证" in html)
    check("前端含自启健康警示位", 'id="autostartWarn"' in html)

    st = http(BASE + "api/status")
    for k in ("online", "autostart", "autostart_health", "daemon",
              "daemon_pid", "components", "first_deploy", "foreign_files",
              "last_error", "events", "cfg", "host", "interval", "username"):
        check("status 字段 %s" % k, k in st, "" if k in st else "缺失")
    rec("  [信息] components=%s autostart=%s daemon=%s" %
        (st.get("components"), st.get("autostart"), st.get("daemon")))

    lg = http(BASE + "api/log?n=20")
    _txt = lg.get("text", "") if isinstance(lg, dict) else ""
    check("GET /api/log?n=20", isinstance(lg, dict) and "text" in lg,
          "尾部: " + _txt.strip().splitlines()[-1][:90] if _txt.strip() else "")

    tk = http(BASE + "api/task?id=bogus")
    check("未知 task id 不崩", isinstance(tk, dict))

    # ========== 2.5 API 契约：鉴权 + 单响应（回归 P0 修复）==========
    rec("\n===== 2.5 API 契约：鉴权 + 单响应 =====")
    r = raw_post(BASE, "/api/config", {}, token=None)
    check("无 token 调用被拒(403)", r.startswith(b"HTTP/1.0 403"),
          r.split(b"\r\n", 1)[0].decode("utf-8", "replace"))
    r = raw_post(BASE, "/api/config", {}, token=TOKEN, host_hdr="evil.com")
    check("非回环 Host 被拒(403)", r.startswith(b"HTTP/1.0 403"))
    try:
        get_text(BASE)  # 根页面不带 token
        check("根页面无 token 被拒(403)", False)
    except urllib.error.HTTPError as e:
        check("根页面无 token 被拒(403)", e.code == 403)
    r = raw_post(BASE, "/api/config", {}, token=TOKEN)
    check("config 合法调用单响应 200", r.count(b"HTTP/1.0 200 OK") == 1)
    r = raw_post(BASE, "/api/connect", {}, token=TOKEN)
    n200 = r.count(b"HTTP/1.0 200 OK")
    body = r.split(b"\r\n\r\n", 1)[-1].strip()
    check("connect 恰好一段响应（回归双响应 bug）", n200 == 1, "段数=%d" % n200)
    check("connect 返回真实结果（非裸 ok:true）", body != b'{"ok": true}',
          body[:100].decode("utf-8", "replace"))

    # ========== 3. 组件状态 ==========
    rec("\n===== 3. 组件安装 / 状态 / 卸载 =====")
    r = http(BASE + "api/components/install", "POST", {"name": "daemon"})
    rec("  [信息] install daemon -> %s" % r)
    check("daemon 组件安装", r.get("ok") is True, r.get("message", ""))
    check("仅释放 vbs（无 exe 副本）",
          os.path.exists(os.path.join(VERIFY, "CampusNetAuthDaemon.vbs"))
          and not os.path.exists(
              os.path.join(VERIFY, "CampusNetAuthDaemon.exe")))
    time.sleep(10)
    st = http(BASE + "api/status")
    check("M1 安装即拉起守护", st.get("daemon") is True,
          "pid=%s" % st.get("daemon_pid"))
    rec("  [信息] components=%s" % st.get("components"))

    r = http(BASE + "api/daemon/stop", "POST")
    time.sleep(2)
    st = http(BASE + "api/status")
    check("守护优雅停止", r.get("ok") is True and st.get("daemon") is False,
          "port_free=%s" % (not cn.daemon_running()))

    # ========== 4. 自启健康检查透出 ==========
    rec("\n===== 4. 自启健康检查（新功能透出）=====")
    r = http(BASE + "api/autostart/install", "POST")
    ok_reg = r.get("ok") is True
    check("自启组件安装", ok_reg, r.get("message", ""))
    st = http(BASE + "api/status")
    ah = st.get("autostart_health") or [None, ""]
    rec("  [信息] Run键=%r health=%s" % (reg_read(), ah))
    check("健康时 autostart_health=(True,'')", ah[0] is True and ah[1] == "",
          str(ah))

    # 负例：把 vbs 挪走 → 健康检查应报"启动器已丢失"并透出到 /api/status
    vbs = os.path.join(VERIFY, "CampusNetAuthDaemon.vbs")
    bak = vbs + ".smokebak"
    if os.path.exists(vbs):
        os.rename(vbs, bak)
        st = http(BASE + "api/status")
        ah2 = st.get("autostart_health") or [None, ""]
        rec("  [信息] 负例 health=%s" % (ah2,))
        check("缺失时健康检查报警",
              ah2[0] is False and "丢失" in (ah2[1] or ""), str(ah2))
        os.rename(bak, vbs)
        st = http(BASE + "api/status")
        check("恢复后健康检查转好",
              (st.get("autostart_health") or [None])[0] is True)

    # 卸载守卫：自启启用时不得卸值守组件
    r = http(BASE + "api/components/uninstall", "POST", {"name": "daemon"})
    check("自启启用时拒绝卸值守", r.get("ok") is False, r.get("message", ""))
    r = http(BASE + "api/components/uninstall", "POST", {"name": "autostart"})
    check("卸自启成功", r.get("ok") is True, r.get("message", ""))
    check("卸自启后 vbs 保留(与值守共用)", os.path.exists(vbs))
    r = http(BASE + "api/components/uninstall", "POST", {"name": "daemon"})
    check("卸值守成功", r.get("ok") is True, r.get("message", ""))
    check("卸值守后 vbs 移除", not os.path.exists(vbs))

    # ========== 5. 退出清理（A 方案路径）==========
    rec("\n===== 5. UI 退出清理（_%s）=====" % "MEI")
    hwnd = 0
    for _ in range(20):
        hwnd = find_hwnd()
        if hwnd:
            break
        time.sleep(1)
    check("主窗口出现", hwnd != 0)
    try:
        http(BASE + "api/window", "POST", {"action": "close"})
    except Exception as e:
        rec("  [信息] close 请求异常: %r" % e)
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < 30:
        time.sleep(0.5)
    rc = p.poll()
    if rc is None:
        p.kill()
        rc = p.wait()
        rec("  [信息] 30s 未自行退出，已强杀")
    check("UI 进程退出码 0", rc == 0, "rc=%s" % rc)
    time.sleep(2)
    mei_after = mei_set()
    new_mei = sorted(mei_after - mei_before)
    check("无新增 _MEI 残留", not new_mei, str(new_mei))

    # ========== 6. daemon 角色退出 ==========
    rec("\n===== 6. daemon 角色启动 / STOP 优雅退出 =====")
    mei_before2 = mei_set()
    dp = subprocess.Popen([exe, "daemon"], cwd=VERIFY)
    started = False
    for _ in range(30):
        if cn.daemon_running():
            started = True
            break
        time.sleep(1)
    check("守护启动（端口 %d）" % cn.SINGLETON_PORT, started,
          "pid=%s" % cn.daemon_pid())
    cn.stop_daemon()
    t0 = time.time()
    while dp.poll() is None and time.time() - t0 < 20:
        time.sleep(0.5)
    rc2 = dp.poll()
    if rc2 is None:
        dp.kill()
        rc2 = dp.wait()
    check("守护 STOP 后 rc=0", rc2 == 0, "rc=%s" % rc2)
    time.sleep(2)
    new_mei2 = sorted(mei_set() - mei_before2)
    check("守护退出无 _MEI 残留", not new_mei2, str(new_mei2))

    shutil.rmtree(VERIFY, ignore_errors=True)
    return 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except Exception as e:
        import traceback
        rec("[异常] %r" % e)
        rec(traceback.format_exc())
    finally:
        try:
            import campusnet as cn
            import winreg
            val = BACKUP_RUN
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, cn.RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                if val is None:
                    try:
                        winreg.DeleteValue(k, cn.TASK_NAME)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(k, cn.TASK_NAME, 0, winreg.REG_SZ, val)
            rec("\n[恢复] Run 键已还原为 %r" % (val,))
        except Exception as e:
            rec("\n[警告] Run 键恢复受限: %r" % e)
        rec("\n===== 结论 =====")
        rec("失败项: %s" % (FAILS or "无"))
        with open(os.path.join(ROOT, "_smoke_result.txt"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(REPORT))
        rec("报告: _smoke_result.txt")
    sys.exit(0 if not FAILS else 1)
