# -*- coding: utf-8 -*-
"""CampusNetAuth · Web GUI 后端（零第三方依赖，仅 stdlib http.server）

前端是一份内嵌的 Apple 风格 HTML（毛玻璃侧栏 / 圆角卡片 / iOS 配色 /
呼吸状态灯 / 自绘标题栏与气泡通知），由 desktop.py 用 pywebview 装进原生
无边框窗口；也可以直接用浏览器访问本服务的地址。

设计要点：
  * 前端资源内嵌为字符串，PyInstaller 打包时无需额外收集静态文件。
  * 耗时操作（自检 / 一键体检 / 抓取诊断）走后台任务 + 轮询，不阻塞请求。
  * 所有网络判定复用 campusnet 的业务逻辑，本文件只做转接与呈现。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import campusnet as cn  # noqa: E402

# pywebview 窗口句柄（desktop.py 注入），用于 HTTP 端点控制窗口
_MAIN_HWND = [0]
_LOG_TAIL = 200

# 后台任务：自检/体检等耗时操作，前端轮询 /api/task?id=xxx
_TASKS: dict = {}
_TASKS_LOCK = threading.Lock()
_TASK_TTL = 300  # 已完成任务保留秒数，超过自动清理（防 _TASKS 无限增长）


def _prune_tasks() -> None:
    """清理已结束且超时未取走的任务条目（status != running 且超过 TTL）。"""
    now = time.time()
    with _TASKS_LOCK:
        stale = [k for k, v in _TASKS.items()
                 if v.get("status") != "running"
                 and now - v.get("ts", 0) > _TASK_TTL]
        for k in stale:
            _TASKS.pop(k, None)


def set_main_hwnd(hwnd: int) -> None:
    _MAIN_HWND[0] = int(hwnd or 0)


# ============================ 业务转接 ============================

# 状态轮询探测缓存：/api/status 每 20s 轮询一次，而网络探测（连通性 +
# 在线信息）要发最多 4 个 HTTP。缓存 _PROBE_TTL 秒内复用结果，网络差时
# 轮询不会被 HTTP 超时拖成"一直读取状态"；手动「立即检测」不走此缓存。
_PROBE_CACHE = {"t": 0.0, "online": None, "info": {}}
_PROBE_TTL = 5.0


def _probe_network(cfg) -> tuple:
    now = time.time()
    if (now - _PROBE_CACHE["t"] < _PROBE_TTL
            and _PROBE_CACHE["online"] is not None):
        return _PROBE_CACHE["online"], _PROBE_CACHE["info"]
    client = cn.PortalClient(cfg)
    online, _loc = client.check_online()
    info = client.interface("getOnlineUserInfo") if online else None
    if not isinstance(info, dict):
        info = {}
    _PROBE_CACHE.update({"t": now, "online": online, "info": info})
    return online, info


def _wake_daemon() -> None:
    """向守护进程发 CHECK 唤醒指令：手动登录/操作后让守护立即复核一轮，
    状态同步从"下一轮周期（≤30s）"变为即时。守护不在运行时静默忽略。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", cn.SINGLETON_PORT))
        s.sendall(b"CHECK")
        s.close()
    except Exception:
        pass


def _status_payload() -> dict:
    cfg = cn.load_config()
    online, info = _probe_network(cfg)
    # 空文件夹提醒只出现一次：先取本会话的"无关文件"快照，再落盘部署标记
    # （state.json），第二次启动起 first_deploy() 为 False → 不再提醒。
    foreign = cn.foreign_files()
    if cn.FIRST_DEPLOY:
        cn.mark_first_run()
    return {
        "online": online,
        "info": info,
        "autostart": cn.autostart_status(),
        # 自启链路健康检查：Run 键 → 启动器 → exe 文件完整性（缺失时前端红色警示）
        "autostart_health": cn.autostart_health(),
        "daemon": cn.daemon_running(),
        "daemon_pid": cn.daemon_pid(),
        "username": cfg.get("username", ""),
        "service": cfg.get("service", ""),
        "host": "%s:%s" % (cfg.get("portal_host", ""),
                           cfg.get("portal_port", 80)),
        "interval": cfg.get("interval", ""),
        "cfg": cfg,
        "components": cn.components_status(),
        "first_deploy": cn.FIRST_DEPLOY,
        "foreign_files": foreign,
        # 守护最近一次"致命登录失败"提示（账号/密码问题等），成功后自动清除
        "last_error": cn.load_state().get("last_error"),
        # 最近状态事件（掉线/重连/恢复/登录结果），守护状态翻转时写入
        "events": cn.load_state().get("events") or [],
    }


def _tail_log(n: int = _LOG_TAIL) -> str:
    """从日志文件尾部读取最后 n 行（seek 到尾部，不整读全文件；
    日志经 RotatingFileHandler 轮转后长期受控，但尾部读取仍是更省的姿势）。"""
    try:
        with open(cn.LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # 最多读尾部 256KB，足够覆盖 n=200 行日志
            f.seek(max(0, size - 256 * 1024))
            tail = f.read()
        lines = tail.decode("utf-8", "replace").splitlines()
        if not lines:
            return ""
        return "\n".join(lines[-n:]) + "\n"
    except Exception as e:  # noqa: BLE001
        return "（暂无日志）%r" % e


def _spawn(cmd: list, cwd: str = None) -> None:
    """以完全无窗口、脱离父进程的方式启动子进程。

    注意：本程序打包为 GUI 子系统（--windowed），若以普通方式启动 CUI
    子进程（python.exe 等），Windows 会为其分配控制台，默认终端为
    Windows Terminal 时会闪出终端窗口。故必须带 CREATE_NO_WINDOW。
    """
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
             | getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    subprocess.Popen(cmd, cwd=cwd or cn.BASE_DIR,
                     creationflags=flags, close_fds=True)


def _start_task(fn) -> str:
    tid = uuid.uuid4().hex[:12]
    with _TASKS_LOCK:
        _TASKS[tid] = {"status": "running", "output": ""}

    def run():
        try:
            out = fn()
            with _TASKS_LOCK:
                _TASKS[tid] = {"status": "done", "output": out,
                               "ts": time.time()}
        except Exception as e:  # noqa: BLE001
            with _TASKS_LOCK:
                _TASKS[tid] = {"status": "error", "output": "%r" % e,
                               "ts": time.time()}

    threading.Thread(target=run, daemon=True).start()
    return tid


def _task_self_test() -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "test"]
    else:
        cmd = [sys.executable, os.path.join(cn.BASE_DIR, "campusnet.py"),
               "test"]
    r = subprocess.run(cmd, capture_output=True, timeout=120,
                       creationflags=flags, cwd=cn.BASE_DIR)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    return out + err


