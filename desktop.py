# -*- coding: utf-8 -*-
"""CampusNetAuth · 桌面壳(pywebview)

把 HTML GUI 包进原生桌面窗口(pywebview + Edge WebView2):
  - 单进程:后端 HTTPServer 线程 + pywebview 窗口(Edge WebView2)
  - 无边框自绘标题栏:WndProc 子类化接管拖动/Aero Snap/双击最大化/边缘缩放
  - hidden=True 等 WebView2 加载完成再显示 → 彻底零闪启动
  - js_api 桥接:前端可调用 Python(打开目录/窗口控制)

用法:
  python desktop.py            # 桌面模式(默认,随机端口)
  python desktop.py --dev      # 开发模式(打印日志)
"""
from __future__ import annotations

import re
import time
import argparse
import os
import sys
import threading
import ctypes
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --windowed 打包后 sys.stdout/stderr 为 None,print 会崩溃;这里做兜底
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _own_webview2_alive(text: str, marker: str) -> bool:
    """从 wmic CSV 输出中判断是否存在"属于本程序"的 msedgewebview2 进程。

    判定依据:命令行里同时含 msedgewebview2 与本程序独占的 .webview2-cache
    用户数据目录(其它 WebView2 应用用各自的目录,不会被误判)。
    """
    marker = marker.lower()
    for ln in text.splitlines():
        low = ln.lower()
        if "msedgewebview2" in low and marker in low:
            return True
    return False


# --- PInvoke 版 msedgewebview2 进程枚举（替代 wmic，绕开沙箱黑名单/Win11 已废弃） ---
# 启动时记录本程序 WebView2 子进程 PID 列表 → 退出时按 PID 等，
# 不再依赖 wmic/命令行 marker 检测（wmic 在沙箱被拦时立即 return 导致弹窗）。

_PROCESSENTRY32W_FIELDS = [
    ("dwSize", ctypes.c_uint32),
    ("cntUsage", ctypes.c_uint32),
    ("th32ProcessID", ctypes.c_uint32),
    ("th32DefaultHeapID", ctypes.c_void_p),
    ("th32ModuleID", ctypes.c_uint32),
    ("cntThreads", ctypes.c_uint32),
    ("th32ParentProcessID", ctypes.c_uint32),
    ("pcPriClassBase", ctypes.c_long),
    ("dwFlags", ctypes.c_uint32),
    ("szExeFile", ctypes.c_wchar * 260),
]
_PROCESSENTRY32W = type(
    "PROCESSENTRY32W",
    (ctypes.Structure,),
    {"_fields_": _PROCESSENTRY32W_FIELDS,
     # ⚠️ 动态 type() 创建的类没有 __class__ cell,lambda 里不能用 super();
     # 直接调基类构造器 + 填 dwSize(Windows 要求:必须是结构体实际大小)
     "__init__": lambda self: (ctypes.Structure.__init__(self),
                               setattr(self, "dwSize", ctypes.sizeof(self)))[-1]},
)
_SELF_WEBVIEW2_PIDS: list = []  # 启动时记录的本程序 WebView2 子进程 PID


def _collect_webview2_pids(root_pid: int) -> list:
    """收集以 root_pid 为根的进程树中所有 msedgewebview2.exe 的 PID。

    用 CreateToolhelp32Snapshot 枚举一次快照后按父进程链 BFS 收集：
    WebView2 的 browser 进程父进程=本程序，renderer/gpu 等子进程父进程=其
    browser 进程 → 整棵树都能收进来。⚠️ 绝不收集其它应用启动的 WebView2，
    否则退出等待时可能永远等不到，超时强杀还会误伤别人家的进程。
    纯 PInvoke，不依赖 wmic（沙箱拦截或 Win11 24H2+ 已废弃时仍可工作）。"""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == ctypes.c_void_p(-1).value:
        return []
    procs: list = []  # (pid, ppid, name)
    try:
        pe = _PROCESSENTRY32W()
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            procs.append((pe.th32ProcessID, pe.th32ParentProcessID,
                          pe.szExeFile.lower()))
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    pids: list = []
    frontier = [root_pid]
    while frontier:
        nxt = []
        for pid in frontier:
            for p, pp, name in procs:
                if pp == pid and name == "msedgewebview2.exe":
                    pids.append(p)
                    nxt.append(p)
        frontier = nxt
    return pids


