#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网无感认证守护程序 —— 锐捷 ePortal / SAM+ 专用
================================================================
功能：开机自启、后台静默运行、断线自动重连校园网。

协议要点（已针对本机门户 172.16.54.18 逆向确认）：
  * 登录接口 : POST /eportal/InterFace.do?method=login
  * 密码明文 : <密码> + ">" + <queryString 中的 mac>
  * 加密     : 明文整体反转 -> 教科书 RSA（无 PKCS#1 填充，16-bit 小端打包）
  * 公钥     : 登录页隐藏字段 publicKeyExponent / publicKeyModulus（每次动态取）
  * 参数编码 : 每个字段都要 encodeURIComponent 两次

仅使用 Python 标准库，无任何第三方依赖。
"""

import os
import re
import sys
import json
import time
import ctypes
import atexit
import getpass
import socket
import logging
import argparse
import shutil
import subprocess
import http.client
import urllib.parse
import logging.handlers
import concurrent.futures as _cf
from ctypes import wintypes

# ============================ 常量 ============================

APP_NAME = "CampusNetAuth"
TASK_NAME = "CampusNetAuth"
# 守护的控制通道端口：守护 start 时 bind+listen(1) 持有该端口（既作单例锁，
# 又作指令通道），UI/CLI 连上后发 "STOP"（优雅退出）或 "CHECK"（立即复核一轮）；
# daemon_running() 也以"该端口可连 + pid 文件"判定守护存活。
SINGLETON_PORT = 47667

def _base_dir():
    """数据目录：
    * 打包态（PyInstaller exe）—— exe 所在目录（sys.executable 指向真实
      exe；__file__ 指向临时解包目录，绝不能用作数据目录）
    * 开发态（python *.py）   —— 脚本所在目录

    所有配置 / 凭据 / 日志 / 状态文件都落在该目录下，
    移动整个文件夹即整体迁移，无需重装。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CRED_PATH = os.path.join(BASE_DIR, "cred.bin")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "campusnet.log")
PID_PATH = os.path.join(BASE_DIR, "daemon.pid")


def _cleanup_stale_mei(max_age: float = 3600.0) -> int:
    """清理上次异常退出残留的 PyInstaller onefile 解包目录(%TEMP%\\_MEIxxxx)。

    背景：onefile exe 退出时 bootloader 会递归删除自己的 _MEI 目录，若进程
    被强杀/断电/崩溃则会在 %TEMP% 留下垃圾，积累后既占磁盘又干扰排查。
    这里在每次启动时清扫超过 max_age(默认 1 小时)的旧残留：
      * 跳过当前进程的 sys._MEIPASS（它必然是刚解包的，也按年龄天然跳过）；
      * 用「先改名探测、成功再改回」确认目录没被其它进程占用——目录内
        有文件被占用时改名必失败 → 跳过，避免误删运行中实例的解包目录；
      * 只删最旧、确定不再被使用的目录，绝不碰当前目录。
    返回删除个数（供日志/调试）。任何异常静默吞掉，不影响主流程。
    """
    if not (os.name == "nt" and getattr(sys, "frozen", False)):
        return 0
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if not tmp or not os.path.isdir(tmp):
        return 0
    cur = os.path.normcase(os.path.abspath(getattr(sys, "_MEIPASS", "")))
    now = time.time()
    removed = 0
    try:
        names = os.listdir(tmp)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("_MEI"):
            continue
        p = os.path.normcase(os.path.abspath(os.path.join(tmp, name)))
        if p == cur:
            continue
        try:
            if not os.path.isdir(p):
                continue
            if now - os.path.getmtime(p) < max_age:
                continue
        except OSError:
            continue
        probe = p + "._gcprobe"
        try:
            os.rename(p, probe)   # 目录被占用(进程正在用)→ 改名失败 → 跳过
            os.rename(probe, p)   # 探测成功 → 改回原名再删
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
        except OSError:
            try:  # 探测名残留兜底改回
                if os.path.exists(probe):
                    os.rename(probe, p)
            except OSError:
                pass
    return removed


# 模块导入即清扫(仅打包态生效;开发态返回 0)。放在常量区之后、日志初始化
# 之前执行,静默完成,不依赖日志系统。
_MEI_CLEANED = _cleanup_stale_mei()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "portal_host": "172.16.54.18",
    "portal_port": 80,
    "portal_base": "/eportal",
    "username": "",
    # 服务名：default=校园网, DX=电信, YD=移动, LT=联通
    # 设为 "auto" 则自动选取门户返回的默认服务
    "service": "default",
    # 检测周期（秒）：在线时的轮询间隔
    "interval": 30,
    # 认证失败后的重试间隔（秒），会指数退避到 max_interval
    "retry_interval": 30,
    "max_interval": 600,
    # 网络不可达（不在校园网）时的轮询间隔
    "offline_interval": 60,
    "timeout": 8,
    # 连通性/门户探测超时（秒）：比正式接口更短，网络差时快速判定、避免轮询卡死
    "probe_timeout": 3,
    # 在线时主动续约会话的间隔（秒）：调用 ePortal keepalive 防认证过期被踢
    "keepalive_interval": 300,
    # 登录成功后即时确认的等待（秒）：短等后立刻复核一次在线状态，
    # 防网关"假成功"（result=success 但会话没建起来）拖到下一轮才发现
    "verify_interval": 5,
    # 校外/断网退避等待期间的环境快探间隔（秒）：恢复时即时响应（不等完整退避）
    "recheck_interval": 30,
    # 连通性检测目标（HTTP，避免 HTTPS 证书干扰）
    "detect_targets": [
        {"host": "www.msftconnecttest.com", "path": "/connecttest.txt",
         "expect": "Microsoft Connect Test"},
        {"host": "captive.apple.com", "path": "/hotspot-detect.html",
         "expect": "Success"},
        {"host": "www.baidu.com", "path": "/", "expect": ""},
    ],
    "log_level": "INFO",
}

# GBK 门户常见的登录结果提示（用于把错误码翻译成人话）
ERR_HINT = {
    "ERR_USER_FIRSTLOGIN_NEED_CHANGE_PASSWORD": "首次登录必须修改密码，请先在网页端改密",
    "ERR_AUTH_USER_OR_PASS_ERROR": "账号或密码错误",
    "ERR_USER_ARREARAGE": "账号欠费",
    "ERR_USER_BIND_ERROR": "账号已被绑定其它设备/IP",
    "ERR_USER_ONLINE_LIMIT": "在线设备数超过限制",
    "ERR_USER_FORBIDDEN": "账号被禁用",
    "ERR_USER_EXPIRED": "账号已过期",
    "ERR_RADIUS_REJECT": "认证服务器拒绝（RADIUS reject）",
    "ERR_USER_NOT_EXIST": "账号不存在",
    "ERR_PASSWORD_ERROR": "密码错误",
    "wait": "网关正在处理，稍后重试即可",
}


# ============================ 日志 ============================

def setup_logger(to_console=True, level="INFO"):
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    try:
        # 日志轮转：单文件 1MB、保留 3 份备份，防止长期运行无限增长
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    if to_console and sys.stdout is not None:
        try:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
        except Exception:
            pass
    return logger


log = logging.getLogger(APP_NAME)


# ============================ 工具 ============================

def dq(s):
    """双重 URL 编码，等价于 JS 的 encodeURIComponent(encodeURIComponent(x))"""
    return urllib.parse.quote(urllib.parse.quote(str(s), safe=""), safe="")


def mask_user(u):
    if not u or len(u) <= 4:
        return u or ""
    return u[:2] + "*" * (len(u) - 4) + u[-2:]


def safe_message(res, default="未知错误"):
    """从门户响应里安全取出可读错误信息，避免把 null 打印成 'None'。"""
    if isinstance(res, dict):
        msg = res.get("message")
        if msg:
            return str(msg)
        raw = res.get("_raw")
        if raw:
            return str(raw)
    return default


def force_utf8_console():
    """
    把 Windows 控制台切到 UTF-8(65001)，避免中文输出乱码。

    为什么不在 .bat 里用 chcp：cmd.exe 切换代码页后，会按"字符偏移"重新
    定位批处理文件指针，而文件实际是"字节偏移"，含中文的后续行会被错位
    读取甚至整行跳过（实测输出为空，或把 chcp 读成 ho）。所以改由 Python
    在运行时直接设置控制台代码页，最稳。
    """
    if os.name != "nt":
        return
    try:
        if not ctypes.windll.kernel32.GetConsoleWindow():
            # 无控制台（--windowed 打包）：若仍调 SetConsoleOutputCP 会把进程
            # 默认输出代码页污染成 65001，导致 attach_console() 之后
            # GetConsoleOutputCP() 拿不到父控制台的真实代码页（编码错配乱码）。
            return
        k32 = ctypes.windll.kernel32
        if k32.GetConsoleOutputCP() != 65001:
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
    except Exception:
        pass


def hide_console():
    """隐藏本进程独占的控制台窗口（消除双击 exe / 后台拉起时的黑窗闪屏）。

    关键判断：GetConsoleProcessList 返回挂在该控制台上的进程数。
    * 计数 == 1 -> 控制台只属于本进程（双击 exe、wscript 静默启动等）-> 可安全隐藏；
    * 计数 > 1  -> 控制台被父进程（cmd/bat）共享，本进程只是"前台打印者"，
                  此时绝不能隐藏，否则 CLI 命令的输出会跟着父窗口一起消失。
    开发态（未打包）与 daemon 用 CREATE_NO_WINDOW 启动（本就没有控制台）时直接跳过。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return
        pids = (ctypes.c_ulong * 4)()
        if k32.GetConsoleProcessList(pids, 4) <= 1:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


class _NullStream:
    """--windowed 打包且无父控制台时，兜底 stdout/stderr，避免 print 抛错。"""

    def write(self, *a, **k):
        return 0

    def flush(self, *a, **k):
        pass

    def writelines(self, lines):
        pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: (0 if name == "write" else None)


def attach_console():
    """GUI 子系统(--windowed)打包下，让 CLI 命令附加到父进程(cmd)控制台。

    双击 exe（UI / daemon）-> Windows 根本不创建控制台，天然零黑窗；
    从 cmd 运行 `CampusNetAuth.exe status` -> AttachConsole(ATTACH_PARENT_PROCESS)
    附加到父 cmd 控制台，stdout/stderr/stdin 重定向到 CONOUT$/CONIN$，输出正常显示。
    --console 打包（已有控制台）时本函数为 no-op。
    附加失败（例如 explorer 双击 CLI）时把 stdout/stderr 兜底为空流。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    try:
        k32 = ctypes.windll.kernel32
        if k32.GetConsoleWindow():  # 已有控制台（--console 打包），无需附加
            return True
    except Exception:
        return False
    attached = False
    try:
        # ATTACH_PARENT_PROCESS = -1 (0xFFFFFFFF)
        attached = bool(k32.AttachConsole(0xFFFFFFFF))
    except Exception:
        attached = False
    if attached:
        try:
            # 尊重父控制台当前代码页，用匹配编码读写 CONOUT$/CONIN$：
            # cmd 默认 936 -> GBK；bat 里 chcp 65001 -> UTF-8；ConPTY 按其报告值。
            # 这样无论父控制台是什么代码页，中文都不乱码（不再强制切代码页）。
            cp = k32.GetConsoleOutputCP()
            enc = ("utf-8" if cp in (65001, 0)
                   else "gbk" if cp in (936, 0x3A4) else "cp%d" % cp)
            sys.stdout = open("CONOUT$", "w", encoding=enc, errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding=enc, errors="replace")
            sys.stdin = open("CONIN$", "r", encoding=enc, errors="replace")
        except Exception:
            pass
    else:
        if sys.stdout is None:
            sys.stdout = _NullStream()
        if sys.stderr is None:
            sys.stderr = _NullStream()
    return attached