def _task_health() -> str:
    """一键体检：启动守护 → 下线 → 等守护自动重连（真端到端验证）"""
    lines = []
    if not cn.daemon_running():
        lines.append("启动后台守护…")
        cn.start_daemon()
        time.sleep(3)
    if not cn.daemon_running():
        return "\n".join(lines + ["守护未能启动，体检中止"])
    lines.append("守护已运行，正在下线以触发自动重连…")
    # 走与 cmd_logout 一致的流程
    class _A:
        pass
    rc = cn.cmd_logout(_A())
    lines.append("下线命令返回码=%s" % rc)
    ok = False
    for i in range(75):
        time.sleep(1)
        cfg = cn.load_config()
        online, _ = cn.PortalClient(cfg).check_online()
        if online:
            lines.append("第 %d 秒：守护已自动重连成功 ✓" % (i + 1))
            ok = True
            break
    if not ok:
        lines.append("75 秒内未自动重连，请查看日志")
    return "\n".join(lines)


# ============================ HTTP ============================

class Handler(BaseHTTPRequestHandler):
    server_version = "CampusNetAuth/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志，避免刷屏
        pass

    # ---- 工具 ----
    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return {}

    # ---- GET ----
    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        try:
            if path in ("/", "/index.html"):
                self._send(INDEX_HTML.encode("utf-8"),
                           "text/html; charset=utf-8")
            elif path == "/api/status":
                self._json(_status_payload())
            elif path == "/api/log":
                n = _LOG_TAIL
                for kv in qs.split("&"):
                    if kv.startswith("n="):
                        try:
                            n = max(1, int(kv[2:]))
                        except Exception:  # noqa: BLE001
                            pass
                self._json({"text": _tail_log(n)})
            elif path == "/api/task":
                tid = ""
                for kv in qs.split("&"):
                    if kv.startswith("id="):
                        tid = kv[3:]
                _prune_tasks()
                with _TASKS_LOCK:
                    self._json(_TASKS.get(tid, {"status": "missing",
                                                "output": ""}))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": "%r" % e}, 500)

    # ---- POST ----
    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        data = self._body()
        try:
            if path == "/api/config":
                self._save_config(data)
            elif path == "/api/connect":
                self._connect(data)
            elif path == "/api/logout":
                self._json({"ok": True,
                            "rc": cn.cmd_logout(type("_A", (), {})())})
            elif path == "/api/daemon/start":
                if not cn.daemon_component_installed():
                    self._json({"ok": False,
                                "error": "需要先安装「值守组件」（自动化 → 组件管理）"})
                else:
                    self._json({"ok": cn.start_daemon()})
            elif path == "/api/daemon/stop":
                self._json({"ok": cn.stop_daemon()})
            elif path == "/api/daemon/restart":
                if not cn.daemon_component_installed():
                    self._json({"ok": False,
                                "error": "需要先安装「值守组件」（自动化 → 组件管理）"})
                else:
                    cn.stop_daemon()
                    time.sleep(1)
                    self._json({"ok": cn.start_daemon()})
            elif path == "/api/autostart/install":
                self._json({"ok": cn.install_autostart()})
            elif path == "/api/autostart/uninstall":
                self._json({"ok": cn.uninstall_autostart()})
            elif path == "/api/components/install":
                ok, msg = cn.install_component((data or {}).get("name", ""))
                self._json({"ok": ok, "message": msg,
                            "components": cn.components_status()})
            elif path == "/api/components/uninstall":
                ok, msg = cn.uninstall_component((data or {}).get("name", ""))
                self._json({"ok": ok, "message": msg,
                            "components": cn.components_status()})
            elif path == "/api/self-test":
                self._json({"id": _start_task(_task_self_test)})
            elif path == "/api/health":
                self._json({"id": _start_task(_task_health)})
            elif path == "/api/diagnose":
                if getattr(sys, "frozen", False):
                    _spawn([sys.executable, "diagnose"])
                else:
                    _spawn([sys.executable,
                            os.path.join(cn.BASE_DIR, "campusnet.py"),
                            "diagnose"])
                self._json({"ok": True})
            elif path == "/api/open-dir":
                try:
                    os.startfile(cn.BASE_DIR)  # noqa: S606
                    self._json({"ok": True})
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": "%r" % e})
            elif path == "/api/window":
                self._window(data.get("action", ""))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": "%r" % e}, 500)

    # ---- 具体实现 ----
    def _save_config(self, data: dict) -> None:
        cfg = cn.load_config()
        if "host" in data:
            host, _, port = str(data["host"]).partition(":")
            cfg["portal_host"] = host.strip()
            if port.strip().isdigit():
                cfg["portal_port"] = int(port)
        for key in ("username", "service", "interval", "offline_interval",
                    "retry_interval", "max_interval", "timeout"):
            if key in data and str(data[key]) != "":
                v = data[key]
                if key in ("interval", "offline_interval", "retry_interval",
                           "max_interval", "timeout"):
                    try:
                        cfg[key] = int(v)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    cfg[key] = str(v)
        cn.save_config(cfg)
        # 密码：留空表示不修改
        pwd = data.get("password") or ""
        if pwd:
            cn.CredentialStore().save(pwd)
        self._json({"ok": True})

    def _connect(self, data: dict) -> None:
        self._save_config(data)
        cfg = cn.load_config()
        cred = cn.CredentialStore().load()
        if not cfg.get("username") or not cred:
            self._json({"ok": False, "error": "尚未配置账号密码"})
            return
        client = cn.PortalClient(cfg)
        online, _ = client.check_online()
        if online:
            _wake_daemon()  # 告知守护：已在线，立即复核一轮
            self._json({"ok": True, "result": "already_online"})
            return
        res = client.login(cfg["username"], cred)
        if res.get("result") == "success":
            _wake_daemon()  # 登录成功 → 唤醒守护即时同步状态
            if res.get("userIndex"):
                cn.update_state({"userIndex": res["userIndex"],
                                 "loginTime": time.strftime(
                                     "%Y-%m-%d %H:%M:%S")},
                                drop=("last_error",))
            self._json({"ok": True, "result": "success",
                        "service": res.get("_service")})
        else:
            self._json({"ok": False,
                        "error": str(res.get("message") or "登录失败"),
                        "hint": res.get("_hint", "")})

    def _window(self, action: str) -> None:
        """窗口控制：优先用 pywebview js_api 内部的 HTTP 端点（由前端调用），
        这里提供基于 Win32 句柄的兜底实现。"""
        hwnd = _MAIN_HWND[0]
        if not hwnd:
            self._json({"ok": False, "error": "no hwnd"})
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            if action == "minimize":
                u.ShowWindow(hwnd, 6)      # SW_MINIMIZE
            elif action == "maximize":
                if u.IsZoomed(hwnd):
                    u.ShowWindow(hwnd, 9)  # SW_RESTORE
                else:
                    u.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            elif action == "close":
                u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            self._json({"ok": True})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": "%r" % e})


def find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int = 0, host: str = "127.0.0.1"):
    """启动 GUI 服务，返回 (server, url)"""
    port = port or find_free_port()
    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % port