def _pid_alive(pid: int) -> bool:
    """PID 是否仍在运行（GetExitCodeProcess 返 STILL_ACTIVE=259 视为还活）。"""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = wintypes.DWORD()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def _terminate_pid(pid: int) -> bool:
    """TerminateProcess 强杀指定 PID（用于等不到 WebView2 自然退出时兜底释放句柄）。"""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not h:
        return False
    r = k32.TerminateProcess(h, 1)
    k32.CloseHandle(h)
    return bool(r)


def _record_webview2_pids(delay: float = 2.0, retries: int = 4) -> None:
    """webview.start() 启动后异步记录"本程序进程树内"的 msedgewebview2 PID。

    用线程延迟枚举(WebView2 子进程启动有 1~2 秒延迟)，记录到
    _SELF_WEBVIEW2_PIDS 供退出时 _wait_webview2_exit 按 PID 等待；
    记录失败(启动过慢)则每 1s 重试，最多 retries 次，仍空则留给 wmic 兜底。
    只记录父进程链是本程序的 WebView2，不误收其它应用。"""
    def _worker():
        time.sleep(delay)
        for _ in range(retries):
            try:
                pids = _collect_webview2_pids(os.getpid())
                if pids:
                    _SELF_WEBVIEW2_PIDS.clear()
                    _SELF_WEBVIEW2_PIDS.extend(pids)
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
    threading.Thread(target=_worker, daemon=True).start()