# ============================ RSA（复刻 security.js） ============================

def _utf16_code_units(s):
    """
    等价 JS 的 s.split("").reverse().join("") 所操作的 UTF-16 code unit 序列。
    非 BMP 字符（如 emoji）在 JS 里会拆成代理对，这里保持一致。
    """
    units = []
    for ch in s:
        cp = ord(ch)
        if cp > 0xFFFF:
            cp -= 0x10000
            units.append(0xD800 + (cp >> 10))
            units.append(0xDC00 + (cp & 0x3FF))
        else:
            units.append(cp)
    return units


def eportal_rsa_encrypt(plain, exponent_hex, modulus_hex):
    """
    复刻 ePortal security.js 的 RSAUtils.encryptedString：
      1. a[i] = s.charCodeAt(i)，末尾补 0 直到 length % chunkSize == 0
      2. chunkSize = 2 * biHighIndex(modulus)
      3. 每 2 字节小端合成一个 16-bit digit：digit[j] = a[2j] + (a[2j+1] << 8)
      4. 块做 pow(block, e, m)
      5. 输出 biToHex：每个有效 digit 输出 4 位 hex，块之间用空格连接
    注意：这是"教科书 RSA"，没有 PKCS#1 v1.5 随机填充。
    """
    e = int(exponent_hex, 16)
    m = int(modulus_hex, 16)
    if m == 0:
        raise ValueError("RSA modulus 为空")

    # biHighIndex：最高非零 16-bit digit 的下标
    high_index = max(0, ((m.bit_length() + 15) // 16) - 1)
    chunk_size = 2 * high_index
    if chunk_size <= 0:
        raise ValueError("modulus 异常，无法计算 chunkSize")

    # a[i] = s.charCodeAt(i)：按 UTF-16 code unit 取值，不能截断成单字节
    data = _utf16_code_units(plain)

    # 末尾补 0，直到长度是 chunkSize 的整数倍
    while len(data) % chunk_size != 0:
        data.append(0)

    blocks = []
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i + chunk_size]
        # block.digits[j] = a[2j] + (a[2j+1] << 8)
        # 注意 digit 可能超过 16 bit（非 ASCII 时），但数学值不变，
        # BigInt 后续运算会自行进位规范化，所以这里直接累加即可。
        block_int = 0
        for j in range(len(chunk) // 2):
            block_int += (chunk[2 * j] + (chunk[2 * j + 1] << 8)) << (16 * j)
        crypt = pow(block_int, e, m)
        n_digits = max(1, (crypt.bit_length() + 15) // 16)
        blocks.append(format(crypt, "0%dx" % (n_digits * 4)))
    return " ".join(blocks)


def reverse_str(s):
    """JS: s.split("").reverse().join("") —— 按 UTF-16 code unit 反转。

    Python 的 s[::-1] 按 Unicode 码点反转，遇 emoji 等非 BMP 字符时会保留
    完整代理对；而 JS 的 split("") 会先把代理对拆开再反转，两者结果不同，
    会导致 RSA 密文不一致。这里显式按 code unit 对齐 JS 行为。
    （纯 BMP 字符——含中文、日文、ASCII——行为与 s[::-1] 完全一致。）
    """
    units = _utf16_code_units(s)
    units.reverse()
    return "".join(chr(u) for u in units)


# ============================ 凭据加密（Windows DPAPI） ============================

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_available():
    return os.name == "nt"


def dpapi_encrypt(data: bytes) -> bytes:
    """CryptProtectData：仅当前 Windows 用户可解密"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptProtectData 失败: %s" % ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(blob: bytes) -> bytes:
    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData 失败（换用户或换机器后无法解密）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


class CredentialStore:
    """密码存储：Windows 用 DPAPI，其它系统退化为 base64（会明确警告）"""

    def __init__(self, path=CRED_PATH):
        self.path = path

    def save(self, password: str):
        raw = password.encode("utf-8")
        if _dpapi_available():
            blob = dpapi_encrypt(raw)
            with open(self.path, "wb") as f:
                f.write(b"DPAPI\x01" + blob)
        else:
            import base64
            with open(self.path, "wb") as f:
                f.write(b"PLAIN\x01" + base64.b64encode(raw))
            log.warning("非 Windows 系统，密码以 base64 明文存储（不安全）")
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    def load(self):
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as f:
            blob = f.read()
        if blob.startswith(b"DPAPI\x01"):
            return dpapi_decrypt(blob[6:]).decode("utf-8")
        if blob.startswith(b"PLAIN\x01"):
            import base64
            return base64.b64decode(blob[6:]).decode("utf-8")
        # 兼容早期无标记文件
        try:
            return dpapi_decrypt(blob).decode("utf-8")
        except Exception:
            return None


# ============================ HTTP（手动控制重定向） ============================

class HttpResponse:
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.url = url

    def text(self, encodings=("utf-8", "gbk", "gb18030")):
        for enc in encodings:
            try:
                return self.body.decode(enc)
            except Exception:
                continue
        return self.body.decode("utf-8", "replace")

    def get(self, name, default=None):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default


class CookieJar:
    """极简 Cookie 罐：按 (host, port) 保存 name=value 对。

    不做 Path/Domain/Expires 完整匹配——本工具只面向单一门户，这里是
    防御性实现：实测当前门户每次都发新 JSESSIONID 且不校验，但若门户
    升级为强制会话校验，带上 Cookie 才能保证登录链路不断。
    """

    def __init__(self):
        self._cookies = {}  # (host, port) -> {name: value}

    def get(self, host, port):
        jar = self._cookies.get((host, port))
        if not jar:
            return None
        return "; ".join("%s=%s" % (k, v) for k, v in jar.items())

    def update(self, host, port, set_cookie_list):
        jar = self._cookies.setdefault((host, port), {})
        for raw in set_cookie_list:
            name, sep, val = raw.partition("=")
            name = name.strip()
            if not name:
                continue
            value = val.split(";")[0].strip()
            low = val.lower()
            if value.lower() == "deleted" or "max-age=0" in low:
                jar.pop(name, None)
            else:
                jar[name] = value


def http_request(method, host, port, path, headers=None, body=None, timeout=8,
                 cookie_jar=None):
    """发起 HTTP 请求，不自动跟随重定向。

    cookie_jar: 可选的 CookieJar。请求时自动携带该 (host,port) 的 Cookie，
    响应后把 Set-Cookie 存入罐中，实现跨请求会话保持。
    """
    hdr = {"User-Agent": UA,
           "Accept": "*/*",
           "Connection": "close"}
    if cookie_jar is not None:
        c = cookie_jar.get(host, port)
        if c:
            hdr["Cookie"] = c
    if headers:
        hdr.update(headers)
    if body is not None and isinstance(body, str):
        body = body.encode("utf-8")
    if body is not None:
        hdr.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        hdr["Content-Length"] = str(len(body))

    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request(method, path, body=body, headers=hdr)
        resp = conn.getresponse()
        data = resp.read()
        headers_dict = {}
        set_cookies = []
        for k, v in resp.getheaders():
            headers_dict[k] = v
            if k.lower() == "set-cookie":
                set_cookies.append(v)
        if cookie_jar is not None and set_cookies:
            cookie_jar.update(host, port, set_cookies)
        return HttpResponse(resp.status, headers_dict, data,
                            "http://%s:%d%s" % (host, port, path))
    except Exception as e:
        return HttpResponse(-1, {}, ("%r" % e).encode("utf-8"),
                            "http://%s:%d%s" % (host, port, path))
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def parse_inputs(html):
    """解析所有 <input>，返回 {id: value}"""
    result = {}
    for tag in re.findall(r"<input\b[^>]*>", html, re.I):
        attrs = dict(re.findall(r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']', tag))
        if "id" in attrs:
            result[attrs["id"]] = attrs.get("value", "")
        if "name" in attrs and attrs["name"] not in result:
            result.setdefault(attrs["name"], attrs.get("value", ""))
    return result


# ============================ 门户客户端 ============================

class PortalClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.host = cfg["portal_host"]
        self.port = int(cfg.get("portal_port", 80))
        self.base = cfg.get("portal_base", "/eportal").rstrip("/")
        self.timeout = float(cfg.get("timeout", 8))
        self._user_index = None
        self.cookies = CookieJar()  # 防御性：门户若启用会话校验则自动带 Cookie

    # ---------- 基础 ----------
    def url(self, path):
        return "http://%s:%d%s" % (self.host, self.port, path)

    def interface(self, method, data=None):
        path = "%s/InterFace.do?method=%s" % (self.base, method)
        body = urllib.parse.urlencode(data or {}, encoding="utf-8")
        hdr = {"Referer": self.url(self.base + "/index.jsp"),
               "X-Requested-With": "XMLHttpRequest"}
        r = http_request("POST", self.host, self.port, path,
                         headers=hdr, body=body, timeout=self.timeout,
                         cookie_jar=self.cookies)
        if r.status != 200:
            return None
        txt = r.text().strip()
        if not txt:
            return None
        try:
            return json.loads(txt)
        except Exception:
            return {"_raw": txt[:400]}

    # ---------- 连通性 ----------
    def check_online(self):
        """
        返回 (online, location)
          online=True  : 已能上外网
          online=False : 被门户劫持，location 为登录页 URL
          online=None  : 网络不通（不在校园网 / 网卡未连接）
        """
        targets = self.cfg.get("detect_targets") or []
        any_connected = False
        # 连通性探测用短超时（probe_timeout，默认 3s）：这些是公网大站，
        # 3s 足够；网络差时避免 N 目标 × 8s 串行把轮询/守护循环拖死。
        ptime = min(self.timeout, float(self.cfg.get("probe_timeout", 3)))
        if len(targets) <= 1:
            results = [http_request("GET", t["host"], 80, t.get("path", "/"),
                                    timeout=ptime) for t in targets]
        else:
            # 多目标并发探测：最坏耗时从 N×ptime 降到 ptime
            with _cf.ThreadPoolExecutor(
                    max_workers=min(len(targets), 4)) as _ex:
                results = list(_ex.map(
                    lambda t: http_request("GET", t["host"], 80,
                                           t.get("path", "/"), timeout=ptime),
                    targets))
        for t, r in zip(targets, results):
            if r.status < 0:
                continue
            any_connected = True
            if r.status in (301, 302, 303, 307, 308):
                loc = r.get("Location") or ""
                if self.host in loc or "eportal" in loc.lower():
                    return False, loc
                continue
            if r.status == 200:
                body = r.text()
                # 门户劫持也可能直接 200 返回一个跳转页面
                if self.host in body or "eportal" in body.lower():
                    m = re.search(r'(?:location|href|URL)\s*=\s*["\']([^"\']+)["\']',
                                  body, re.I)
                    if m and self.host in m.group(1):
                        return False, m.group(1)
                    continue
                expect = t.get("expect", "")
                if not expect or expect in body:
                    return True, None
        if not any_connected:
            return None, None
        return False, None

    def portal_reachable(self):
        """校园网门户本身是否可达——用于判断当前是否处于 iHBUT 校园网环境。
        校外网络（家里/热点/公共 WiFi）门户不可达，守护必须静默等待、绝不认证。"""
        r = http_request("GET", self.host, self.port, "/",
                         timeout=min(self.timeout,
                                     float(self.cfg.get("probe_timeout", 3))),
                         cookie_jar=self.cookies)
        return r.status > 0

    def get_services(self):
        return self.interface("getServices")

    def page_info(self, query_string=""):
        return self.interface("pageInfo", {"queryString": query_string})

    def online_info(self, user_index=None):
        ui = user_index or self._user_index
        if not ui:
            return None
        return self.interface("getOnlineUserInfo", {"userIndex": ui})

    def keepalive(self, user_index=None):
        ui = user_index or self._user_index
        if not ui:
            return None
        return self.interface("keepalive", {"userIndex": ui})

    # ---------- 登录页发现 ----------
    def discover_login_page(self):
        """
        找到真正的登录页 URL 并返回 (url, html)。
        未认证时，访问任意外网会被网关重定向到 portal 登录页，
        该 URL 的 query 即为后续登录所需的 queryString。
        """
        candidates = []

        online, location = self.check_online()
        if online:
            return None, None
        if location:
            candidates.append(location)

        # 兜底：直接请求门户（不跟随重定向）
        for p in ("/", self.base + "/index.jsp", self.base + "/"):
            r = http_request("GET", self.host, self.port, p,
                             timeout=self.timeout, cookie_jar=self.cookies)
            if r.status in (301, 302, 303, 307, 308):
                loc = r.get("Location")
                if loc:
                    candidates.append(urllib.parse.urljoin(self.url("/"), loc))
            elif r.status == 200:
                txt = r.text()
                if "publicKeyExponent" in txt or "passwordEncrypt" in txt:
                    candidates.append(self.url(p))
                else:
                    m = re.search(r'(?:location|href)\s*=\s*["\']([^"\']+)["\']',
                                  txt, re.I)
                    if m:
                        candidates.append(urllib.parse.urljoin(self.url("/"),
                                                               m.group(1)))

        seen = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            parsed = urllib.parse.urlparse(url)
            if not parsed.netloc:
                continue
            port = parsed.port or 80
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            r = http_request("GET", parsed.hostname, port, path,
                             timeout=self.timeout, cookie_jar=self.cookies)
            if r.status == 200:
                html = r.text()
                if "publicKeyExponent" in html or "pwd" in html:
                    return url, html
        return None, None

    # ---------- 登录 ----------
    def login(self, username, password, service_hint=None):
        url, html = self.discover_login_page()
        if not html:
            return {"result": "fail",
                    "message": "未能获取登录页（已在线？或不在校园网环境）"}

        parsed = urllib.parse.urlparse(url)
        query_string = parsed.query
        qs_params = urllib.parse.parse_qs(query_string)
        mac = (qs_params.get("mac") or [""])[0]

        fields = parse_inputs(html)
        exp = fields.get("publicKeyExponent", "")
        mod = fields.get("publicKeyModulus", "")
        encrypt_flag = fields.get("passwordEncrypt", "false")
        use_encrypt = str(encrypt_flag).lower() == "true"

        # 服务选择
        service = service_hint or self.cfg.get("service", "default")
        if service == "auto":
            service = self._auto_service(query_string)

        if use_encrypt:
            if not (exp and mod):
                return {"result": "fail",
                        "message": "登录页要求加密但未找到 RSA 公钥"}
            plain = reverse_str(password + ">" + mac)
            try:
                pwd_send = eportal_rsa_encrypt(plain, exp, mod)
            except Exception as e:
                return {"result": "fail", "message": "RSA 加密失败: %r" % e}
        else:
            pwd_send = password

        payload = {
            "userId": dq(username),
            "password": dq(pwd_send),
            "service": dq(service),
            "queryString": dq(query_string),
            "operatorPwd": "",
            "operatorUserId": "",
            "validcode": "",
            "passwordEncrypt": dq("true" if use_encrypt else "false"),
        }

        path = "%s/InterFace.do?method=login" % self.base
        body = "&".join("%s=%s" % (k, v) for k, v in payload.items())
        r = http_request("POST", self.host, self.port, path,
                         headers={"Referer": url}, body=body,
                         timeout=self.timeout, cookie_jar=self.cookies)
        if r.status != 200:
            return {"result": "fail", "message": "HTTP %s" % r.status}

        txt = r.text().strip()
        try:
            res = json.loads(txt)
        except Exception:
            return {"result": "fail", "message": "响应解析失败: %s" % txt[:200]}

        if res.get("result") == "success":
            self._user_index = res.get("userIndex")
            res["_service"] = service
            res["_encrypted"] = use_encrypt
        else:
            msg = res.get("message", "")
            for code, hint in ERR_HINT.items():
                if code in msg:
                    res["_hint"] = hint
                    break
        return res

    def _auto_service(self, query_string=""):
        info = self.page_info(query_string) or {}
        services = info.get("service") or {}
        for key, val in services.items():
            if isinstance(val, dict) and str(val.get("serviceDefault")) == "true":
                return key
        return "default"

    def logout(self, user_index=None, force_by_cred=False,
               username=None, password=None):
        ui = user_index or self._user_index
        if ui:
            return self.interface("logout", {"userIndex": ui})
        if force_by_cred and username and password:
            return self.interface("logoutByUserIdAndPass",
                                  {"userId": username, "pass": password})
        return {"result": "fail", "message": "没有可用的在线会话 userIndex"}


# ============================ 配置与守护 ============================

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            cfg.update(user_cfg)
        except Exception as e:
            log.warning("配置文件读取失败，使用默认配置: %r", e)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    """整体覆盖写 state.json。

    ⚠️ 除非你就是要删字段（如 clear_session），否则一律用 update_state()
    合并写——整体替换会丢掉 first_run_at（首跑时间戳）与 events（最近事件条）
    等由其它模块维护的字段。历史 bug：tkinter 回退 UI 登录成功时整写，
    把守护记录的 events 清空（界面"最近事件"条随之消失）。
    注：全新部署判定 first_deploy() 只看 state.json 文件是否存在、不看内容，
    所以整写不会让"空文件夹提醒"重现；但字段丢失本身就是 bug，必须合并写。
    """
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def update_state(patch, drop=()):
    """合并式更新 state.json：保留其它字段（如 first_run_at 部署标记、
    守护写入的 userIndex 等），避免整字典覆盖互相冲掉。
    drop 为需删除的字段名元组（如登录成功后清除 last_error）。"""
    st = load_state()
    for k in drop:
        st.pop(k, None)
    st.update(patch)
    save_state(st)


def clear_session():
    """下线/重置时清除会话字段（userIndex/loginTime/last_error），
    保留 first_run_at 等部署标记，避免重新触发全新部署判定。"""
    st = load_state()
    for k in ("userIndex", "loginTime", "last_error"):
        st.pop(k, None)
    save_state(st)


# 状态事件类型 → 展示文案（供 UI 即时状态条复用）
EVENT_LABEL = {
    "lost": "掉线重连",
    "restored": "恢复在线",
    "offline": "网络不可达",
    "login_ok": "认证成功",
    "login_fail": "认证失败",
    "kickout": "会话失效",
}


def record_event(kind, msg):
    """记录最近一次状态事件（掉线/重连/恢复/登录结果），写 state.json
    events 环形缓冲（保留最近 8 条），UI 轮询 /api/status 时即时展示。

    去重规则：与最后一条同 kind 且同 msg 且在 60s 内 → 只更新时间戳，
    防止"连续登录失败"这类事件风暴刷屏。其余场景（状态翻转/登录结果）
    天然低频，不会给 state.json 造成写盘压力。"""
    st = load_state()
    evs = st.get("events") or []
    now = int(time.time())
    if (evs and evs[-1]["kind"] == kind and evs[-1]["msg"] == msg
            and now - evs[-1]["t"] < 60):
        evs[-1]["t"] = now  # 同事件 60s 内合并
    else:
        evs.append({"t": now, "kind": kind, "msg": msg})
    st["events"] = evs[-8:]
    save_state(st)


def acquire_singleton():
    """防止守护进程重复启动"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLETON_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def write_pid():
    """守护启动时记录自身 PID，便于管理界面精确停止它"""
    try:
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def remove_pid():
    try:
        if os.path.exists(PID_PATH):
            os.remove(PID_PATH)
    except Exception:
        pass


def read_pid():
    """返回仍然存活的守护进程 PID，否则 None"""
    if not os.path.exists(PID_PATH):
        return None
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            # PROCESS_QUERY_LIMITED_INFORMATION
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return pid
            return None
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def daemon_running():
    """守护是否在运行：优先端口连接探测，失败时用 netstat 兜底"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", SINGLETON_PORT))
        return True
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass
    return _port_pid(SINGLETON_PORT) is not None


# CREATE_NO_WINDOW：启动子进程时不创建控制台窗口（见下方警告）
_NO_WINDOW = 0x08000000


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    """MIB_TCPROW_OWNER_PID（IPv4 TCP 连接行，含所属进程 PID）"""

    _fields_ = [("dwState", wintypes.DWORD),
                ("dwLocalAddr", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD),
                ("dwRemotePort", wintypes.DWORD),
                ("dwOwningPid", wintypes.DWORD)]


def _port_pid(port):
    """找到监听指定端口的进程 PID（用于兼容没写 PID 文件的历史守护）。

    ⚠️ 切勿改回 `netstat -ano` 子进程实现！netstat.exe / taskkill.exe /
    schtasks.exe 都是**控制台程序(CUI)**，而本程序 UI 以 `--windowed` 打包
    （GUI 子系统，自身无控制台）。在 GUI 进程里启动 CUI 子进程时 Windows 必须为
    其分配控制台：若用户把「Windows Terminal」设为默认终端应用，就会闪出一个
    终端窗口（实测每次开 UI 后约 1 秒必现，类名 CASCADIA_HOSTING_WINDOW_CLASS）。
    故这里用 IP Helper API 直查，零子进程、零闪烁，且更快。
    """
    try:
        iphlpapi = ctypes.windll.iphlpapi
        AF_INET = 2
        TCP_TABLE_OWNER_PID_LISTENER = 3
        size = wintypes.DWORD(0)
        # 第一次传 None 取所需缓冲区大小（返回 ERROR_INSUFFICIENT_BUFFER=122）
        rc = iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False,
            AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
        if rc != 122 or size.value == 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        rc = iphlpapi.GetExtendedTcpTable(
            buf, ctypes.byref(size), False,
            AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
        if rc != 0:
            return None
        n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        if n <= 0:
            return None
        rows = ctypes.cast(
            ctypes.byref(buf, 4),
            ctypes.POINTER(_MIB_TCPROW_OWNER_PID * n)).contents
        for row in rows:
            # dwLocalPort 是网络字节序（大端），需转成主机序
            local = (((row.dwLocalPort & 0xFF) << 8)
                     | ((row.dwLocalPort >> 8) & 0xFF))
            if local == port:
                return int(row.dwOwningPid)
    except Exception:
        pass
    return None


def _kill_pid(pid):
    """终止进程（TerminateProcess 直调，零子进程，不闪终端窗口）。

    见 _port_pid 的说明：这里同样不能用 taskkill.exe。
    """
    try:
        k32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        h = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _kill_by_image(image):
    """终止所有镜像名为 image 的进程（Toolhelp32 快照；零子进程）。

    onefile 模式下，daemon.pid 记录的是 Python 子进程 PID，父级引导进程
    (PyInstaller bootloader) 仍持有 exe 镜像句柄。停守护时若只杀子进程，
    父级引导会短暂残留 → 卸载组件的 os.remove 拿到共享锁失败。
    按镜像名（仅 DAEMON_EXE_NAME，绝不波及主 UI 的 CampusNetAuth.exe）
    收尾残余进程，让镜像句柄尽快释放。
    """
    try:
        k32 = ctypes.windll.kernel32
        TH32CS_SNAPPROCESS = 0x2
        PROCESS_TERMINATE = 0x0001
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                        ("th32ProcessID", ctypes.c_uint32),
                        ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", ctypes.c_uint32),
                        ("cntThreads", ctypes.c_uint32),
                        ("th32ParentProcessID", ctypes.c_uint32),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.c_uint32),
                        ("szExeFile", ctypes.c_wchar * 260)]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (0, -1):
            return 0
        killed = 0
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if k32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    if pe.szExeFile and pe.szExeFile.lower() == image.lower():
                        h = k32.OpenProcess(PROCESS_TERMINATE, False,
                                            pe.th32ProcessID)
                        if h:
                            if k32.TerminateProcess(h, 1):
                                killed += 1
                            k32.CloseHandle(h)
                    if not k32.Process32NextW(snap, ctypes.byref(pe)):
                        break
        finally:
            k32.CloseHandle(snap)
        return killed
    except Exception:
        return 0


def daemon_pid():
    """返回守护进程 PID（新实例读文件，旧实例从端口查），没有则 None"""
    pid = read_pid()
    if pid:
        return pid
    return _port_pid(SINGLETON_PORT)


def _stop_daemon_graceful():
    """向守护进程的单例端口发送优雅停止指令（守护监听该端口）。
    守护收到 STOP 后自行退出 → PID 文件/端口自动清理，
    PyInstaller bootloader 随子进程自然退出，零进程残留。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", SINGLETON_PORT))
        s.sendall(b"STOP")
        s.close()
        return True
    except Exception:
        return False


def stop_daemon():
    """停止后台守护，返回是否成功（找不到进程时返回 False）。

    优先优雅停止（守护监听单例端口，收到 STOP 自行退出，零残留）；
    指令发送失败（守护已死/端口异常）时回退按 PID 终止。
    兼容旧版 Daemon.exe 副本启动的守护（同样监听单例端口）。
    """
    pid = read_pid() or _port_pid(SINGLETON_PORT)
    graceful = False
    if pid or daemon_running():
        graceful = _stop_daemon_graceful()
    if not graceful:
        if not pid:
            return False
        try:
            ok = _kill_pid(pid)
            if not ok:
                log.error("停止守护失败：无法终止进程 PID=%s", pid)
        except Exception as e:
            log.error("停止守护失败: %r", e)
            ok = False
    # 等待守护退出（端口释放 + pid 文件清理），最多 ~6s
    deadline = time.time() + 6
    while time.time() < deadline:
        if not daemon_running():
            break
        time.sleep(0.3)
    # 兜底：进程未按预期退出时，按端口占用者 PID 强杀
    leftover = _port_pid(SINGLETON_PORT)
    if leftover:
        _kill_pid(leftover)
    remove_pid()
    alive = daemon_running()
    if not alive:
        log.info("守护已停止")
    return not alive


def _start_addr_change_watcher():
    """Windows 网络地址变化监听：断网重连 / WiFi 切换 / 插拔网线 /
    回到校园网等网络层变化 → 立即向守护发 CHECK 唤醒指令。

    把这类场景的恢复延迟从"30s 轮询/分段快探"降到秒级：
    NotifyAddrChange 阻塞等待系统通知，地址一变就返回，守护
    select 被 CHECK 即时打断后立刻复核一轮。被踢下线（IP 不变）
    不触发，仍由 30s 连通性轮询 + keepalive 失败复核兜底，两者互补。

    非 Windows 或 API 不可用时静默降级（返回 None），绝不拖垮守护。
    """
    if os.name != "nt":
        return None
    try:
        import threading as _threading
        _iphlpapi = ctypes.WinDLL("iphlpapi")
        _fn = _iphlpapi.NotifyAddrChange
        # 同步阻塞调用：两个句柄参数均传 NULL。OVERLAPPED 在新版
        # ctypes.wintypes 中已移除，用 c_void_p 替代（仅作类型声明）。
        _fn.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
        _fn.restype = wintypes.DWORD
    except Exception:
        log.debug("NotifyAddrChange 不可用，网络事件唤醒降级为纯轮询")
        return None

    _last_poke = [0.0]

    def _poke():
        # 节流：事件风暴（如 DHCP 续租连续通知）时至少间隔 5s 唤醒一次，
        # 避免守护被连续打断空转
        now = time.time()
        if now - _last_poke[0] < 5:
            return
        _last_poke[0] = now
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(("127.0.0.1", SINGLETON_PORT))
            s.sendall(b"CHECK")
            s.close()
        except Exception:
            pass

    def _loop():
        while True:
            try:
                # 同步阻塞：返回即表示 IP 地址/路由发生变化（ERROR_SUCCESS=0）。
                # 单线程串行调用满足"同一时刻仅一个未完成请求"的 API 约束。
                rc = _fn(None, None)
                if rc == 0:
                    log.info("检测到网络地址变化，唤醒守护立即复核")
                    _poke()
            except Exception:
                pass
            time.sleep(1)  # 防极端忙循环

    t = _threading.Thread(target=_loop, name="net-addr-watch", daemon=True)
    t.start()
    log.info("已启用网络事件唤醒：断网重连/WiFi 切换将即时响应（秒级复核）")
    return t


def _fmt_uptime(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def _fmt_info(t):
    """(ip, service) -> 可读字符串"""
    if not t or t[0] in (None, "?"):
        return "未知"
    if t[1] and t[1] != "?":
        return "%s/%s" % (t[0], t[1])
    return str(t[0])


def run_daemon(cfg, cred):
    lock = acquire_singleton()
    if lock is None:
        log.warning("已有守护实例在运行（端口 %d 被占用），本次退出", SINGLETON_PORT)
        return 1

    write_pid()
    atexit.register(remove_pid)

    client = PortalClient(cfg)
    username = cfg["username"]
    interval = int(cfg.get("interval", 30))
    retry = int(cfg.get("retry_interval", 30))
    max_interval = int(cfg.get("max_interval", 600))
    offline_interval = int(cfg.get("offline_interval", 60))
    timeout = int(cfg.get("timeout", 8))
    keepalive_iv = int(cfg.get("keepalive_interval", 300))
    recheck_iv = int(cfg.get("recheck_interval", 30))
    verify_iv = max(2, int(cfg.get("verify_interval", 5)))
    fail_streak = 0
    portal_ok = False  # 上次门户可达状态；False→True 时重置退避，恢复后快速重连
    persisted_index = None  # 已写入 state.json 的 userIndex，避免重复写盘
    last_keepalive = 0.0   # 上次 keepalive 续约时间（主动防踢）
    prev_online = None     # 上一轮探测结果，用于状态翻转事件去重

    # ---- 运行统计与心跳节流 ----
    started_at = time.time()
    stats = {"loops": 0, "login_ok": 0, "login_fail": 0,
             "reconnects": 0, "state_flips": 0, "keepalive_ok": 0}
    last_info = None  # 上次登录成功的 (IP, 服务)，用于检测网络身份变化
    last_heartbeat = 0.0
    HB_INTERVAL = 300  # 心跳日志节流：每 5 分钟一条

    log.info("守护启动 | 账号=%s | 服务=%s | 门户=%s:%s | 检测周期=%ss | "
             "离线退避=%ss | 失败重试=%ss | 最长退避=%ss | 请求超时=%ss | "
             "保活=%ss | 恢复快探=%ss",
             mask_user(username), cfg.get("service"),
             cfg.get("portal_host", "?"), cfg.get("portal_port", "?"),
             interval, offline_interval, retry, max_interval, timeout,
             keepalive_iv, recheck_iv)
    log.info("守护规则：仅当校园网(iHBUT)门户可达且未认证时才登录，校外网络静默等待；"
             "在线时每 %ss 主动续约会话，防止认证过期被踢", keepalive_iv)

    # 网络事件被动唤醒（可选增强）：IP/网络地址变化时立即复核，
    # 断网重连、WiFi 切换、回到校园网等场景从 30s 轮询延迟降到秒级。
    _start_addr_change_watcher()

    def _hb():
        """在线静默期的心跳日志，证明守护存活（不刷屏）"""
        nonlocal last_heartbeat
        now = time.time()
        if now - last_heartbeat >= HB_INTERVAL:
            last_heartbeat = now
            log.info("存活心跳 | 已运行 %s | 在线 | 累计登录成功=%d 失败=%d",
                     _fmt_uptime(now - started_at),
                     stats["login_ok"], stats["login_fail"])

    import select as _select

    def _wait(sec):
        """可中断等待：阻塞至超时或收到指令（STOP=优雅退出 / CHECK=唤醒复核）。

        select 可被 socket 数据即时打断，比 time.sleep 多了"立即响应"能力：
        UI 手动操作后发 CHECK，等待中的守护立刻醒来复核，不等完整周期。"""
        try:
            _ready, _, _ = _select.select([lock], [], [], sec)
            if not _ready:
                return None
            _conn, _ = lock.accept()
            try:
                _data = _conn.recv(16).decode("utf-8", "replace").strip().upper()
            finally:
                _conn.close()
            if _data == "STOP":
                log.info("收到停止指令，守护优雅退出")
                return "STOP"
            if _data == "CHECK":
                # UI 手动登录/操作后发来：结束等待，立即进入本轮探测
                log.info("收到唤醒指令，立即复核网络状态")
            return None
        except Exception:
            return None

    while True:
        try:
            stats["loops"] += 1

            # 门户守卫：仅当"尚未确认在校园网"时才探测门户；已确认（portal_ok）
            # 则跳过 —— 外网在线必然说明校园网在线，无需每轮重复探测门户
            # （在线路径每轮从 4 次 HTTP 降到 3 次并发）。
            # 校外网络（家里/热点/公共 WiFi）门户不可达，守护静默等待、绝不认证。
            if not portal_ok:
                if not client.portal_reachable():
                    wait = min(offline_interval * min(fail_streak, 10),
                               max_interval)
                    if fail_streak == 0:
                        log.info("未检测到校园网门户（不在 iHBUT 网络），守护静默等待，"
                                 "不会尝试认证")
                    elif fail_streak % 10 == 1:
                        log.info("仍不在校园网：已连续 %d 次探测不到门户，退避等待 %ss",
                                 fail_streak, wait)
                    fail_streak += 1
                    # 退避等待分段快探：每 recheck_iv 秒轻探一次门户，
                    # 回到校园网立即恢复，不等完整退避周期
                    _slept = 0
                    while _slept < wait:
                        _seg = min(recheck_iv, wait - _slept)
                        if _wait(_seg) == "STOP":
                            return 0
                        _slept += _seg
                        if client.portal_reachable():
                            log.info("检测到校园网门户，提前结束退避，立即重连")
                            break
                    continue
                # 门户从不可达变为可达：重置退避计数，避免用校外累积的大间隔等待
                portal_ok = True
                if fail_streak:
                    stats["state_flips"] += 1
                    fail_streak = 0
                    log.info("校园网门户已恢复，重置检测退避，立即重试")

            online, location = client.check_online()
            # ---- 状态事件上浮：仅在状态翻转时记录（首轮不产生假事件）----
            if online != prev_online:
                if online is True and prev_online is False:
                    record_event("restored", "网络已恢复在线")
                elif online is False and prev_online is True:
                    record_event("lost", "检测到未认证，正在自动重连…")
                elif online is None and prev_online is not None:
                    record_event("offline", "网络不可达，等待联网…")
                prev_online = online
            if online:
                if fail_streak:
                    log.info("网络已恢复")
                fail_streak = 0
                # 持久化当前 userIndex，便于 logout 命令在"开机即在线"时也能用
                try:
                    info = client.interface("getOnlineUserInfo")
                    if isinstance(info, dict) and info.get("userIndex"):
                        if persisted_index != info["userIndex"]:
                            persisted_index = info["userIndex"]
                            st = load_state()
                            st["userIndex"] = persisted_index
                            save_state(st)
                except Exception:
                    pass
                _hb()
                # ---- keepalive 主动防踢 ----
                # 在线时定期续约会话，降低认证过期被踢的概率；续约失败立即
                # 复核在线状态，确认未认证就 continue 回循环头（当轮即重登）。
                if (persisted_index
                        and time.time() - last_keepalive >= keepalive_iv):
                    try:
                        _kr = client.keepalive(persisted_index)
                        if (isinstance(_kr, dict)
                                and str(_kr.get("result", "")).lower()
                                == "success"):
                            last_keepalive = time.time()
                            stats["keepalive_ok"] += 1
                        else:
                            # 续约未成功：可能会话已失效 → 立即复核。
                            # 失败也更新 last_keepalive，节流防重试风暴
                            # （被踢场景由 30s 连通性探测兜底发现并重登）。
                            last_keepalive = time.time()
                            log.info("keepalive 续约未成功（%s），立即复核在线状态…",
                                     safe_message(_kr) if isinstance(_kr, dict)
                                     else "无响应")
                            _online2, _ = client.check_online()
                            if _online2 is False:
                                continue  # 回循环头：本轮回合将发现未认证并立即登录
                    except Exception:
                        last_keepalive = time.time()
                if _wait(interval) == "STOP":
                    return 0
                continue

            if online is None:
                # 网络完全不可达：可能不在校园网 / 未连 WiFi。
                # 重置"已确认在校园网"标记，下一轮重新探测门户归属。
                portal_ok = False
                wait = min(offline_interval * min(fail_streak, 10),
                           max_interval)
                if fail_streak == 0:
                    log.info("网络不可达（可能不在校园网环境），等待联网…")
                elif fail_streak % 10 == 1:
                    log.info("网络仍不可达：已连续 %d 次，退避等待 %ss",
                             fail_streak, wait)
                fail_streak += 1
                # 退避等待分段快探：每 recheck_iv 秒复核一次外网连通性，
                # 网络恢复立即回到在线检测，不等完整退避周期
                _slept = 0
                while _slept < wait:
                    _seg = min(recheck_iv, wait - _slept)
                    if _wait(_seg) == "STOP":
                        return 0
                    _slept += _seg
                    if client.check_online()[0]:
                        log.info("网络已恢复，提前结束退避，立即重试")
                        break
                continue

            log.info("检测到未认证，开始登录…")
            res = client.login(username, cred)
            if res.get("result") == "success":
                stats["login_ok"] += 1
                if fail_streak:
                    stats["reconnects"] += 1
                    log.info("断线自动重连成功 ✓（累计自动重连 %d 次）",
                             stats["reconnects"])
                fail_streak = 0
                if res.get("userIndex"):
                    # 合并写：保留 first_run_at 等其它字段，并清除上次的登录错误提示
                    update_state({"userIndex": res["userIndex"],
                                  "loginTime": time.strftime("%Y-%m-%d %H:%M:%S")},
                                 drop=("last_error",))
                info = client.online_info()
                extra = ""
                cur = None
                if isinstance(info, dict):
                    cur = (info.get("userIp", "?"),
                           info.get("service", "?"))
                    extra = " | IP=%s 服务=%s 套餐=%s" % (
                        cur[0], cur[1], info.get("userGroup", "?"))
                if last_info and last_info != cur:
                    log.info("网络身份变化: %s -> %s",
                             _fmt_info(last_info), _fmt_info(cur))
                last_info = cur
                log.info("登录成功%s", extra)
                record_event("login_ok", "认证成功%s" % extra)
                # ---- 登录后即时确认（防"假成功"）----
                # result=success 不代表会话真的建立（网关偶发假成功/被抢线）；
                # 短等 verify_iv 秒后立即复核一次在线状态，仍被劫持就
                # 当轮补登，不等完整周期。用 _wait 而非 sleep，保持可中断。
                if _wait(verify_iv) == "STOP":
                    return 0
                _online3, _ = client.check_online()
                if _online3 is False:
                    log.info("登录后复核未通过（会话未生效），立即补登…")
                    continue
                if _wait(interval) == "STOP":
                    return 0
            else:
                stats["login_fail"] += 1
                fail_streak += 1
                msg = res.get("message")
                msg = str(msg) if msg else ""
                hint = res.get("_hint", "")
                wait = min(retry * fail_streak, max_interval)
                log.error("登录失败: %s%s | 累计失败=%d | 下次重试=%ss",
                          msg, ("（%s）" % hint) if hint else "",
                          stats["login_fail"], wait)
                record_event("login_fail", "登录失败：%s" % (msg or hint or "未知错误"))
                if any(k in msg for k in
                       ("PASS", "密码", "USER", "账号", "EXIST", "FORBIDDEN")):
                    log.error("疑似账号/密码问题，请运行 `campusnet.py setup` 重新配置")
                    # 即时上浮到 UI：/api/status 透出 last_error，状态卡警示
                    update_state({"last_error": {
                        "t": int(time.time()),
                        "msg": "疑似账号/密码问题：请在「设置」重新配置账号"}})
                    wait = min(retry * 20, max_interval)
                if _wait(wait) == "STOP":
                    return 0
        except KeyboardInterrupt:
            log.info("守护被手动终止")
            log.info("守护退出 | 已运行 %s | 循环 %d 次 | 登录成功=%d | "
                     "登录失败=%d | 自动重连=%d | 网络状态切换=%d",
                     _fmt_uptime(time.time() - started_at),
                     stats["loops"], stats["login_ok"], stats["login_fail"],
                     stats["reconnects"], stats["state_flips"])
            return 0
        except Exception as e:
            fail_streak += 1
            log.exception("守护循环异常: %r", e)
            wait = min(retry * fail_streak, max_interval)
            log.error("异常后等待 %ss 重试（第 %d 次连续异常）", wait, fail_streak)
            if _wait(wait) == "STOP":
                return 0


# ============================ 自启动 ============================

def pythonw_path():
    d = os.path.dirname(sys.executable)
    p = os.path.join(d, "pythonw.exe")
    if os.path.exists(p):
        return p
    return sys.executable


# ============================ 模块化组件系统 ============================
# 单文件便携模式：exe 自带守护全部能力，组件文件（vbs 启动器）
# 由用户从 UI 按需「安装」放出、按需「卸载」移除。
# 注：2026-08-31 起不再有 CampusNetAuthDaemon.exe 命名副本（守护 = 同一
# exe + daemon 参数），DAEMON_EXE_NAME 仅用于识别/清理历史遗留文件。

DAEMON_EXE_NAME = "CampusNetAuthDaemon.exe"
DAEMON_VBS_NAME = "CampusNetAuthDaemon.vbs"
UI_VBS_NAME = "CampusNetAuthUI.vbs"

# 程序自身产生的全部文件（foreign_files 里排除，避免把自家产物当"无关文件"）
_PRODUCT_FILES = (
    DAEMON_EXE_NAME, DAEMON_VBS_NAME, UI_VBS_NAME,
    "config.json", "cred.bin", "state.json",
    "campusnet.log", "daemon.pid", "_ui_trace.log", "_ui_crash.log",
)
# 判定"是否全新部署"只认部署产物（不认运行时文件 log/pid，
# 否则首次启动瞬间就会被视为已部署，空文件夹提醒失效）
_DEPLOY_MARKERS = (
    DAEMON_EXE_NAME, DAEMON_VBS_NAME, UI_VBS_NAME,
    "config.json", "cred.bin", "state.json",
)


def _asset_path(name):
    """内置资源路径：打包态在 PyInstaller 解包目录（--add-data），
    开发态直接读脚本所在目录的源文件。"""
    if getattr(sys, "frozen", False):
        p = os.path.join(getattr(sys, "_MEIPASS", ""), name)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def release_asset(name, force=False):
    """把内置资源释放到 BASE_DIR（已存在且未要求强制时视为已安装）。
    返回是否可用（已存在或释放成功）。"""
    src = _asset_path(name)
    if not os.path.exists(src):
        log.error("内置资源缺失: %s（打包时未打入？）", name)
        return False
    dst = os.path.join(BASE_DIR, name)
    if os.path.exists(dst) and not force:
        return True
    try:
        shutil.copy2(src, dst)
        log.info("已释放组件文件: %s", dst)
        return True
    except Exception as e:
        log.error("释放 %s 失败: %r", name, e)
        return False


def _remove_file(name):
    """移除组件文件。Windows 上进程刚退出时文件可能还被引导进程短暂持有
    共享锁（onefile 模式尤其明显），故带短重试：最多 ~6 秒。"""
    p = os.path.join(BASE_DIR, name)
    deadline = time.time() + 6
    last_err = None
    while time.time() < deadline:
        if not os.path.exists(p):
            return True
        try:
            os.remove(p)
            log.info("已移除组件文件: %s", p)
            return True
        except Exception as e:
            last_err = e
            time.sleep(0.3)
    log.error("移除 %s 失败: %r", name, last_err)
    return False


def first_deploy():
    """是否全新部署：BASE_DIR 中没有任何部署产物
    （config/cred/state/vbs/Daemon.exe 任一存在即视为已部署）。"""
    return not any(os.path.exists(os.path.join(BASE_DIR, f))
                   for f in _DEPLOY_MARKERS)


# 本次会话的"无关文件"快照：首次查询时缓存。
# 首次运行 UI 会立刻落盘 state.json（mark_first_run），此后 live 的
# first_deploy() 已变 False —— 若这里不缓存，横幅会在 20 秒后下次轮询时
# 自动消失，用户可能没看到。缓存后：横幅本次会话保持可见直到手动关闭；
# 下一次启动 first_deploy() 为 False → 不再提醒（"第二次不提醒，已展开文件"）。
_FOREIGN_CACHE = None


def foreign_files():
    """全新部署时，目录里除自身 exe 与程序产物外的其他文件
    （用于"建议放空文件夹"提醒）。已部署过则返回空列表。

    结果按会话缓存：即使首次运行已落盘 state.json（部署标记），
    本会话仍按启动时的目录快照判定，横幅不会中途消失。
    """
    global _FOREIGN_CACHE
    if not FIRST_DEPLOY:
        return []
    if _FOREIGN_CACHE is None:
        me = os.path.basename(sys.executable).lower()
        try:
            names = os.listdir(BASE_DIR)
        except Exception:
            names = []
        _FOREIGN_CACHE = [n for n in names
                          if n.lower() != me and n not in _PRODUCT_FILES
                          and not n.startswith(".")]
    return list(_FOREIGN_CACHE)


def mark_first_run():
    """首次运行落盘部署标记（state.json），使第二次启动起 first_deploy() 为 False。

    语义：程序首跑即"展开文件"（state.json 落地），文件夹从第二次起不再视为
    全新部署 → 空文件夹提醒只出现一次。幂等：state.json 已存在时不改内容。
    """
    if not FIRST_DEPLOY:
        return
    st = load_state()
    if "first_run_at" not in st:
        st["first_run_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(st)


def _ensure_daemon_vbs():
    """值守组件安装：释放守护启动器 CampusNetAuthDaemon.vbs（约 1.2KB 容错版）。

    守护核心能力内置在 exe 中，用「同一 exe + daemon 参数」启动，
    vbs 只是无窗口启动器（与开机自启共用同一文件）。
    不再复制 18.8MB 的 CampusNetAuthDaemon.exe 命名副本（2026-08-31 优化）。"""
    return release_asset(DAEMON_VBS_NAME)


def daemon_component_installed():
    """值守组件已装 = 守护启动器 vbs 就位（可手动启动守护）。"""
    return os.path.exists(os.path.join(BASE_DIR, DAEMON_VBS_NAME))


def ui_component_installed():
    return os.path.exists(os.path.join(BASE_DIR, UI_VBS_NAME))


def autostart_component_status():
    """开机自启组件状态：以 Run 键 + 启动器 vbs 完整性共同判断。
    missing=未安装 / installed=启动器就位但未启用 / enabled=已安装并启用

    ⚠️ 2026-09-01 修复：仅看 Run 键不看启动器文件 → Run 键残留但 vbs 缺失时
    误报 enabled，导致"已启用"误导用户，且开机自启必失败（80070002）。
    修复：vbs 缺失统一视为 missing（无论 Run 键是否残留）。"""
    vbs_ok = os.path.exists(os.path.join(BASE_DIR, DAEMON_VBS_NAME))
    if not vbs_ok:
        # 启动器缺失 → 视为未安装（无论 Run 键是否残留，避免假象）
        return "missing"
    if autostart_status():
        return "enabled"
    return "installed"


def components_status():
    return {
        "autostart": autostart_component_status(),
        "daemon": "installed" if daemon_component_installed() else "missing",
        "ui": "installed" if ui_component_installed() else "missing",
    }


def install_component(name):
    """安装组件，返回 (ok, message)。
    autostart=Daemon.vbs+注册表 Run 键；daemon=守护启动器 vbs（不再复制 exe 副本）；
    ui=UI.vbs 静默启动器。"""
    if name == "autostart":
        if not release_asset(DAEMON_VBS_NAME):
            return False, "释放 Daemon.vbs 失败（资源缺失，请重新打包）"
        if not install_autostart():
            return False, "写入开机自启注册表失败，详见日志"
        return True, "开机自启组件已安装并启用"
    if name == "daemon":
        if not _ensure_daemon_vbs():
            return False, "释放守护启动器失败（资源缺失，请重新打包）"
        # 安装即生效：启动器就位后立即拉起守护（已运行则幂等跳过），
        # 避免"装了忘启动"的空窗期
        try:
            if not daemon_running():
                start_daemon()
        except Exception as e:
            log.warning("值守组件安装后自动启动守护失败: %r", e)
        return True, "值守组件已安装（守护启动器 vbs 已就位，不再复制 exe 副本）"
    if name == "ui":
        if not release_asset(UI_VBS_NAME):
            return False, "释放 UI.vbs 失败（资源缺失，请重新打包）"
        return True, "界面启动器组件已安装"
    return False, "未知组件: %s" % name


def uninstall_component(name):
    """卸载组件：移除放出的文件（自启顺带删除注册表 Run 键）。
    返回 (ok, message)。"""
    if name == "autostart":
        uninstall_autostart()
        # vbs 归值守组件管理：自启卸载只删 Run 键，保留 vbs 供守护使用
        return True, "开机自启组件已卸载（Run 键已删除，守护启动器保留）"
    if name == "daemon":
        if daemon_running():
            return False, "守护正在运行，请先「停止守护」再卸载"
        if autostart_status():
            return False, "开机自启仍在启用中（Run 键依赖此启动器），请先卸载「开机自启组件」"
        ok = _remove_file(DAEMON_VBS_NAME)
        return ok, ("值守组件已卸载" if ok
                    else "移除 Daemon.vbs 失败，详见日志")
    if name == "ui":
        ok = _remove_file(UI_VBS_NAME)
        return ok, ("界面启动器组件已卸载" if ok
                    else "移除 UI.vbs 失败，详见日志")
    return False, "未知组件: %s" % name


def daemon_exe():
    """返回守护进程可执行文件路径（严格组件模式）。

    * 打包态：直接返回自身 exe（守护 = 同一 CampusNetAuth.exe + daemon 参数，
      不再需要 CampusNetAuthDaemon.exe 命名副本；组件守卫由调用方按
      daemon_component_installed() 判定）。
    * 开发态：保持 pythonw 命名副本逻辑（开发环境无组件概念）。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    d = os.path.dirname(sys.executable)
    exe = os.path.join(d, DAEMON_EXE_NAME)
    if os.path.exists(exe):
        return exe
    src = pythonw_path()
    try:
        if os.path.abspath(src).lower() != os.path.abspath(exe).lower():
            shutil.copyfile(src, exe)
        if os.path.exists(exe):
            log.info("已创建守护命名副本: %s", exe)
            return exe
    except Exception as e:
        log.warning("创建守护命名副本失败(%r)，回退 pythonw", e)
    return src


# 启动早期快照：会话期间即使生成了 config/log 也不改变"本次是否全新部署"
FIRST_DEPLOY = first_deploy()


def start_daemon():
    """以无窗口方式启动守护，返回是否成功。
    打包态未安装「值守组件」（守护启动器 vbs 缺失）时返回 False。"""
    if daemon_running():
        return True
    if not daemon_component_installed():
        log.error("值守组件未安装（缺少 %s），请先在 UI 安装",
                  DAEMON_VBS_NAME)
        return False
    try:
        exe = daemon_exe()
        if getattr(sys, "frozen", False):
            # 打包态：守护 = 同一 CampusNetAuth.exe + "daemon" 参数；
            # CREATE_NO_WINDOW 保证无黑窗（不再需要 Daemon.exe 命名副本）
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            subprocess.Popen([exe, "daemon"], cwd=BASE_DIR,
                             creationflags=flags, close_fds=True)
        else:
            script = os.path.abspath(__file__)
            subprocess.Popen(
                [exe, script, "daemon"], cwd=BASE_DIR,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                close_fds=True)
        return True
    except Exception as e:
        log.error("守护启动失败: %r", e)
        return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_cmd():
    """注册表 Run 键里存放的启动命令。

    * 打包态：优先 wscript 静默运行同目录的 CampusNetAuthDaemon.vbs
      （窗口样式 0 = 完全无窗口，登录时不闪任何黑窗）；
      vbs 缺失（被手动删除）时退化为直接运行 exe（仍会被 hide_console 隐藏）。
    * 开发态：pythonw 命名副本 + 脚本参数（保持原有逻辑）。
    """
    if getattr(sys, "frozen", False):
        vbs = os.path.join(BASE_DIR, DAEMON_VBS_NAME)
        if os.path.exists(vbs):
            return 'wscript.exe "%s"' % vbs
        return '"%s" daemon' % os.path.abspath(sys.executable)
    script = os.path.abspath(__file__)
    return '"%s" "%s" daemon' % (daemon_exe(), script)


def _clean_legacy_task():
    """清理历史版本用 schtasks 创建的计划任务（尽力而为，忽略失败）"""
    try:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, timeout=10,
                       creationflags=_NO_WINDOW)
    except Exception:
        pass


def install_autostart():
    """写入 HKCU 运行键：当前用户级自启，无需管理员权限。
    schtasks 在受限环境会报“拒绝访问”，注册表方案更可靠。"""
    # 前置校验：打包态自启必须经 vbs 静默启动器，vbs 缺失时直接拒绝，
    # 避免装出一个"开机必弹 80070002"的残缺自启（用户侧已出现此报错）。
    if getattr(sys, "frozen", False):
        if not os.path.exists(os.path.join(BASE_DIR, DAEMON_VBS_NAME)):
            log.error("安装开机自启失败: 启动器 %s 缺失", DAEMON_VBS_NAME)
            return False
        if not os.path.exists(os.path.join(BASE_DIR, "CampusNetAuth.exe")):
            log.error("安装开机自启失败: CampusNetAuth.exe 不在程序目录")
            return False
    try:
        import winreg
        cmd = _autostart_cmd()
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY,
                                0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, TASK_NAME, 0, winreg.REG_SZ, cmd)
        _clean_legacy_task()
        log.info("开机自启已安装（注册表 Run 键）")
        log.info("  启动命令: %s", cmd)
        return True
    except Exception as e:
        log.error("安装开机自启失败: %r", e)
        return False


def uninstall_autostart():
    """删除 HKCU 运行键，并顺带清理历史 schtasks 任务"""
    ok = True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, TASK_NAME)
    except FileNotFoundError:
        pass  # 本来就没装
    except Exception as e:
        ok = False
        log.error("移除开机自启失败: %r", e)
    _clean_legacy_task()
    if ok:
        log.info("开机自启已移除")
    return ok


def autostart_status():
    """HKCU 运行键是否存在（兼容历史 schtasks 残留）"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            winreg.QueryValueEx(k, TASK_NAME)
        return True
    except FileNotFoundError:
        pass
    except Exception:
        pass
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, timeout=10,
                           creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def autostart_health():
    """自启链路健康检查：Run 键 → 启动器（vbs/exe）→ exe 全链路文件完整性。

    返回 (ok, reason)：
    * 未启用自启 / Run 键读不到 → (True, "")（未安装不属异常，不打扰用户）
    * Run 键指向的 vbs 或 exe 缺失 → (False, 原因)。这正是"开机弹
      80070002 / 系统找不到指定的文件"报错的根因场景，前端据此显示红色警示条，
      引导用户补齐文件或重装自启组件，而不是等下次开机再弹窗。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            value, _ = winreg.QueryValueEx(k, TASK_NAME)
    except FileNotFoundError:
        return True, ""  # 自启未安装
    except Exception:
        return True, ""  # 读失败不误报（不打扰）
    value = (value or "").strip()
    m = re.search(r'"([^"]+)"', value) or re.match(r"(\S+)", value)
    if not m:
        return True, ""
    ref = m.group(1).strip()
    if not ref:
        return True, ""
    # 只对"看起来是文件引用"的 Run 值做存在性校验；无法解析（垃圾值）
    # 视为健康，避免误报打扰
    if not re.search(r"(?i)\.(vbs|exe)$", ref):
        return True, ""
    if ref.lower().endswith(".vbs"):
        exe_path = os.path.join(os.path.dirname(ref), "CampusNetAuth.exe")
        if not os.path.exists(ref):
            return False, "开机自启指向的启动器已丢失：" + ref
        if not os.path.exists(exe_path):
            return False, ("开机自启将失败：启动器目录缺少 CampusNetAuth.exe，"
                           "请把 exe 与 " + DAEMON_VBS_NAME + " 放在同一文件夹，"
                           "或卸载后重新安装「开机自启组件」")
        return True, ""
    if not os.path.exists(ref):
        return False, "开机自启指向的程序已丢失：" + ref
    return True, ""


# ============================ 子命令 ============================

def cmd_setup(args):
    cfg = load_config()
    print("=" * 58)
    print(" 校园网无感认证 —— 初始化配置")
    print("=" * 58)

    host = input("门户地址 [%s]: " % cfg["portal_host"]).strip()
    if host:
        if "://" in host:
            host = urllib.parse.urlparse(host).netloc
        cfg["portal_host"] = host.split(":")[0]
        if ":" in host:
            cfg["portal_port"] = int(host.split(":")[1])

    user = input("账号(学号) [%s]: " % cfg["username"]).strip()
    if user:
        cfg["username"] = user

    print("可选服务: default=校园网, DX=电信, YD=移动, LT=联通, auto=自动")
    svc = input("服务 [%s]: " % cfg.get("service")).strip()
    if svc:
        cfg["service"] = svc

    pwd = getpass.getpass("密码（输入不回显）: ")
    if not pwd:
        print("! 密码为空，未保存")
        return 1
    pwd2 = getpass.getpass("再输一次确认: ")
    if pwd != pwd2:
        print("! 两次输入不一致")
        return 1

    if any(ord(c) > 127 for c in pwd):
        print("\n! 警告：密码包含非 ASCII 字符（如中文）。")
        print("  门户的 JS 加密库对此类字符处理存在缺陷，本地加密结果可能与浏览器不一致，")
        print("  可能导致登录失败。建议把密码改成纯字母/数字/常见符号。")
        if input("  仍要继续保存？[y/N]: ").strip().lower() not in ("y", "yes"):
            print("已取消")
            return 1

    cfg["username"] = cfg["username"] or user
    save_config(cfg)
    CredentialStore().save(pwd)
    print("\n配置已写入: %s" % CONFIG_PATH)
    print("密码已用 Windows DPAPI 加密保存: %s（仅当前用户可解密）" % CRED_PATH)

    if input("\n是否立即测试登录？[Y/n]: ").strip().lower() in ("", "y", "yes"):
        setup_logger(to_console=True, level=cfg.get("log_level", "INFO"))
        return cmd_login(argparse.Namespace(force=True, quiet=False))
    return 0


def cmd_login(args):
    cfg = load_config()
    cred = CredentialStore().load()
    if not cfg["username"] or not cred:
        log.error("尚未配置账号密码，请先运行: campusnet.py setup")
        return 1
    client = PortalClient(cfg)

    if not getattr(args, "force", False):
        online, _ = client.check_online()
        if online:
            log.info("当前已在线，无需登录（加 --force 可强制重登）")
            return 0
    else:
        # --force：先下线旧会话，让门户重新放行登录页，否则 login()
        # 拿不到登录页（discover_login_page 在线时返回空）而失败
        ui = load_state().get("userIndex")
        if not ui:
            info = client.interface("getOnlineUserInfo")
            if isinstance(info, dict):
                ui = info.get("userIndex")
        if ui:
            r = client.logout(user_index=ui)
            if isinstance(r, dict) and str(r.get("result")).lower() == "success":
                log.info("已下线旧会话（--force），重新认证…")
            else:
                log.info("下线旧会话未确认（%s），尝试重新认证…",
                         safe_message(r))
        time.sleep(1)

    res = client.login(cfg["username"], cred)
    if res.get("result") == "success":
        log.info("登录成功 | 服务=%s | 加密=%s",
                 res.get("_service"), res.get("_encrypted"))
        if res.get("userIndex"):
            update_state({"userIndex": res["userIndex"],
                          "loginTime": time.strftime("%Y-%m-%d %H:%M:%S")},
                         drop=("last_error",))
        info = client.interface("getOnlineUserInfo",
                                {"userIndex": res.get("userIndex", "")})
        if isinstance(info, dict):
            log.info("  姓名=%s  IP=%s  用户组=%s  在线时长上限=%s",
                     info.get("userName"), info.get("userIp"),
                     info.get("userGroup"), info.get("maxLeavingTime"))
        return 0
    msg = res.get("message")
    msg = str(msg) if msg else ""
    log.error("登录失败: %s%s", msg,
              ("（%s）" % res["_hint"]) if res.get("_hint") else "")
    return 2


def cmd_once(args):
    """单次检测：离线才登录（适合任务计划定时调用）"""
    cfg = load_config()
    cred = CredentialStore().load()
    if not cfg["username"] or not cred:
        log.error("尚未配置账号密码")
        return 1
    client = PortalClient(cfg)
    online, _ = client.check_online()
    if online:
        log.info("已在线，无需操作")
        return 0
    if online is None:
        log.info("网络不可达，跳过")
        return 0
    res = client.login(cfg["username"], cred)
    if res.get("result") == "success":
        log.info("登录成功")
        return 0
    log.error("登录失败: %s", res.get("message", ""))
    return 2


def cmd_status(args):
    cfg = load_config()
    client = PortalClient(cfg)
    online, loc = client.check_online()
    print("门户地址 : %s" % client.url(""))
    print("账号     : %s" % (cfg.get("username") or "(未配置)"))
    print("服务     : %s" % cfg.get("service"))
    print("网络状态 : %s" % {True: "已认证/可上网",
                             False: "未认证（被门户劫持）",
                             None: "网络不可达"}[online])
    print("开机自启 : %s" % ("已启用" if autostart_status() else "未启用"))
    print("守护进程 : %s" % (
        "运行中(PID=%s)" % daemon_pid() if daemon_running() else "已停止"))
    print("凭据文件 : %s" % ("已存在" if os.path.exists(CRED_PATH) else "缺失"))
    if loc:
        print("登录页   : %s" % loc[:160])
    if online:
        # 已在线时，若知道 userIndex 才能查详情；这里用 pageInfo 展示可用服务
        info = client.page_info()
        if isinstance(info, dict) and info.get("service"):
            names = []
            for k, v in info["service"].items():
                if isinstance(v, dict):
                    names.append("%s(%s)" % (v.get("serviceShowName", k), k))
            print("可选服务 : %s" % ", ".join(names))
    return 0


def cmd_log(args):
    """打印最近 N 行日志。

    日志文件按 UTF-8 写入；本命令显式按 UTF-8 读取并配合
    force_utf8_console() 输出，避免 bat/PowerShell 按 GBK 解码导致乱码。
    """
    n = max(1, int(getattr(args, "lines", 15) or 15))
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        print("无法读取日志: %r" % e)
        return 1
    tail = lines[-n:]
    if tail:
        sys.stdout.write("".join(tail))
    else:
        print("（日志为空）")
    return 0


def cmd_logout(args):
    """下线：优先用持久化的 userIndex，其次用会话取回 userIndex，最后用账号密码兜底"""
    cfg = load_config()
    client = PortalClient(cfg)
    username = cfg.get("username", "")
    cred = CredentialStore().load()

    def by_index(ui):
        """用 userIndex 下线，成功返回 True，失败返回响应（dict/None）"""
        if not ui:
            return None
        res = client.logout(user_index=ui)
        if isinstance(res, dict) and str(res.get("result")).lower() == "success":
            return True
        return res

    steps = []  # 记录各方式失败原因，便于排查

    # 1) 持久化的 userIndex（守护每次登录都会写入 state.json）
    ui = load_state().get("userIndex")
    r = by_index(ui)
    if r is True:
        clear_session()
        log.info("已下线（userIndex 方式）")
        return 0
    if r is not True:
        steps.append("userIndex 方式: " + safe_message(r))

    # 2) 在线时，门户会随会话返回当前 userIndex，取回后再下线
    info = client.interface("getOnlineUserInfo")
    if isinstance(info, dict) and info.get("userIndex"):
        r = by_index(info["userIndex"])
        if r is True:
            clear_session()
            log.info("已下线（会话方式）")
            return 0
        if r is not True:
            steps.append("会话方式: " + safe_message(r))
    else:
        # 拿不到 userIndex：很可能本来就未在线，无需下线
        online, _ = client.check_online()
        if online is not True:
            log.info("当前未在线，无需下线（或网络不可达）")
            return 0

    # 3) 凭据兜底：用账号密码强制下线
    if username and cred:
        res = client.logout(force_by_cred=True, username=username, password=cred)
        if isinstance(res, dict) and str(res.get("result")).lower() == "success":
            clear_session()
            log.info("已下线（凭据方式）")
            return 0
        steps.append("凭据方式: " + safe_message(res))

    log.error("下线失败：%s", "；".join(steps) or safe_message(info))
    log.error("可打开 %s 在网页端手动注销", client.url("/"))
    return 2


def cmd_diagnose(args):
    """门户抓取诊断（融合自 probe/capture_probe.py，常驻监控 + 自动取证）。

    入口已完全内置到本 exe：UI 工具箱的「抓取诊断」按钮也会调到这里。
    """
    try:
        import diagnose_probe  # 根目录下的 diagnose_probe.py，打包时被 PyInstaller 静态分析捕获
    except ImportError as e:
        log.error("诊断模块缺失: %r", e)
        print("诊断模块缺失: %r" % e)
        return 1
    argv = []
    if getattr(args, "interval", None):
        argv += ["--interval", str(args.interval)]
    if getattr(args, "max_wait", None) is not None and args.max_wait:
        argv += ["--max-wait", str(args.max_wait)]
    if getattr(args, "keep", False):
        argv += ["--keep"]
    if getattr(args, "dry", 0):
        argv += ["--dry", str(args.dry)]
    return diagnose_probe.main(argv)


def cmd_install(args):
    return 0 if install_autostart() else 1


def cmd_uninstall(args):
    return 0 if uninstall_autostart() else 1


def cmd_start(args):
    """启动后台守护（等价于 UI 里的「启动守护」）"""
    return 0 if start_daemon() else 1


def cmd_stop(args):
    """停止后台守护（等价于 UI 里的「停止守护」）"""
    if not daemon_running():
        print("守护进程未在运行")
        return 0
    return 0 if stop_daemon() else 1


def cmd_ui(args):
    """启动图形管理界面（优先 HTML 版 pywebview，失败回退 tkinter）"""
    try:
        with open(os.path.join(BASE_DIR, "_ui_trace.log"), "a",
                  encoding="utf-8") as f:
            f.write("cmd_ui entered argv=%r frozen=%s\n" %
                    (sys.argv, getattr(sys, "frozen", False)))
    except Exception:
        pass
    force_utf8_console()
    hide_console()  # 双击 exe 时消除黑窗；从 cmd 前台运行时不动父控制台
    # ---- HTML UI（pywebview，Novelist 同款）优先 ----
    # 注意：desktop.main() 阻塞直到窗口关闭；仅当 pywebview 缺失或
    # WebView2 初始化失败时才快速返回/抛异常 → 回退 tkinter。
    try:
        import desktop  # 延迟导入：守护进程不需要加载 pywebview
        with open(os.path.join(BASE_DIR, "_ui_trace.log"), "a",
                  encoding="utf-8") as f:
            f.write("import desktop OK; calling desktop.main()\n")
        desktop.main()
        return 0
    except Exception as e:
        try:
            with open(os.path.join(BASE_DIR, "_ui_trace.log"), "a",
                      encoding="utf-8") as f:
                f.write("desktop HTML UI unavailable (%r) → fallback tkinter\n" % e)
        except Exception:
            pass
    # ---- tkinter 回退 ----
    try:
        import ui  # 延迟导入：守护进程不需要加载 tkinter
        with open(os.path.join(BASE_DIR, "_ui_trace.log"), "a",
                  encoding="utf-8") as f:
            f.write("import ui OK; ui.main=%r\n" % ui.main)
    except ImportError as e:
        log.error("无法加载管理界面: %r", e)
        log.error("请改用包含 tkinter 的 Python，例如：")
        log.error("  C:\\Users\\XiChen\\AppData\\Local\\Programs\\Python"
                  "\\Python314\\python.exe campusnet.py ui")
        return 1
    except Exception as e:
        # 捕获 import 期间的所有异常（含 SyntaxError 等），落盘便于排查
        import traceback
        try:
            with open(os.path.join(BASE_DIR, "_ui_trace.log"), "a",
                      encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
    try:
        return ui.main()
    except Exception:
        # --windowed 打包下崩溃是静默的（无控制台、print 丢失）；
        # 落盘 traceback 到 exe 同目录便于排查
        import traceback
        try:
            with open(os.path.join(BASE_DIR, "_ui_crash.log"), "w",
                      encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise


def cmd_test(args):
    """自检：RSA 实现 + 门户连通性"""
    print("=" * 58)
    print(" 自检")
    print("=" * 58)
    cfg = load_config()

    # 1. RSA 自检：1024-bit 已知密钥，验证加解密闭环
    exp = "10001"
    mod = ("9c2899b8ceddf9beafad2db8e431884a79fd9b9c881e459c0e1963984779d66"
           "12222cee814593cc458845bbba42b2d3474c10b9d31ed84f256c6e3a1c795e68"
           "e18585b84650076f122e763289a4bcb0de08762c3ceb591ec44d764a69817318"
           "fbce09d6ecb0364111f6f38e90dc44ca89745395a17483a778f1cc8dc990d87c3")
    ok = True
    try:
        # 用 python 侧解密反向验证：c = m^e mod n，再解 m = c^d（这里只验证正向可执行）
        c = eportal_rsa_encrypt(reverse_str("test123>000000000000"), exp, mod)
        clen = len(c)
        print("RSA 加密    : OK (密文 %d hex 字符，1024-bit 密钥预期 256)" % clen)
        if clen != 256:
            print("             ! 长度非 256，可能密钥位数不同，属正常")
    except Exception as e:
        ok = False
        print("RSA 加密    : 失败 -> %r" % e)

    # 2. 门户连通（200/302 都算通：已登录时根路径会 302 到成功页）
    client = PortalClient(cfg)
    r = http_request("GET", client.host, client.port, "/",
                     timeout=client.timeout, cookie_jar=client.cookies)
    if r.status > 0:
        loc = r.get("Location")
        print("门户连通    : OK (HTTP %s%s)" %
              (r.status, " -> %s" % loc[:60] if loc else ""))
    else:
        ok = False
        print("门户连通    : 失败 (HTTP %s)" % r.status)

    online, loc = client.check_online()
    print("当前状态    : %s" % {True: "已在线", False: "未认证",
                                None: "网络不可达"}[online])

    # 3. 登录页可用性（未认证时才有意义）
    if online is False:
        url, html = client.discover_login_page()
        if html:
            fields = parse_inputs(html)
            print("登录页获取  : OK")
            print("  URL       : %s" % url[:120])
            print(" 公钥指数   : %s" % fields.get("publicKeyExponent", "(无)"))
            print(" 公钥模数   : %s…" % str(fields.get("publicKeyModulus", ""))[:40])
            print(" 是否加密   : %s" % fields.get("passwordEncrypt", "(无)"))
        else:
            ok = False
            print("登录页获取  : 失败")
    else:
        print("登录页获取  : 跳过（当前已在线，需下线后才能看到登录页）")

    # 4. 配置
    print("配置文件    : %s" % ("存在" if os.path.exists(CONFIG_PATH) else "缺失"))
    print("凭据文件    : %s" % ("存在" if os.path.exists(CRED_PATH) else "缺失"))
    print("=" * 58)
    return 0 if ok else 1


# ============================ CLI ============================

def main():
    try:
        with open(os.path.join(BASE_DIR, "_ui_trace.log"), "a",
                  encoding="utf-8") as f:
            f.write("main() entered argv=%r frozen=%s\n" %
                    (sys.argv, getattr(sys, "frozen", False)))
    except Exception:
        pass
    force_utf8_console()
    # 注意：不做 sys.stdout.reconfigure(utf-8) 强制切换。
    # PyInstaller windowed 下 stdout 是继承自父进程句柄的 TextIOWrapper：
    #   - 真实控制台（cmd / bat）：Python PEP 528 总是以 UTF-8 写屏幕，显示正常；
    #   - 管道（PowerShell 捕获 / 重定向）：保持 locale 编码（gbk），
    #     与 cmd 5.1 / PowerShell 按 ACP 解码子进程字节的行为一致，避免 UTF-8
    #     字节被 GBK 解码成乱码（如 "门户地址" -> "闂ㄦ埛鍦板潃"）。

    parser = argparse.ArgumentParser(
        description="校园网无感认证（锐捷 ePortal）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="常用：setup -> login -> install")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("setup", help="交互式配置账号密码").set_defaults(func=cmd_setup)
    sub.add_parser("daemon", help="后台常驻守护（自动重连）").set_defaults(func=None)
    sub.add_parser("start", help="启动后台守护").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="停止后台守护").set_defaults(func=cmd_stop)
    sub.add_parser("once", help="检测一次，未认证则登录").set_defaults(func=cmd_once)
    sub.add_parser("status", help="查看状态").set_defaults(func=cmd_status)
    p_log = sub.add_parser("log", help="打印最近日志（UTF-8，防乱码）")
    p_log.add_argument("-n", "--lines", type=int, default=15,
                       help="行数（默认 15）")
    p_log.set_defaults(func=cmd_log)
    sub.add_parser("logout", help="下线").set_defaults(func=cmd_logout)
    sub.add_parser("install", help="安装开机自启").set_defaults(func=cmd_install)
    sub.add_parser("uninstall", help="移除开机自启").set_defaults(func=cmd_uninstall)
    p_diag = sub.add_parser("diagnose",
                            help="门户抓取诊断（持续监控 + 自动取证，"
                                 "原 probe/capture_probe.py 功能）")
    p_diag.add_argument("--interval", type=int, default=5,
                        help="轮询间隔（秒，默认 5）")
    p_diag.add_argument("--max-wait", type=int, default=30,
                        help="最长等待认证（分钟，0=无限，默认 30）")
    p_diag.add_argument("--keep", action="store_true",
                        help="出报告后继续监控（Ctrl+C 停止）")
    p_diag.add_argument("--dry", type=int, default=0,
                        help="只跑 N 轮就出报告（内部自测用）")
    p_diag.set_defaults(func=cmd_diagnose)
    sub.add_parser("test", help="自检").set_defaults(func=cmd_test)
    sub.add_parser("ui", help="打开图形管理界面").set_defaults(func=cmd_ui)
    p_login = sub.add_parser("login", help="立即登录")
    p_login.add_argument("--force", action="store_true", help="已在线也强制重登")
    p_login.set_defaults(func=cmd_login)

    # 无参数 = 打开管理界面（exe 双击即用；开发态 python campusnet.py 同理）
    if len(sys.argv) == 1:
        try:
            with open(os.path.join(BASE_DIR, "_ui_trace.log"), "a",
                      encoding="utf-8") as f:
                f.write("main: argv==1, dispatching to cmd_ui\n")
        except Exception:
            pass
        return cmd_ui(None)
    args = parser.parse_args()

    is_daemon = (getattr(args, "cmd", None) == "daemon")
    if not is_daemon:
        # --windowed 打包下 CLI 附加父 cmd 控制台输出；UI 模式无父控制台则空流兜底
        attach_console()

    cfg = load_config()
    setup_logger(to_console=not is_daemon, level=cfg.get("log_level", "INFO"))

    if is_daemon:
        hide_console()  # 手动从 cmd 前台运行 daemon 时共享控制台，不会误隐藏
        cred = CredentialStore().load()
        if not cfg["username"] or not cred:
            log.error("尚未配置账号密码，请先运行：python campusnet.py setup")
            return 1
        return run_daemon(cfg, cred)

    if getattr(args, "func", None):
        return args.func(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