# ============================ 前端 ============================

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CampusNetAuth · 校园网无感认证</title>
<style>
:root{
  --bg:#f5f5f7; --card:#fff; --secondary:#f2f2f6; --border:#d2d2d7;
  --text:#1d1d1f; --muted:#5f5f66; --muted2:#84848a;
  --primary:#007AFF; --primary-dark:#0066d6; --primary-50:#e8f1ff;
  --success:#1f9d45; --success-50:#e7f8ec; --danger:#e0332a;
  --danger-50:#ffeceb; --warn:#c26a00;
  --radius:18px; --radius-md:12px; --radius-sm:9px;
  --shadow:0 1px 2px rgba(0,0,0,.03),0 8px 24px rgba(0,0,0,.05);
  --shadow-lg:0 12px 40px rgba(0,0,0,.14);
  --tb-h:38px; --side-w:216px;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",
    "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  font-size:13px;line-height:1.5;overflow:hidden;
  -webkit-font-smoothing:antialiased;
}
button{font-family:inherit;font-size:inherit;border:0;background:none;
  cursor:pointer;color:inherit}
input{font-family:inherit;font-size:inherit}

/* ---------- 自绘标题栏 ---------- */
.titlebar{
  height:var(--tb-h);display:flex;align-items:center;
  padding:0 14px;user-select:none;position:relative;z-index:50;
  background:rgba(246,246,248,.82);
  backdrop-filter:blur(30px) saturate(180%);
  -webkit-backdrop-filter:blur(30px) saturate(180%);
  border-bottom:.5px solid rgba(0,0,0,.07);
}
.tb-brand{display:flex;align-items:center;gap:8px;flex:1;min-width:0}
.tb-dot{width:9px;height:9px;border-radius:50%;background:var(--primary);
  box-shadow:0 0 0 3px var(--primary-50);flex:none}
.tb-title{font-size:12.5px;font-weight:600;letter-spacing:.2px}
.tb-sub{font-size:11.5px;color:var(--muted);margin-left:2px}
.tb-btns{display:flex;gap:8px;align-items:center}
.tb-btn{width:12px;height:12px;border-radius:50%;position:relative;
  transition:filter .15s}