def main() -> None:
    ap = argparse.ArgumentParser(description="校园网无感认证 · 桌面版")
    ap.add_argument("--dev", action="store_true", help="开发模式:打印日志")
    args = ap.parse_args()

    if getattr(sys, "frozen", False):
        os.chdir(os.path.dirname(sys.executable))  # 数据文件落在 exe 目录

    import gui_server

    srv, url = gui_server.serve(0, "127.0.0.1")
    if args.dev:
        print(f"[desktop] 后端已启动: {url}", flush=True)
    # 服务地址（含会话 token）落盘到 _ui_trace.log：便于无头验证 / 冒烟测试
    # 发现本机服务地址。该文件与 cred.bin 同目录、同用户 ACL——能读到它的
    # 进程本就可 DPAPI 解密凭据，token 落盘不扩大攻击面；token 真正防的是
    # 网页与其他本地用户（读不到此文件）。
    try:
        import campusnet as _cn
        with open(os.path.join(_cn.BASE_DIR, "_ui_trace.log"), "a",
                  encoding="utf-8") as _f:
            _f.write("gui server url: %s\n" % url)
    except Exception:  # noqa: BLE001
        pass

    import webview
    # 拖动逻辑：pywebview JS 侧像素级拖动（customize.js 的 onBodyMouseDown 从点击
    # 元素向上冒泡查找 DRAG_REGION_SELECTOR 匹配的祖先 → pywebviewMoveWindow）。
    # ⚠️ 注意：WndProc 的 WM_NCHITTEST→HTCAPTION 对 WebView2 子窗口覆盖的客户区
    # 收不到消息（WebView2 消费了命中测试），拖动必须走 JS 官方通道。
    webview.settings['DRAG_REGION_SELECTOR'] = '.drag-region'
    # ⚠️ DIRECT_TARGET_ONLY 必须保持 True（pywebview 6.x customize.js）：
    # easy_drag=True 时 window 级 mousedown 监听会对任意位置调用 onMouseDown，
    # 若 DIRECT_TARGET_ONLY=False 则不做 target 匹配 → 整个页面（内容区/按钮/
    # 侧栏）按下移动都会拖窗口，灾难级 bug。
    # True 模式下只有 target 本身匹配 .drag-region 才拖 → 可拖区域偏小，
    # 由前端在标题栏初始化时给 .tb-brand 及其子元素补 .drag-region 类解决
    # （见 gui_server.py「标题栏拖动」段），按钮已 stopPropagation 不受影响。
    webview.settings['DRAG_REGION_DIRECT_TARGET_ONLY'] = True
    # 固定 WebView2 用户数据目录(默认 private_mode 每次启动用临时目录:
    # 残留的 msedgewebview2 进程会占住目录 → 启动时删除失败 → WebView2 初始化
    # 异常 → 页面异常/崩溃。固定目录可复用,无删除冲突,二次启动更快)
    _webview_cache = os.path.join(
        os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)),
        ".webview2-cache")

    def _wait_webview2_exit(timeout: float = 5.0) -> None:
        """退出前等待本程序启动的 WebView2 子进程(msedgewebview2.exe)结束。

        PyInstaller onefile 的 bootloader 在 app 退出后立即递归删除
        %TEMP%\\_MEIxxxx 解包目录;若 msedgewebview2.exe 尚未退出并仍占用该
        目录内文件,删除失败 → windowed 模式下弹 "Failed to remove temporary
        directory" 警告(见 v6.22.2 源码 pyi_main_onefile_parent_cleanup)。

        主路径:启动时 _record_webview2_pids() 已把本程序进程树内的 WebView2
        PID 记进 _SELF_WEBVIEW2_PIDS,这里按 PID 轮询等其自然退出;等待中每轮
        补收进程树新 PID(WebView2 可能重启渲染进程);超时后 TerminateProcess
        强杀残余 → 强制释放 _MEI 目录句柄 → bootloader 清理不再弹警告。
        兜底:列表为空(启动后立即退出,记录线程来不及落盘)时回退 wmic +
        命令行 marker 检测。仅操作本程序进程树内的 PID,绝不误伤其它应用。
        """
        if not getattr(sys, "frozen", False):
            return
        deadline = time.time() + timeout
        # 阶段1:给 _record_webview2_pids 线程一点时间把 PID 写入列表
        # (冷启动 WebView2 起来慢,用户快速关闭时记录线程可能尚未落盘;
        #  等不到就回退 wmic,但主路径优先,绝不依赖 wmic 是否可用)
        fill_end = time.time() + min(2.5, timeout * 0.6)
        while not _SELF_WEBVIEW2_PIDS and time.time() < fill_end:
            time.sleep(0.2)
        pids = list(_SELF_WEBVIEW2_PIDS)
        if pids:
            # 主路径:按 PID 等自然退出;每轮重读记录列表合并新 PID,
            # 并补收进程树新增 WebView2(WebView2 可能重启渲染进程)
            while time.time() < deadline:
                try:
                    pids = list(dict.fromkeys(pids + list(_SELF_WEBVIEW2_PIDS)))
                    alive = [p for p in pids if _pid_alive(p)]
                    for p in _collect_webview2_pids(os.getpid()):
                        if p not in pids:
                            pids.append(p)
                    if not alive:
                        return  # 本程序的 WebView2 已全部退出,句柄已释放
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.3)
            # 超时:强杀残余 WebView2,强制释放 _MEI 目录句柄
            for p in pids:
                try:
                    _terminate_pid(p)
                except Exception:  # noqa: BLE001
                    pass
            return
        # 兜底:启动后立即退出、PID 记录缺失时,用 wmic + 命令行 marker 检测
        import subprocess as _sp
        marker = _webview_cache.lower()
        try:
            while time.time() < deadline:
                try:
                    out = _sp.run(
                        ["wmic", "process", "where", "name='msedgewebview2.exe'",
                         "get", "ProcessId,CommandLine", "/format:csv"],
                        capture_output=True, text=True, timeout=3,
                        creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
                    text = (out.stdout or "")
                except Exception:  # noqa: BLE001 wmic 不存在/被禁用
                    time.sleep(0.5)
                    return
                if not _own_webview2_alive(text, marker):
                    return  # 本程序的 WebView2 已全部退出,句柄已释放
                time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass

    class Api:
        """前端可调用的桌面能力(js_api 注入 window.pywebview.api)。"""

        def open_dir(self, name: str = "") -> None:
            """在系统文件管理器中打开目录(默认 exe 目录,probe 为抓取诊断输出)。"""
            import subprocess
            try:
                import campusnet as cn
                base = cn.BASE_DIR
            except Exception:  # noqa: BLE001
                base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
                    else os.path.dirname(os.path.abspath(__file__))
            target = os.path.join(base, os.path.basename(name)) if name else base
            target = os.path.abspath(target)
            if os.path.isfile(target):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            else:
                os.makedirs(target, exist_ok=True)
                subprocess.Popen(["explorer", os.path.normpath(target)])

        def notify(self, msg: str) -> None:
            """系统级桌面通知。"""
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, str(msg), "校园网无感认证", 0x40)
            except Exception:  # noqa: BLE001
                pass

        def get_platform(self) -> str:
            return sys.platform

        # ---- 无边框自绘标题栏的窗口控制(前端交通灯按钮调用) ----
        def win_minimize(self) -> None:
            _win().minimize()

        def win_maximize_toggle(self) -> None:
            w = _win()
            try:
                import ctypes
                hwnd = _find_hwnd()
                if hwnd:
                    if ctypes.windll.user32.IsZoomed(hwnd):
                        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    else:
                        ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
                    return
            except Exception:  # noqa: BLE001
                pass
            if w.state == "maximized":
                w.restore()
            else:
                w.maximize()

        def win_close(self) -> None:
            _win().destroy()

    _win_ref: list = []

    def _win() -> webview.Window:
        return _win_ref[0]

    window = webview.create_window(
        "CampusNetAuth · 校园网无感认证",
        url,
        width=1060,
        height=700,
        min_size=(1000, 620),   # 与前端布局对齐,防排版错乱
        frameless=True,          # 无 Windows 原生边框/标题栏(窗口逻辑由 WndProc 子类接管)
        easy_drag=True,          # JS 侧拖动：仅 .drag-region（标题栏）命中拖动；配合 DRAG_REGION_DIRECT_TARGET_ONLY
        text_select=True,        # 允许文字框选/复制
        js_api=Api(),
        background_color="#f5f5f7",
        confirm_close=False,
        hidden=True,             # 关键:窗口先隐藏,等 WebView2 初始化完成再显示,零闪启动
    )
    _win_ref.append(window)
    # 关闭窗口(含自绘关闭按钮)时停止后端服务
    window.events.closed += lambda: (srv.shutdown(), srv.server_close())

    def _find_hwnd():
        import ctypes
        ctypes.windll.user32.FindWindowW.restype = ctypes.c_void_p  # 句柄是 64 位指针,防截断
        return ctypes.windll.user32.FindWindowW(None, "CampusNetAuth · 校园网无感认证")

    def _show_window_via_ctypes() -> None:
        """用 Win32 API 直接强制显示主窗口,居中于屏幕;同时把句柄注册给
        gui_server,让前端 HTTP /api/window(最小化/最大化/关闭)可用。"""
        try:
            import ctypes
            hwnd = _find_hwnd()
            if hwnd:
                try:
                    gui_server.set_main_hwnd(int(hwnd))  # 窗口控制 HTTP 端点依赖此句柄
                except Exception:  # noqa: BLE001
                    pass
                if _shown[0]:  # 已显示过 → 不再强制居中/重置尺寸(尊重用户已摆好的窗口)
                    return
                _shown[0] = True
                SWP_NOZORDER = 0x4
                sw = ctypes.windll.user32.GetSystemMetrics(0)   # SM_CXSCREEN
                sh = ctypes.windll.user32.GetSystemMetrics(1)   # SM_CYSCREEN
                x = (sw - 1060) // 2
                y = (sh - 700) // 2
                ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, 1060, 700, SWP_NOZORDER)
                ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
        except Exception:  # noqa: BLE001
            pass

    _shown = [False]          # 一次性显示标志:loaded 与 5s 兜底只生效一次
    _wnd_installed = [False]  # 窗口逻辑安装标志:防止 loaded+兜底双重子类化 WndProc

    MIN_W, MIN_H = 1000, 620  # 窗口最小尺寸(与 create_window min_size 对齐)
    TITLEBAR_H = 38          # HTML 自绘标题栏高度(px,见 gui_server :root --tb-h)
    BTN_AREA_W = 100         # 右侧交通灯按钮区宽度(最小化/最大化/关闭,需可点击,不能当标题栏拖)
    EDGE = 8                 # 窗口边缘可拖拽调大小的宽度(px)

    _ORIG_WNDPROC: int = 0
    _WNDPROC_REFS: list = []  # 全局引用,防止回调被 GC 导致窗口崩溃

    def _install_window_logic() -> None:
        """把窗口逻辑完整交给 Windows 系统(官方自定义标题栏方案,同 Electron):

        - 补回 WS_THICKFRAME|WS_CAPTION|WS_MINIMIZEBOX|WS_MAXIMIZEBOX|WS_SYSMENU 样式,
          让系统把它当成"标准窗口"→ Aero Snap 吸附、拖到顶部最大化、双击标题栏
          最大化、最大化后拖拽还原、Alt+Space 系统菜单等全部原生可用;
        - WM_NCCALCSIZE 返回 0 → 系统标题栏/边框不绘制,客户区=整个窗口
          (外观仍是我们自绘的标题栏,最大化时精确填满 work area,无边框偏移);
        - WM_NCHITTEST → 顶部标题栏区返回 HTCAPTION(系统接管拖动/双击最大化),
          边缘返回 resize 手柄(四边/四角可拖拽调大小),
          右侧按钮区返回 HTCLIENT(HTML 按钮正常点击);
        - WM_GETMINMAXINFO → 最小尺寸(原生限制,不再需要守护线程)。
        """
        if _wnd_installed[0]:  # 已安装过:避免 loaded 与 5s 兜底重复子类化
            return
        _wnd_installed[0] = True
        nonlocal _ORIG_WNDPROC
        import ctypes

        hwnd = _find_hwnd()
        if not hwnd:
            if args.dev:
                print("[desktop-window] 未找到窗口,跳过窗口逻辑安装", flush=True)
            return

        # ---------- 结构体定义 ----------
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT),
                        ("ptMaxPosition", POINT), ("ptMinTrackSize", POINT),
                        ("ptMaxTrackSize", POINT)]

        class NCCALCSIZE_PARAMS(ctypes.Structure):
            _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        # ---------- 消息/命中常量 ----------
        WM_NCCALCSIZE = 0x0083
        WM_NCHITTEST = 0x0084
        WM_GETMINMAXINFO = 0x0024
        HTCLIENT = 1
        HTCAPTION = 2
        HTLEFT, HTRIGHT = 10, 11
        HTTOP, HTTOPLEFT, HTTOPRIGHT = 12, 13, 14
        HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17

        user32 = ctypes.windll.user32
        LRESULT = ctypes.c_longlong
        # 64 位关键:返回指针/句柄的 API 不设 restype 会被截断成 32 位;
        # 传指针参数的 API 不设 argtypes 会把 64 位地址按 c_int 转换 → OverflowError
        user32.SetWindowLongPtrW.restype = LRESULT
        user32.GetWindowLongPtrW.restype = LRESULT
        user32.CallWindowProcW.restype = LRESULT
        user32.SendMessageW.restype = LRESULT
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_uint64, ctypes.c_int64]
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_uint64, ctypes.c_int64]

        # 子类化窗口过程:lparam 可能为负(多显示器),用有符号 64 位接收
        WNDPROC_T = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int64)

        # 回调运行状态:rect 缓存(WM_NCHITTEST 高频) + 快速失败保护
        import time as _t2
        _st = {
            "rect": None,        # (left, top, width, height) 缓存
            "rect_ts": 0.0,      # 缓存时间戳
            "err": 0,            # 连续异常次数
            "dead": False,       # 异常过多 → 恢复原 proc,回调降级为纯透传
        }

        @WNDPROC_T
        def wndproc(h, msg, wparam, lparam):
            try:
                if _st["dead"]:
                    return user32.CallWindowProcW(_ORIG_WNDPROC, h, msg, wparam, lparam)
                if msg == WM_NCCALCSIZE:
                    # 隐藏系统标题栏/边框:客户区=整个窗口。
                    # 最大化时把 proposed rect 精确对准 work area,
                    # 消除"最大化后内容溢出/偏移"的问题。
                    if wparam and user32.IsZoomed(h):
                        params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
                        mon = user32.MonitorFromWindow(h, 2)  # MONITOR_DEFAULTTONEAREST
                        mi = MONITORINFO()
                        mi.cbSize = ctypes.sizeof(MONITORINFO)
                        if user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                            params.rgrc[0] = mi.rcWork
                    return 0

                if msg == WM_NCHITTEST:
                    # lparam = 屏幕坐标(low= x, high= y,有符号 16 位)
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    # 窗口位置缓存(鼠标高频移动时避免反复 GetWindowRect)
                    now = _t2.time()
                    if _st["rect"] is None or now - _st["rect_ts"] > 0.05:
                        rr = RECT()
                        user32.GetWindowRect(h, ctypes.byref(rr))
                        _st["rect"] = (rr.left, rr.top, rr.right - rr.left, rr.bottom - rr.top)
                        _st["rect_ts"] = now
                    lft, top, w, hgt = _st["rect"]
                    cx = x - lft
                    cy = y - top
                    if not user32.IsZoomed(h):  # 最大化窗口不能 resize
                        if cx <= EDGE and cy <= EDGE:
                            return HTTOPLEFT
                        if cx >= w - EDGE and cy <= EDGE:
                            return HTTOPRIGHT
                        if cx <= EDGE and cy >= hgt - EDGE:
                            return HTBOTTOMLEFT
                        if cx >= w - EDGE and cy >= hgt - EDGE:
                            return HTBOTTOMRIGHT
                        if cy <= EDGE:
                            return HTTOP
                        if cy >= hgt - EDGE:
                            return HTBOTTOM
                        if cx <= EDGE:
                            return HTLEFT
                        if cx >= w - EDGE:
                            return HTRIGHT
                    # 顶部标题栏(非按钮区)→ HTCAPTION:系统原生拖动/
                    # Aero Snap/双击最大化/最大化后拖拽还原
                    if cy < TITLEBAR_H and cx < w - BTN_AREA_W:
                        return HTCAPTION
                    return HTCLIENT

                if msg == WM_GETMINMAXINFO:
                    mm = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
                    mm.ptMinTrackSize.x = MIN_W
                    mm.ptMinTrackSize.y = MIN_H
                    return 0

                # 其他消息:窗口位置变化时失效缓存
                if msg in (0x0003, 0x0005, 0x0007):  # WM_MOVE / WM_SIZE / WM_SHOWWINDOW
                    _st["rect"] = None
            except Exception:  # noqa: BLE001
                # 快速失败保护:回调连续出错说明子类化与宿主不兼容,
                # 恢复原窗口过程,降级为纯透传(回到 pywebview 默认行为),避免崩溃/卡死
                _st["err"] += 1
                if _st["err"] >= 20 and _ORIG_WNDPROC:
                    try:
                        _st["dead"] = True
                        user32.SetWindowLongPtrW(h, GWL_WNDPROC, _ORIG_WNDPROC)
                    except Exception:  # noqa: BLE001
                        pass
            if _ORIG_WNDPROC:
                return user32.CallWindowProcW(_ORIG_WNDPROC, h, msg, wparam, lparam)
            return 0

        # ---------- 安装子类 + 补标准窗口样式 ----------
        GWL_WNDPROC = -4
        GWL_STYLE = -16
        WS_THICKFRAME = 0x00040000   # 可调整大小边框(拖动边缘/Aero Snap 依赖)
        WS_MINIMIZEBOX = 0x00020000
        WS_MAXIMIZEBOX = 0x00010000
        WS_SYSMENU = 0x00080000      # Alt+Space 系统菜单
        WS_CAPTION = 0x00C00000      # 标题栏样式位(被 NCCALCSIZE 隐藏,只影响系统逻辑)
        style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        if style:
            user32.SetWindowLongPtrW(
                hwnd, GWL_STYLE,
                style | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
                       | WS_SYSMENU | WS_CAPTION)
        wndproc_ptr = ctypes.cast(wndproc, ctypes.c_void_p).value
        _ORIG_WNDPROC = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, wndproc_ptr)
        _WNDPROC_REFS.append(wndproc)  # 防 GC,保活回调对象
        if args.dev:
            print(f"[desktop-window] SetWindowLongPtrW(GWL_WNDPROC) → 原proc={_ORIG_WNDPROC}, 新proc={wndproc_ptr}", flush=True)
        # 刷新窗口边框(应用新样式+子类,不改变位置/大小)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x4 | 0x1 | 0x2 | 0x20)  # NOZORDER|NOSIZE|NOMOVE|FRAMECHANGED

    # 窗口显示策略:仅启动时用 ctypes 居中显示一次;此后窗口逻辑完全由
    # 系统接管(WndProc 子类),用户可自由拖动/吸附/最大化/resize,不做任何强制纠正
    def _on_loaded():
        try:
            _show_window_via_ctypes()
            _install_window_logic()
        except Exception:  # noqa: BLE001
            pass
    window.events.loaded += _on_loaded

    # 兜底:loaded 不触发时,5 秒后也做显示 + 安装窗口逻辑
    def _fallback_show():
        import time as _t
        _t.sleep(5)
        _show_window_via_ctypes()
        _install_window_logic()
    threading.Thread(target=_fallback_show, daemon=True).start()

    # 窗口看门狗:窗口曾经存在、之后句柄消失(=窗口已关闭) → 强制退出进程。
    # 解决 pywebview 窗口关闭后 webview.start() 可能不返回(WebView2 环境
    # 清理挂起/崩溃转储卡住)导致的"关闭后进程残留/看起来卡死"问题。
    def _window_watchdog():
        import time as _t
        seen = False
        while True:
            try:
                _t.sleep(1)
                hwnd = _find_hwnd()
                if hwnd:
                    seen = True
                elif seen:
                    _wait_webview2_exit(4.0)  # 等 WebView2 子进程退出,防 _MEI 清理弹警告
                    os._exit(0)  # 窗口曾存在 → 现在消失了 → 已关闭,立即退出
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=_window_watchdog, daemon=True).start()

    try:
        _record_webview2_pids()  # 异步记录本程序 WebView2 子进程 PID(供退出等待,勿删)
        webview.start(debug=args.dev, private_mode=False, storage_path=_webview_cache)
        # 窗口已关闭:等 WebView2 子进程释放 _MEIPASS 句柄后立即退出进程
        # (不能只靠 main 返回——pywebview/.NET 的后台线程会阻止 Python 解释器
        # 退出,导致进程残留;必须 os._exit;先等待可避免 bootloader 清理弹警告)
        _wait_webview2_exit(5.0)
        os._exit(0)
    except Exception as exc:  # noqa: BLE001 桌面窗口启动失败(无 GUI 环境等)→ 降级浏览器模式
        if args.dev:
            print(f"[desktop] 桌面窗口启动失败({exc}),降级为浏览器模式")
        import webbrowser
        webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