.tb-btn:hover{filter:brightness(.92)}
.tb-close{background:#FF5F57}.tb-min{background:#FEBC2E}.tb-max{background:#28C840}
.tb-btn:active{transform:scale(.94)}

/* ---------- 布局 ---------- */
.app{display:flex;height:calc(100% - var(--tb-h))}
.sidebar{
  width:var(--side-w);flex:none;display:flex;flex-direction:column;
  padding:18px 12px 14px;
  background:rgba(246,246,248,.72);
  backdrop-filter:blur(30px) saturate(180%);
  -webkit-backdrop-filter:blur(30px) saturate(180%);
  border-right:.5px solid rgba(0,0,0,.07);
}
.side-head{padding:0 8px 18px}
.side-logo{font-size:15px;font-weight:700;letter-spacing:.2px}
.side-logo-sub{font-size:11px;color:var(--muted);margin-top:3px}
.nav{display:flex;flex-direction:column;gap:3px}
.nav-item{
  display:flex;align-items:center;gap:10px;padding:9px 11px;
  border-radius:11px;color:var(--muted);font-size:13px;font-weight:500;
  transition:background .18s,color .18s;text-align:left;width:100%;
}
.nav-item:hover{background:rgba(255,255,255,.6);color:var(--text)}
.nav-item.active{
  background:var(--card);color:var(--primary);font-weight:600;
  box-shadow:0 1px 2px rgba(0,0,0,.04),0 4px 12px rgba(0,0,0,.05);
}
.nav-ico{width:17px;height:17px;flex:none;opacity:.9}
.side-foot{margin-top:auto;padding:12px 10px 0;font-size:10.5px;
  color:var(--muted);line-height:1.55;
  border-top:.5px solid rgba(0,0,0,.06)}

.main{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{
  height:46px;flex:none;display:flex;align-items:center;
  justify-content:space-between;padding:0 22px;
}
.topbar-sub{font-size:12px;color:var(--muted)}
.state{display:flex;align-items:center;gap:7px;font-size:12.5px;
  font-weight:700}
.state .ok{color:var(--success)}
.state .warn{color:var(--warn)}
.state .err{color:var(--danger)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted2);
  flex:none;transition:background .3s}
.dot.live{animation:breathe 2s ease-in-out infinite}
@keyframes breathe{0%,100%{opacity:1}50%{opacity:.3}}
.dot.green{background:var(--success)}
.dot.red{background:var(--danger)}
.dot.orange{background:var(--warn)}

.content{flex:1;overflow-y:auto;padding:0 22px 22px;scroll-behavior:smooth}
.content::-webkit-scrollbar{width:9px}
.content::-webkit-scrollbar-thumb{background:rgba(0,0,0,.16);
  border-radius:9px;border:2px solid transparent;background-clip:padding-box}
.content::-webkit-scrollbar-thumb:hover{background:rgba(0,0,0,.3);
  background-clip:padding-box;border:2px solid transparent}
.content::-webkit-scrollbar-track{background:transparent}

.page{display:none;animation:rise .34s cubic-bezier(.22,1,.36,1)}
.page.active{display:block}
@keyframes rise{from{opacity:0;transform:translateY(9px)}
  to{opacity:1;transform:none}}

/* ---------- 卡片 ---------- */
.card{
  background:var(--card);border-radius:var(--radius);
  padding:20px 22px;box-shadow:var(--shadow);margin-bottom:16px;
}
.card-title{font-size:14.5px;font-weight:700;letter-spacing:.1px}
.card-sub{font-size:12px;color:var(--muted);margin-top:5px}
.page-title{font-size:21px;font-weight:700;letter-spacing:.2px;
  padding:2px 2px 15px}

/* ---------- 表单 ---------- */
.field{margin-top:16px}
.label{font-size:12px;color:var(--muted);margin-bottom:7px;
  font-weight:600}
.input{
  width:100%;padding:9px 13px;border-radius:var(--radius-sm);
  border:.5px solid var(--border);background:var(--card);
  color:var(--text);outline:none;transition:border-color .2s,box-shadow .2s;
  font-size:13px;
}
.input:focus{border-color:var(--primary);
  box-shadow:0 0 0 3.5px var(--primary-50)}
.input::placeholder{color:var(--muted2)}
.row{display:flex;gap:12px;align-items:center}

/* ---------- 分段控件 ---------- */
.seg{display:inline-flex;background:var(--secondary);padding:2.5px;
  border-radius:11px;gap:2px}
.seg button{
  padding:6px 15px;border-radius:9px;font-size:12.5px;font-weight:500;
  color:var(--muted);transition:all .2s cubic-bezier(.22,1,.36,1);
}
.seg button.active{
  background:var(--card);color:var(--text);font-weight:600;
  box-shadow:0 1px 3px rgba(0,0,0,.09);
}

/* ---------- 按钮 ---------- */
.btn{
  padding:8px 17px;border-radius:var(--radius-sm);font-size:12.5px;
  font-weight:600;transition:all .18s cubic-bezier(.22,1,.36,1);
  white-space:nowrap;
}
.btn-primary{background:var(--primary);color:#fff;
  box-shadow:0 1px 2px rgba(0,122,255,.28),0 4px 12px rgba(0,122,255,.2)}
.btn-primary:hover{background:var(--primary-dark);transform:translateY(-1px);
  box-shadow:0 2px 4px rgba(0,122,255,.32),0 7px 18px rgba(0,122,255,.26)}
.btn-ghost{background:var(--secondary);color:var(--text)}
.btn-ghost:hover{background:#e8e8ed;transform:translateY(-1px)}
.btn-danger{background:var(--danger-50);color:var(--danger)}
.btn-danger:hover{background:#ffdcd9;transform:translateY(-1px)}
.btn:active{transform:translateY(0) scale(.98)}
.btn:disabled{opacity:.42;cursor:not-allowed;transform:none!important}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}

/* ---------- 开关 ---------- */
.switch{width:47px;height:28px;border-radius:14px;background:#e5e5ea;
  position:relative;transition:background .26s cubic-bezier(.22,1,.36,1);
  flex:none}
.switch::after{
  content:"";position:absolute;top:2px;left:2px;width:24px;height:24px;
  border-radius:50%;background:#fff;
  box-shadow:0 2px 5px rgba(0,0,0,.18),0 1px 2px rgba(0,0,0,.1);
  transition:transform .26s cubic-bezier(.22,1,.36,1);
}
.switch.on{background:var(--success)}
.switch.on::after{transform:translateX(19px)}

/* ---------- 状态项 ---------- */
.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px 22px}
.stat-label{font-size:11.5px;color:var(--muted);font-weight:500}
.stat-value{font-size:14.5px;font-weight:650;margin-top:5px;
  letter-spacing:.1px}
.stat-value.ok{color:var(--success)}
.stat-value.err{color:var(--danger)}
.stat-value.warn{color:var(--warn)}
.daemon-line{display:flex;align-items:center;gap:9px;font-size:13px;
  font-weight:600;margin-top:4px}
.ds.run{color:var(--success)}
.ds.stop{color:var(--muted)}
.hint{font-size:11px;color:var(--muted)}

/* ---------- 组件管理 / 部署提示 ---------- */
.deploy-banner{
  display:none;align-items:flex-start;gap:12px;
  background:#fff7e6;border:1px solid rgba(255,159,39,.45);
  color:#7a4d0b;border-radius:var(--radius-md);padding:10px 14px;
  font-size:12px;line-height:1.65;margin-bottom:16px;
}
/* 状态警示条：守护异常 / 登录失败提示（复用部署横幅视觉） */
.warn-bar{
  display:flex;align-items:center;gap:12px;
  background:#fff7e6;border:1px solid rgba(255,159,39,.45);
  color:#7a4d0b;border-radius:var(--radius-md);padding:10px 14px;
  font-size:12px;line-height:1.6;
}
.warn-bar.err{
  background:#fff0f0;border-color:rgba(255,59,48,.4);color:#a33;
}
/* 最近状态事件条：掉线/重连/恢复/登录结果即时展示 */
.evt-bar{
  display:flex;flex-direction:column;gap:4px;
  background:#f6f7f9;border:1px solid var(--border);
  border-radius:var(--radius-md);padding:8px 12px;
  font-size:12px;line-height:1.55;
}
.evt-bar .evt-cap{font-size:11px;color:var(--muted);font-weight:600}
.evt-item{display:flex;align-items:center;gap:8px;color:#444;white-space:nowrap}
.evt-item b{width:14px;height:14px;border-radius:50%;flex-shrink:0;
  display:inline-flex;align-items:center;justify-content:center;
  font-size:9px;color:#fff;background:#bbb}
.evt-item.ok b{background:#34c759}
.evt-item.err b{background:#ff3b30}
.evt-item.warn b{background:#ff9500}
.evt-item i{font-style:normal;font-size:10px;color:var(--muted);margin-left:auto;flex-shrink:0}
.deploy-banner b{font-weight:700}
.comp-row{
  display:flex;align-items:center;gap:10px;padding:11px 2px;
  border-bottom:1px solid var(--border);
}
.comp-row:last-child{border-bottom:none}
.comp-info{flex:1;min-width:0}
.comp-name{font-size:13px;font-weight:650}
.comp-sub{font-size:11px;color:var(--muted);margin-top:2px;line-height:1.5}
.comp-state{
  font-size:11.5px;font-weight:600;flex:none;padding:3px 9px;
  border-radius:20px;background:var(--secondary);color:var(--muted);
  white-space:nowrap;
}
.comp-state.ok{background:#e3f6e8;color:var(--success)}
.comp-state.warn{background:#fff3d6;color:var(--warn)}

/* ---------- 日志 ---------- */
.log{
  background:#1c1c1e;color:#e8e8ed;border-radius:var(--radius-md);
  padding:14px 16px;font-family:"SF Mono",Menlo,Consolas,monospace;
  font-size:11px;line-height:1.65;height:250px;overflow-y:auto;
  white-space:pre-wrap;word-break:break-all;margin-top:14px;
}
.log::-webkit-scrollbar{width:8px}
.log::-webkit-scrollbar-thumb{background:rgba(255,255,255,.2);
  border-radius:8px}

/* ---------- 自绘确认对话框 ---------- */
.modal-mask{
  position:fixed;inset:0;background:rgba(0,0,0,.30);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  z-index:998;display:none;align-items:center;justify-content:center;
  animation:fadeIn .22s ease;
}
.modal-mask.show{display:flex}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal{
  width:400px;max-width:calc(100vw - 48px);
  background:rgba(255,255,255,.95);
  backdrop-filter:blur(30px) saturate(180%);
  -webkit-backdrop-filter:blur(30px) saturate(180%);
  border-radius:20px;box-shadow:var(--shadow-lg);
  padding:24px 26px 20px;
  animation:pop .3s cubic-bezier(.22,1,.36,1);
}
@keyframes pop{from{opacity:0;transform:scale(.94) translateY(8px)}
  to{opacity:1;transform:none}}
.modal-title{font-size:16px;font-weight:700;letter-spacing:.2px}
.modal-body{font-size:13px;color:var(--muted);line-height:1.75;margin-top:12px}
.modal-body b{color:var(--danger);font-weight:700}
.modal-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px}
.modal-actions .btn{min-width:88px}

/* ---------- 气泡通知 ---------- */
.toast{
  position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(80px);
  background:rgba(28,28,30,.94);
  backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);
  color:#fff;padding:12px 20px;border-radius:14px;font-size:12.5px;
  box-shadow:var(--shadow-lg);opacity:0;pointer-events:none;
  transition:all .38s cubic-bezier(.22,1,.36,1);z-index:999;
  max-width:460px;line-height:1.5;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* ---------- 加载遮罩 ---------- */
.spin{
  width:13px;height:13px;border:2px solid rgba(255,255,255,.3);
  border-top-color:#fff;border-radius:50%;display:inline-block;
  animation:spin .7s linear infinite;vertical-align:-2px;margin-right:6px;
}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<div class="titlebar drag-region" id="titlebar">
  <div class="tb-brand">
    <span class="tb-dot"></span>
    <span class="tb-title">CampusNetAuth</span>
    <span class="tb-sub">校园网无感认证</span>
  </div>
  <div class="tb-btns">
    <button class="tb-btn tb-min" id="btnMin" title="最小化"></button>
    <button class="tb-btn tb-max" id="btnMax" title="最大化"></button>
    <button class="tb-btn tb-close" id="btnClose" title="关闭"></button>
  </div>
</div>

<div class="app">
  <aside class="sidebar">
    <div class="side-head">
      <div class="side-logo">CampusNet</div>
      <div class="side-logo-sub">校园网无感认证</div>
    </div>
    <nav class="nav">
      <button class="nav-item active" data-page="conn">
        <svg class="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/>
          <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>
        </svg>连接与账号
      </button>
      <button class="nav-item" data-page="auto">
        <svg class="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
        </svg>自动化
      </button>
      <button class="nav-item" data-page="status">
        <svg class="nav-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>
        </svg>状态与日志
      </button>
    </nav>
    <div class="side-foot">密码使用 Windows DPAPI 加密<br>仅当前 Windows 用户可解密</div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div class="topbar-sub" id="subtitle">正在读取状态…</div>
      <div class="state">
        <span class="dot" id="dot"></span>
        <span id="stateText">—</span>
      </div>
    </div>

    <div class="content">
      <div class="deploy-banner" id="deployBanner">
        <span>检测到当前目录<b>不是空文件夹</b>：建议将 exe 单独放入一个空文件夹运行，程序会在同目录生成配置与组件文件（本提示仅在全新部署时出现）。</span>
        <button class="btn btn-ghost" id="deployBannerClose" style="flex-shrink:0">知道了</button>
      </div>
      <!-- 连接与账号 -->
      <section class="page active" id="page-conn">
        <div class="page-title">校园网自动认证</div>
        <div class="card">
          <div class="card-title">认证信息</div>
          <div class="card-sub">修改后点击「保存并连接」立即生效并尝试认证</div>
          <div class="field">
            <div class="label">门户地址</div>
            <input class="input" id="inHost" placeholder="172.16.54.18:80">
          </div>
          <div class="field">
            <div class="label">运营商</div>
            <div class="seg" id="segService">
              <button data-v="default">校园网</button>
              <button data-v="DX">电信</button>
              <button data-v="YD">移动</button>
              <button data-v="LT">联通</button>
              <button data-v="auto">自动</button>
            </div>
          </div>
          <div class="field">
            <div class="label">账号（学号）</div>
            <input class="input" id="inUser" placeholder="请输入学号">
          </div>
          <div class="field">
            <div class="label">密码</div>
            <input class="input" id="inPass" type="password"
                   placeholder="留空表示不修改（Windows DPAPI 加密保存）">
          </div>
          <div class="btn-row" style="margin-top:20px">
            <button class="btn btn-primary" id="btnConnect">保存并连接</button>
            <button class="btn btn-danger" id="btnLogout">下线</button>
          </div>
        </div>
      </section>

      <!-- 自动化 -->
      <section class="page" id="page-auto">
        <div class="card">
          <div class="card-title">工具箱</div>
          <div class="card-sub">一键自检 · 体检 · 自启管理 · 抓取诊断（原 .bat 已全部内置）</div>
          <div class="btn-row" style="margin-top:16px">
            <button class="btn btn-primary" id="btnSelfTest">自检</button>
            <button class="btn btn-primary" id="btnHealth">一键体检</button>
            <button class="btn btn-ghost" id="btnOpenDir">打开目录</button>
          </div>
          <div class="btn-row" style="margin-top:10px">
            <button class="btn btn-ghost" id="btnDiagnose">抓取诊断</button>
            <span class="hint">登录 Windows 即自动守护，全程无窗口</span>
          </div>
        </div>

        <div class="card">
          <div class="card-title">后台守护</div>
          <div class="card-sub">仅当处于校园网且检测到未认证时才自动登录</div>
          <div class="daemon-line" style="margin-top:14px">
            <span id="daemonState">○ 已停止</span>
          </div>
          <div class="hint" id="daemonCompHint" style="margin-top:6px"></div>
          <div class="warn-bar" id="daemonWarn" style="display:none;margin-top:10px">
            <span>开机自启已启用，但守护未在运行</span>
            <button class="btn btn-primary" id="btnDStart2" style="flex-shrink:0;margin-left:auto">立即启动</button>
          </div>
          <div class="warn-bar err" id="loginErrWarn" style="display:none;margin-top:10px"></div>
          <div class="warn-bar err" id="autostartWarn" style="display:none;margin-top:10px"></div>
          <div class="evt-bar" id="evtBar" style="display:none;margin-top:10px"></div>
          <div class="btn-row" style="margin-top:14px">
            <button class="btn btn-primary" id="btnDStart">启动守护</button>
            <button class="btn btn-ghost" id="btnDStop">停止守护</button>
            <button class="btn btn-ghost" id="btnDRestart">重启守护</button>
          </div>
        </div>

        <div class="card">
          <div class="card-title">自动认证</div>
          <div class="row" style="margin-top:16px;justify-content:space-between">
            <div>
              <div style="font-size:13px;font-weight:600">开机后自动认证</div>
              <div class="card-sub" style="margin-top:4px">登录后自动在后台启动守护，无需任何操作</div>
            </div>
            <div class="switch" id="swAuto"></div>
          </div>
        </div>

        <div class="card">
          <div class="card-title">组件管理</div>
          <div class="card-sub">单文件便携模式：组件按需安装，卸载即移除对应文件</div>
          <div class="comp-row">
            <div class="comp-info">
              <div class="comp-name">开机自启组件</div>
              <div class="comp-sub">释放 Daemon.vbs · 写入注册表自启 · 开关见上方</div>
            </div>
            <span class="comp-state" id="compAutoState">—</span>
            <button class="btn btn-ghost" id="btnCompAutoIns" style="flex-shrink:0">安装</button>
            <button class="btn btn-ghost" id="btnCompAutoUnins" style="flex-shrink:0">卸载</button>
          </div>
          <div class="comp-row">
            <div class="comp-info">
              <div class="comp-name">值守组件</div>
              <div class="comp-sub">释放守护启动器 vbs · 后台守护需先安装</div>
            </div>
            <span class="comp-state" id="compDaemonState">—</span>
            <button class="btn btn-ghost" id="btnCompDaemonIns" style="flex-shrink:0">安装</button>
            <button class="btn btn-ghost" id="btnCompDaemonUnins" style="flex-shrink:0">卸载</button>
          </div>
          <div class="comp-row">
            <div class="comp-info">
              <div class="comp-name">界面启动器组件</div>
              <div class="comp-sub">释放 UI.vbs · 静默拉起管理界面</div>
            </div>
            <span class="comp-state" id="compUiState">—</span>
            <button class="btn btn-ghost" id="btnCompUiIns" style="flex-shrink:0">安装</button>
            <button class="btn btn-ghost" id="btnCompUiUnins" style="flex-shrink:0">卸载</button>
          </div>
          <div class="hint" style="margin-top:12px">config.json / cred.bin 等配置数据首次运行自动生成，不属于组件</div>
        </div>

        <div class="card">
          <div class="card-title">检测参数</div>
          <div class="card-sub">留空则保持原值</div>
          <div class="field"><div class="label">在线检测周期（秒）</div>
            <input class="input" id="inInterval" style="max-width:160px"></div>
          <div class="field"><div class="label">离线检测周期（秒）</div>
            <input class="input" id="inOffline" style="max-width:160px"></div>
          <div class="field"><div class="label">失败重试间隔（秒）</div>
            <input class="input" id="inRetry" style="max-width:160px"></div>
          <div class="field"><div class="label">最长退避间隔（秒）</div>
            <input class="input" id="inMax" style="max-width:160px"></div>
          <div class="field"><div class="label">单次请求超时（秒）</div>
            <input class="input" id="inTimeout" style="max-width:160px"></div>
          <div class="btn-row" style="margin-top:18px">
            <button class="btn btn-primary" id="btnSaveAuto">保存设置</button>
          </div>
        </div>
      </section>

      <!-- 状态与日志 -->
      <section class="page" id="page-status">
        <div class="card">
          <div class="card-title">当前状态</div>
          <div class="stat-grid" style="margin-top:16px">
            <div><div class="stat-label">网络状态</div>
              <div class="stat-value" id="stNet">—</div></div>
            <div><div class="stat-label">账号</div>
              <div class="stat-value" id="stUser">—</div></div>
            <div><div class="stat-label">服务</div>
              <div class="stat-value" id="stSvc">—</div></div>
            <div><div class="stat-label">IP 地址</div>
              <div class="stat-value" id="stIp">—</div></div>
            <div><div class="stat-label">开机自启</div>
              <div class="stat-value" id="stAuto">—</div></div>
            <div><div class="stat-label">后台守护</div>
              <div class="stat-value" id="stDaemon">—</div></div>
          </div>
          <div class="btn-row" style="margin-top:20px">
            <button class="btn btn-ghost" id="btnRefresh">刷新</button>
            <button class="btn btn-primary" id="btnCheck">立即检测</button>
            <button class="btn btn-danger" id="btnLogout2">下线</button>
          </div>
          <div class="hint" style="margin-top:10px">立即检测 = 检查当前在线状态，若未认证将自动登录</div>
        </div>
        <div class="card">
          <div class="card-title">运行日志</div>
          <div class="log" id="logBox">（载入中…）</div>
        </div>
      </section>
    </div>
  </main>
</div>

<div class="toast" id="toast"></div>

<div class="modal-mask" id="modalMask">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-title" id="modalTitle">确认操作</div>
    <div class="modal-body" id="modalBody"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" id="modalCancel">取消</button>
      <button class="btn btn-primary" id="modalOk">确认</button>
    </div>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const api = (p, opt) => fetch(p, opt).then(r => r.json());
const post = (p, body) => api(p, {method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify(body || {})});
// unix 秒时间戳 -> "HH:MM"（用于登录失败提示）
const fmtClock = (ts) => {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes());
};

let toastTimer = null;
function toast(msg, ms){
  const el = $('#toast');
  el.innerHTML = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms || 3000);
}

/* ---------- 自绘确认对话框（替代原生 confirm）---------- */
function confirmDialog({title, html, okText, onOk}){
  const mask = $('#modalMask');
  $('#modalTitle').textContent = title || '确认操作';
  $('#modalBody').innerHTML = html || '';
  $('#modalOk').textContent = okText || '确认';
  mask.classList.add('show');
  const close = () => mask.classList.remove('show');
  $('#modalCancel').onclick = close;
  $('#modalOk').onclick = () => { close(); onOk && onOk(); };
  mask.onclick = (e) => { if (e.target === mask) close(); };
  $('#modalCancel').focus();
}

/* ---------- 窗口控制：优先 pywebview js_api，回退 HTTP ---------- */
async function win(action){
  try{
    if (window.pywebview && window.pywebview.api){
      const a = window.pywebview.api;
      if (action === 'minimize' && a.win_minimize) return a.win_minimize();
      if (action === 'maximize' && a.win_maximize_toggle) return a.win_maximize_toggle();
      if (action === 'close' && a.win_close) return a.win_close();
    }
  }catch(e){}
  return post('/api/window', {action});
}
$('#btnMin').onclick = () => win('minimize');
$('#btnMax').onclick = () => win('maximize');
$('#btnClose').onclick = () => win('close');

/* ---------- 标题栏拖动（pywebview drag-region 官方通道）---------- */
// DIRECT_TARGET_ONLY=True：只有 target 本身是 .drag-region 才触发拖动 →
// 把品牌区及其子元素（logo 点 / 标题 / 副标题）全部补上 .drag-region，
// 整条标题栏（除交通灯按钮区）都可拖；按钮无此 class 且已 stopPropagation。
document.querySelectorAll('#titlebar .tb-brand, #titlebar .tb-brand *')
  .forEach(el => el.classList.add('drag-region'));
// 交通灯按钮阻止 mousedown 冒泡：避免被 .titlebar.drag-region 的拖动劫持成"拖动"
document.querySelectorAll('.tb-btn').forEach(b => {
  b.addEventListener('mousedown', e => e.stopPropagation());
});
// 双击标题栏（非按钮区）切换最大化
$('#titlebar').addEventListener('dblclick', (e) => {
  if (e.target.closest('.tb-btns')) return;
  win('maximize');
});

/* ---------- 页面切换 ---------- */
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('#page-' + btn.dataset.page).classList.add('active');
    $('.content').scrollTop = 0;
  };
});

/* ---------- 分段控件 ---------- */
let curService = 'default';
$('#segService').querySelectorAll('button').forEach(b => {
  b.onclick = () => {
    curService = b.dataset.v;
    $('#segService').querySelectorAll('button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
  };
});
function setService(v){
  curService = v || 'default';
  $('#segService').querySelectorAll('button').forEach(x =>
    x.classList.toggle('active', x.dataset.v === curService));
}

/* ---------- 开关 ---------- */
let swAutoVal = false;
let compAuto = 'missing', compDaemon = 'missing', compUi = 'missing';
let deployBannerDone = false;
$('#swAuto').onclick = async () => {
  const want = !swAutoVal;
  if (want && compAuto === 'missing'){ toast('请先安装「开机自启组件」（下方组件管理）', 3600); return; }
  const ok = want ? await post('/api/autostart/install')
                  : await post('/api/autostart/uninstall');
  if (ok && ok.ok){ toast(want ? '已开启开机自启' : '已关闭开机自启'); load(); }
  else toast('操作失败', 3200);
};

/* ---------- 状态刷新 ---------- */
async function load(){
  let st;
  try { st = await api('/api/status'); }
  catch(e){ $('#stateText').textContent = '服务未响应'; return; }

  const online = st.online;
  const dot = $('#dot');
  dot.className = 'dot live';
  const netTxt = $('#stateText');
  netTxt.className = '';
  if (online === true){ netTxt.textContent = '已连接'; dot.classList.add('green'); netTxt.classList.add('ok'); }
  else if (online === null){ netTxt.textContent = '网络不可达'; dot.classList.add('orange'); netTxt.classList.add('warn'); }
  else { netTxt.textContent = '未认证'; dot.classList.add('red'); netTxt.classList.add('err'); }

  $('#subtitle').textContent = '门户 ' + st.host + ' · 检测周期 ' + st.interval + ' 秒';
  const netMap = {true:['已认证','ok'], false:['未认证','err'], null:['不可达','warn']};
  const nm = netMap[online] || ['—',''];
  const stNetEl = $('#stNet');
  stNetEl.textContent = nm[0];
  stNetEl.className = 'stat-value ' + nm[1];
  $('#stUser').textContent = st.username ? st.username : '—';
  $('#stSvc').textContent = (st.info && st.info.service) || st.service || '—';
  $('#stIp').textContent = (st.info && st.info.userIp) || '—';
  $('#stAuto').textContent = st.autostart ? '已启用' : '未启用';
  const stD = $('#stDaemon');
  stD.textContent = st.daemon ? '运行中' : '已停止';
  stD.className = 'stat-value ' + (st.daemon ? 'ok' : '');
  const ds = $('#daemonState');
  ds.textContent = st.daemon
    ? ('● 运行中 (PID ' + st.daemon_pid + ')') : '○ 已停止';
  ds.className = st.daemon ? 'ds run' : 'ds stop';

  swAutoVal = !!st.autostart;
  $('#swAuto').classList.toggle('on', swAutoVal);

  // 组件状态
  const comps = st.components || {};
  compAuto = comps.autostart || 'missing';
  compDaemon = comps.daemon || 'missing';
  compUi = comps.ui || 'missing';
  const autoMap = {missing:['未安装',''], installed:['已安装',''], enabled:['已启用','ok']};
  const aa = autoMap[compAuto] || ['—',''];
  const cAuto = $('#compAutoState');
  cAuto.textContent = aa[0]; cAuto.className = 'comp-state ' + aa[1];
  const cD = $('#compDaemonState');
  cD.textContent = compDaemon === 'installed' ? '已安装' : '未安装';
  cD.className = 'comp-state ' + (compDaemon === 'installed' ? 'ok' : '');
  const cU = $('#compUiState');
  cU.textContent = compUi === 'installed' ? '已安装' : '未安装';
  cU.className = 'comp-state ' + (compUi === 'installed' ? 'ok' : '');
  $('#btnCompAutoIns').disabled = compAuto !== 'missing';
  $('#btnCompAutoUnins').disabled = compAuto === 'missing';
  $('#btnCompDaemonIns').disabled = compDaemon !== 'missing';
  $('#btnCompDaemonUnins').disabled = compDaemon === 'missing';
  $('#btnCompUiIns').disabled = compUi !== 'missing';
  $('#btnCompUiUnins').disabled = compUi === 'missing';

  // 守护按钮按上下文启用（未装值守组件时禁用并提示）
  const daemonOk = compDaemon === 'installed';
  $('#btnDStart').disabled = !!st.daemon || !daemonOk;
  $('#btnDStop').disabled = !st.daemon;
  $('#btnDRestart').disabled = !st.daemon;
  $('#daemonCompHint').textContent = daemonOk
    ? '值守组件已安装 · 守护以同一 exe 后台进程运行（不复制副本）'
    : '值守组件未安装：请先在上方「组件管理」安装，方可启动守护';

  // 自启已启用但守护未运行 → 警示 + 一键启动（守护异常自愈提示）
  $('#daemonWarn').style.display =
    (!!st.autostart && !st.daemon) ? 'flex' : 'none';
  // 自启链路健康（Run 键→启动器→exe 文件完整性）：异常时红色警示，
  // 直接告知"开机自启将失败"的根因，避免下次开机弹 80070002
  const ah = st.autostart_health || [];
  const awEl = $('#autostartWarn');
  if (st.autostart && ah.length >= 2 && !ah[0] && ah[1]) {
    awEl.textContent = '⚠ ' + ah[1];
    awEl.style.display = 'flex';
  } else {
    awEl.style.display = 'none';
  }
  // 守护最近一次致命登录失败（last_error，成功后自动清除）
  const leEl = $('#loginErrWarn');
  if (st.last_error && st.last_error.msg) {
    leEl.textContent = '⚠ 守护登录失败：' + st.last_error.msg
      + (st.last_error.t ? '（' + fmtClock(st.last_error.t) + '）' : '');
    leEl.style.display = 'flex';
  } else {
    leEl.style.display = 'none';
  }
  // 最近状态事件（掉线/重连/恢复/登录结果）即时展示，最多 3 条
  const evtEl = $('#evtBar');
  const evts = st.events || [];
  if (evts.length){
    const EVT_CLS = {lost:'err', restored:'ok', offline:'warn',
                     login_ok:'ok', login_fail:'err', kickout:'warn'};
    const items = evts.slice(-3).reverse().map(ev => {
      const d = new Date(ev.t * 1000);
      const ts = String(d.getHours()).padStart(2,'0') + ':' +
                 String(d.getMinutes()).padStart(2,'0');
      const msg = String(ev.msg || '').replace(/[<>&"']/g,
        c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]));
      return '<span class="evt-item ' + (EVT_CLS[ev.kind] || '') + '">'
        + '<b>' + (EVT_CLS[ev.kind] === 'err' ? '✕' : '✓') + '</b>'
        + msg + '<i>' + ts + '</i></span>';
    }).join('');
    evtEl.innerHTML = '<span class="evt-cap">最近动态</span>' + items;
    evtEl.style.display = 'flex';
  } else {
    evtEl.style.display = 'none';
  }

  // 空文件夹部署提示（仅全新部署 + 目录有无关文件 + 未手动关闭）
  const showBanner = !!st.first_deploy && (st.foreign_files || []).length > 0
                     && !deployBannerDone;
  $('#deployBanner').style.display = showBanner ? 'flex' : 'none';

  // 表单回填
  $('#inHost').value = st.host || '';
  $('#inUser').value = st.username || '';
  setService(st.service);
  const c = st.cfg || {};
  $('#inInterval').value = c.interval || '';
  $('#inOffline').value = c.offline_interval || '';
  $('#inRetry').value = c.retry_interval || '';
  $('#inMax').value = c.max_interval || '';
  $('#inTimeout').value = c.timeout || '';

  // 日志
  try{
    const lg = await api('/api/log?n=200');
    const box = $('#logBox');
    box.textContent = lg.text || '（暂无日志）';
    box.scrollTop = box.scrollHeight;
  }catch(e){}
}

function busy(on, btn, text){
  if (!btn) return;
  if (on){ btn.dataset._t = btn.innerHTML; btn.innerHTML = '<span class="spin"></span>' + text; btn.disabled = true; }
  else { btn.innerHTML = btn.dataset._t || btn.innerHTML; btn.disabled = false; }
}

async function withTask(url, btn, busyText, doneTitle){
  busy(true, btn, busyText);
  try{
    const r = await post(url);
    if (!r.id){ toast('启动失败'); busy(false, btn); return; }
    const id = r.id;
    for (let i = 0; i < 300; i++){
      await new Promise(res => setTimeout(res, 1000));
      const t = await api('/api/task?id=' + id);
      if (t.status !== 'running'){
        busy(false, btn);
        const ok = t.status === 'done';
        toast(doneTitle + (ok ? '完成' : '失败') + '，结果已输出到日志', 4200);
        load();
        return;
      }
    }
    busy(false, btn);
    toast('任务超时');
  }catch(e){ busy(false, btn); toast('出错：' + e); }
}

/* ---------- 事件绑定 ---------- */
$('#btnConnect').onclick = async () => {
  const btn = $('#btnConnect');
  busy(true, btn, '正在连接…');
  const r = await post('/api/connect', {
    host: $('#inHost').value,
    username: $('#inUser').value,
    password: $('#inPass').value,
    service: curService
  });
  busy(false, btn);
  if (r.ok) toast(r.result === 'already_online' ? '当前已在线，无需重复认证' : '认证成功 ✓');
  else toast('认证失败：' + (r.error || '未知错误') + (r.hint ? '（' + r.hint + '）' : ''), 4200);
  $('#inPass').value = '';
  load();
};

$('#btnLogout').onclick = async () => { await post('/api/logout'); toast('已下线'); load(); };
$('#btnLogout2').onclick = async () => { await post('/api/logout'); toast('已下线'); load(); };
$('#btnRefresh').onclick = () => load();
$('#btnCheck').onclick = async () => {
  const btn = $('#btnCheck');
  busy(true, btn, '检测中…');
  const r = await post('/api/connect', {});
  busy(false, btn);
  if (r.ok) toast(r.result === 'already_online' ? '已在线，无需重新认证 ✓' : '认证成功，已重新连接 ✓');
  else toast('检测失败：' + (r.error || '未知错误') + (r.hint ? '（' + r.hint + '）' : ''), 4200);
  load();
};
$('#btnDStart').onclick = async () => { await post('/api/daemon/start'); toast('守护已启动'); load(); };
$('#btnDStart2').onclick = async () => { await post('/api/daemon/start'); toast('守护已启动'); load(); };
$('#btnDStop').onclick = async () => { await post('/api/daemon/stop'); toast('守护已停止'); load(); };
$('#btnDRestart').onclick = async () => { await post('/api/daemon/restart'); toast('守护已重启'); load(); };
$('#btnOpenDir').onclick = () => post('/api/open-dir');
$('#btnDiagnose').onclick = async () => {
  await post('/api/diagnose');
  toast('抓取诊断已在后台启动，报告将写入 probe/capture_result.json', 4600);
};
$('#btnSelfTest').onclick = () => withTask('/api/self-test', $('#btnSelfTest'), '自检中…', '自检');
$('#btnHealth').onclick = () => {
  if (compDaemon !== 'installed'){
    toast('体检需要后台守护配合，请先安装「值守组件」', 3600);
    return;
  }
  confirmDialog({
  title: '一键体检',
  html: '体检将<b>立即下线当前网络</b>，然后由后台守护自动重连，验证「断线自动恢复」链路。<br><br>期间可能断网约 <b>75 秒</b>，请勿在下载 / 直播 / 游戏等场景下进行。确定继续？',
  okText: '确认体检',
  onOk: () => withTask('/api/health', $('#btnHealth'), '体检中…', '一键体检')
  });
};

/* ---------- 组件管理 ---------- */
async function compOp(action, name, btn){
  busy(true, btn, '处理中…');
  try{
    const r = await post('/api/components/' + action, {name});
    toast((r && r.message) ? r.message : (r && r.ok ? '操作成功' : '操作失败'), 3600);
  }catch(e){ toast('出错：' + e, 3600); }
  busy(false, btn);
  load();
}
$('#btnCompAutoIns').onclick   = () => compOp('install', 'autostart', $('#btnCompAutoIns'));
$('#btnCompAutoUnins').onclick = () => confirmDialog({
  title: '卸载开机自启组件',
  html: '将移除 <b>CampusNetAuthDaemon.vbs</b> 并删除注册表自启项。<br>再次启用时重新点「安装」即可。',
  okText: '确认卸载',
  onOk: () => compOp('uninstall', 'autostart', $('#btnCompAutoUnins'))
});
$('#btnCompDaemonIns').onclick   = () => compOp('install', 'daemon', $('#btnCompDaemonIns'));
$('#btnCompDaemonUnins').onclick = () => confirmDialog({
  title: '卸载值守组件',
  html: '将移除 <b>CampusNetAuthDaemon.vbs</b> 守护启动器（不再复制 exe 副本）。<br>若守护正在运行，需先「停止守护」；若开机自启仍启用，需先卸载「开机自启组件」。',
  okText: '确认卸载',
  onOk: () => compOp('uninstall', 'daemon', $('#btnCompDaemonUnins'))
});
$('#btnCompUiIns').onclick   = () => compOp('install', 'ui', $('#btnCompUiIns'));
$('#btnCompUiUnins').onclick = () => confirmDialog({
  title: '卸载界面启动器组件',
  html: '将移除 <b>CampusNetAuthUI.vbs</b> 静默启动器。',
  okText: '确认卸载',
  onOk: () => compOp('uninstall', 'ui', $('#btnCompUiUnins'))
});
$('#deployBannerClose').onclick = () => { deployBannerDone = true; $('#deployBanner').style.display = 'none'; };
$('#btnSaveAuto').onclick = async () => {
  await post('/api/config', {
    interval: $('#inInterval').value,
    offline_interval: $('#inOffline').value,
    retry_interval: $('#inRetry').value,
    max_interval: $('#inMax').value,
    timeout: $('#inTimeout').value
  });
  toast('设置已保存');
  load();
};

load();
setInterval(load, 20000);
</script>
</body>
</html>
"""
