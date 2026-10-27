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
import hashlib
import socket
import uuid
import logging
import argparse
import shutil
import subprocess
import threading
import http.client
import urllib.parse
import urllib.request
import contextlib
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

# 应用版本（单一来源）：CLI --version、构建的 exe 版本资源、发布说明均以此为准
# 0.5.2 = 内置彻底卸载器（随界面自动释放）+ 守护停机/进程归属硬化
# 0.5.3 = 公网探测无结论时的认证阻塞与退避修复（PR#3）+ 登录页发现优先重定向（PR#2）
#         + 状态文案不再把"探测无结论"误报成"网络不可达"
# 0.6.0 = 自启/守护/界面合并为同一个 exe（不再释放 VBS 组件）+ Release 更新检查
# 0.6.1 = 运行时清理与可观测性（.old.exe 不再堆积、下线不再被守护立刻登回、
#         验证码/短信/CAS 给出结论而不是无限重试、累计计数落盘、MAC 按网卡取、
#         配置值校验、自启/升级相关日志措辞）
#         + 更新检查结果落盘 / 在线自动补查 / 界面与 CLI 透出远端版本
APP_VERSION = "0.6.1"
GITHUB_REPO = "Origin-XiChen/campus-net-auth"
GITHUB_RELEASES_URL = "https://github.com/%s/releases" % GITHUB_REPO
GITHUB_API_LATEST = ("https://api.github.com/repos/%s/releases/latest"
                     % GITHUB_REPO)
# Release 检查缓存：成功 6 小时、失败 5 分钟。只读网络，不自动下载。
UPDATE_CHECK_TTL = 6 * 3600
UPDATE_CHECK_FAIL_TTL = 300

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
    # 手动「下线」后的静默期（秒）：用户显式下线意味着"我现在不想联网"，
    # 守护在这段时间内不自动登录（仍会监控网络与心跳），避免"刚下线就被登回"。
    # 0 = 关闭；执行 login（界面「立即连接」/CLI）会立即清除静默期。
    "logout_pause": 600,
    # 连通性检测目标（HTTP，避免 HTTPS 证书干扰）
    "detect_targets": [
        {"host": "www.msftconnecttest.com", "path": "/connecttest.txt",
         "expect": "Microsoft Connect Test"},
        {"host": "captive.apple.com", "path": "/hotspot-detect.html",
         "expect": "Success"},
        {"host": "www.baidu.com", "path": "/", "expect": ""},
    ],
    # 登录页兜底参数：未认证时若网关不做 302 劫持（DNS 直接黑洞）、门户根路径
    # 又不给登录页，就只能按门户登记的"认证设备参数"自己拼出登录页 URL。
    # 取值：浏览器登录一次，把地址栏 index.jsp? 后面的参数抄过来
    # （wlanacname / nasip 必须与 SAM+ 里登记的 portal 设备一致，否则门户只会
    # 回"WEB认证设备未注册"）。wlanuserip / wlanparameter 留空则用本机
    # 当前 IPv4 与 MAC 自动填充。整项置空 {} 即关闭该兜底。
    "login_page_hints": {
        "wlanacname": "logic",
        "nasip": "10.253.0.17",
        "url": "",
    },
    # 门户公钥 pinning（TOFU）：首次成功登录记录 RSA 公钥指纹到 state.json，
    # 之后指纹不一致即拒发凭据，防伪装门户/同 IP 中间人窃密。
    # 学校更换密钥后需删除 state.json 的 portal_pubkey_fp 字段重新锚定。
    "pin_pubkey": True,
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


def looks_like_eportal_login(html):
    """粗略判定一份 HTML 是否为锐捷 ePortal 的**学生**登录页。

    为什么不能只看“有 pwd 字段”：门户根目录 `/eportal/` 也挂着一份
    后台管理登录页（form action=./admin.do?method=login，同样有 pwd 与
    publicKey），误认成登录页后会拿学生密码去提交管理台接口。这里要求
    页面上真的存在登录表单的输入域，且不是管理台表单。
    """
    if not html:
        return False
    # 管理台登录表单（/eportal/ 上那份）：form action 指向 admin.do
    if re.search(r'action\s*=\s*["\'][^"\']*admin\.do', html, re.I):
        return False
    if "publickeyexponent" in html.lower():
        return True
    return bool(re.search(r'[\s"\']id\s*=\s*["\']pwd["\']', html, re.I))


def local_lan_ipv4(host):
    """本机面向门户的源 IPv4（拼 wlanuserip 用）；拿不到返回 ""。

    UDP connect 不发包，只让内核查路由选源地址；校外/无网时很快失败。"""
    if not host:
        return ""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.connect((host, 80))
        ip = sock.getsockname()[0]
        return ip if ip and not ip.startswith("0.") else ""
    except (OSError, ValueError):
        return ""
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class _IP_ADDR_STRING(ctypes.Structure):
    pass


_IP_ADDR_STRING._fields_ = [
    ("Next", ctypes.POINTER(_IP_ADDR_STRING)),
    ("IpAddress", ctypes.c_char * 16),
    ("IpMask", ctypes.c_char * 16),
    ("Context", wintypes.DWORD),
]


class _IP_ADAPTER_INFO(ctypes.Structure):
    pass


# 只声明到 IpAddressList 为止：GetAdaptersInfo 写进传入缓冲区，
# 我们靠 Next 指针遍历，不读后面的字段，所以不需要完整结构体。
_IP_ADAPTER_INFO._fields_ = [
    ("Next", ctypes.POINTER(_IP_ADAPTER_INFO)),
    ("ComboIndex", wintypes.DWORD),
    ("AdapterName", ctypes.c_char * 260),
    ("Description", ctypes.c_char * 132),
    ("AddressLength", ctypes.c_uint),
    ("Address", ctypes.c_ubyte * 8),
    ("Index", wintypes.DWORD),
    ("Type", ctypes.c_uint),
    ("DhcpEnabled", ctypes.c_uint),
    ("CurrentIpAddress", ctypes.POINTER(_IP_ADDR_STRING)),
    ("IpAddressList", _IP_ADDR_STRING),
]

_ERROR_BUFFER_OVERFLOW = 111


def _enum_ipv4_ifaces():
    """枚举本机 IPv4 网卡 → [(ip, "aa-bb-cc-dd-ee-ff")]。

    用 GetAdaptersInfo（iphlpapi，零依赖、IPv4-only 刚好是我们要的）：
    无需管理员权限，也不用 COM/WMI。任何异常一律返回 []，调用方回退。
    """
    if os.name != "nt":
        return []
    out = []
    try:
        size = wintypes.ULONG(16 * 1024)
        buf = ctypes.create_string_buffer(size.value)
        fn = ctypes.windll.iphlpapi.GetAdaptersInfo
        rc = fn(ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_INFO)),
                ctypes.byref(size))
        if rc == _ERROR_BUFFER_OVERFLOW:
            buf = ctypes.create_string_buffer(size.value)
            rc = fn(ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_INFO)),
                    ctypes.byref(size))
        if rc != 0:
            return []
        node = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_INFO))
        guard = 0
        while node and guard < 64:
            guard += 1
            info = node.contents
            n = int(info.AddressLength)
            mac = ""
            if n >= 6:
                mac = "-".join("%02x" % info.Address[i] for i in range(6))
            ip = info.IpAddressList.IpAddress.decode("ascii", "ignore").strip()
            # 全 0 的 MAC/地址是未连接的虚拟网卡，跳过
            if (mac and mac != "00-00-00-00-00-00"
                    and ip and ip != "0.0.0.0"):
                out.append((ip, mac))
            node = info.Next
    except Exception:
        return []
    return out


def pick_mac_for_ip(ifaces, ip):
    """从 [(ip, mac)] 里挑出与 ip 完全一致的那张网卡的 MAC（纯函数）。

    单独拆出来是为了可单测：ctypes 枚举部分在无网卡/被拦截的环境里只能
    返回空列表，真正的选择规则不该依赖那个环境。
    """
    if not ip:
        return ""
    for entry_ip, mac in ifaces or ():
        if entry_ip == ip and mac:
            return mac
    return ""


def local_mac_for_ip(ip):
    """面向某块网卡（由本机 IPv4 指定）的 MAC；取不到返回 ""。"""
    return pick_mac_for_ip(_enum_ipv4_ifaces(), ip)


def local_mac_text():
    """本机 MAC 的**兜底**来源（aa-bb-cc-dd-ee-ff）。

    uuid.getnode() 取的是“第一块网卡”，可能根本不是走校园网的那块（实测：
    返回 74-3a-f4-ca-f3-ec，而 Wi-Fi 网卡是 74-3a-f4-ca-f3-eb）。因此它只能
    当候选：优先用 local_mac_for_ip（按本机 IPv4 反查同一块网卡）。
    """
    try:
        node = uuid.getnode()
    except Exception:
        return ""
    if (node >> 40) % 2:        # 组播位为 1 → uuid 给的随机数，不是真 MAC
        return ""
    return "-".join("%02x" % ((node >> (8 * i)) & 0xff) for i in range(5, -1, -1))


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
          online=None  : 公网探测无结论，不代表校园网门户不可达
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
        # 透明代理/劫持网关会把任何 host 都回 200：只有带严格 expect 的目标
        # 才有"已在线"决定权，空 expect 目标只证明"网络可达"
        strict = any(t.get("expect") for t in targets)
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
                if expect and expect in body:
                    return True, None
                if not expect and not strict:
                    # 兜底：配置里没有任何严格 expect 目标时保留旧行为
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
    def login_page_query(self):
        """按 login_page_hints 与本机地址拼出登录页 queryString。

        锐捷 ePortal 只在请求带了"认证设备参数"时才下发学生登录页：
        不带参数的 `/eportal/index.jsp` 只会回一句
        `alert('WEB认证设备未注册…')`，根路径可能只给成功页或管理台。
        正常流程里这些参数来自网关 302；网关不做劫持时（例如未认证阶段
        DNS 直接被黑洞），就只能按这里登记的 wlanacname / nasip 自己拼。
        缺关键参数时返回 ""（不拼、不乱猜）。
        """
        hints = self.cfg.get("login_page_hints") or {}
        if not isinstance(hints, dict):
            return ""
        nasip = str(hints.get("nasip") or "").strip()
        acname = str(hints.get("wlanacname") or "").strip()
        if not nasip or not acname:
            return ""
        ip = (str(hints.get("wlanuserip") or "").strip()
              or local_lan_ipv4(self.host))
        if not ip:
            return ""
        mac = (str(hints.get("wlanparameter") or "").strip()
               or local_mac_for_ip(ip)     # 走校园网那块网卡的 MAC（首选）
               or local_mac_text())        # 兜底：uuid.getnode()
        fields = [("wlanuserip", ip), ("wlanacname", acname),
                  ("nasip", nasip)]
        if mac:
            fields.append(("wlanparameter", mac))
        fields.append(("url", str(hints.get("url") or "")))
        return urllib.parse.urlencode(fields)

    def discover_login_page(self, detection=None):
        """
        找到真正的登录页 URL 并返回 (url, html)。
        未认证时，访问任意外网会被网关重定向到 portal 登录页，
        该 URL 的 query 即为后续登录所需的 queryString。
        detection 只复用本次探测结果，避免同一尝试再次探测。
        """
        online, location = self.check_online() if detection is None else detection
        if online:
            return None, None

        seen = set()

        def read_page(candidate, response=None, base_url=None):
            try:
                parsed = urllib.parse.urlparse(
                    urllib.parse.urljoin(base_url or self.url("/"), candidate))
                if (parsed.scheme != "http"
                        or parsed.hostname != self.host.lower()
                        or (parsed.port or 80) != self.port
                        or parsed.username is not None
                        or parsed.password is not None):
                    return None, None
            except (TypeError, ValueError):
                return None, None
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            url = self.url(path)
            if url in seen:
                return None, None
            seen.add(url)
            if response is None:
                response = http_request("GET", self.host, self.port, path,
                                        timeout=self.timeout, cookie_jar=self.cookies)
            if response.status == 200:
                html = response.text()
                if looks_like_eportal_login(html):
                    return url, html
            return None, None

        if location:
            page = read_page(location)
            if page[1]:
                return page

        portal_answered = False
        for p in ("/", self.base + "/index.jsp", self.base + "/"):
            if self.url(p) in seen:
                continue
            r = http_request("GET", self.host, self.port, p,
                             timeout=self.timeout, cookie_jar=self.cookies)
            portal_answered = portal_answered or r.status > 0
            page = read_page(self.url(p), r)
            if page[1]:
                return page
            loc = None
            if r.status in (301, 302, 303, 307, 308):
                loc = r.get("Location")
            elif r.status == 200:
                match = re.search(r'(?:location|href)\s*=\s*["\']([^"\']+)["\']',
                                  r.text(), re.I)
                if match:
                    loc = match.group(1)
            if loc:
                page = read_page(loc, base_url=self.url(p))
                if page[1]:
                    return page

        # ---- 兜底：网关不做 302 劫持时，自己按门户设备参数拼登录页 URL ----
        # 校外网络门户本来就不可达，这里不再追加请求、也不刷日志。
        query = self.login_page_query()
        hints = self.cfg.get("login_page_hints")
        if query:
            path = "%s/index.jsp?%s" % (self.base, query)
            resp = http_request("GET", self.host, self.port, path,
                                timeout=self.timeout, cookie_jar=self.cookies)
            page = read_page(self.url(path), resp)
            if page[1]:
                return page
            if resp.status > 0:
                log.info("登录页兜底无效：按 login_page_hints 拼出的地址没返回"
                         "登录页（核对 wlanacname / nasip 是否与 SAM+ 登记的"
                         " portal 设备一致）")
        elif portal_answered:
            if isinstance(hints, dict) and hints:
                log.info("登录页兜底未生效：login_page_hints 缺 nasip /"
                         " wlanacname（或本机取不到 IPv4）")
            else:
                log.info("未找到可用登录页：网关没给出重定向，门户根路径也不给"
                         "登录页。可把浏览器登录时地址栏 index.jsp? 后面的参数"
                         "填进 config.json 的 login_page_hints")
        return None, None

    def check_authentication(self):
        """返回 (online, 本轮登录页上下文)，仅在公网探测未知时复核门户。"""
        online, location = self.check_online()
        if online is not None:
            return online, None
        info = self.interface("getOnlineUserInfo")
        if (isinstance(info, dict) and info.get("userIndex")
                and str(info.get("result", "success")).lower() == "success"):
            return True, None
        try:
            url, html = self.discover_login_page(detection=(online, location))
            parsed = urllib.parse.urlparse(url)
            if (not html or not parsed.query
                    or parsed.scheme != "http"
                    or parsed.hostname != self.host.lower()
                    or (parsed.port or 80) != self.port
                    or parsed.username is not None
                    or parsed.password is not None):
                return None, None
        except (TypeError, ValueError):
            return None, None
        fields = parse_inputs(html)
        encrypt_flag = str(fields.get("passwordEncrypt", "")).strip().lower()
        if encrypt_flag == "true":
            if not (fields.get("publicKeyExponent")
                    and fields.get("publicKeyModulus")):
                return None, None
        elif encrypt_flag != "false" or "pwd" not in fields:
            return None, None
        return False, (url, html)

    # ---------- 登录 ----------
    def login(self, username, password, service_hint=None, login_page=None):
        """login_page 仅复用本次已验证页面，不跨次保存。"""
        url, html = self.discover_login_page() if login_page is None else login_page
        if not html:
            return {"result": "fail",
                    "message": "未能获取登录页（已在线？或不在校园网环境）"}

        parsed = urllib.parse.urlparse(url)
        query_string = parsed.query
        qs_params = urllib.parse.parse_qs(query_string)
        # 门户页面 login_bch.js 的口径：queryString 里没有 mac 时用 111111111 参与加密
        # （未认证劫持拿到的 URL 往往只带 wlanparameter，没有 mac）
        mac = (qs_params.get("mac") or [""])[0] or "111111111"

        fields = parse_inputs(html)
        exp = fields.get("publicKeyExponent", "")
        mod = fields.get("publicKeyModulus", "")
        encrypt_flag = fields.get("passwordEncrypt", "false")
        use_encrypt = str(encrypt_flag).lower() == "true"

        # 有些门户（锐捷 ePortal 新版登录页）把 passwordEncrypt / RSA 公钥都留空，
        # 真值由页面 JS 从 pageInfo 拉下来再填进隐藏域。只看 HTML 会误判成
        # "不加密"而提交明文密码，这里按页面同样的方式补齐。
        page_detail = None
        if not use_encrypt or not (exp and mod):
            page_detail = self.page_info(query_string)
            if isinstance(page_detail, dict):
                p_exp = str(page_detail.get("publicKeyExponent") or "").strip()
                p_mod = str(page_detail.get("publicKeyModulus") or "").strip()
                if (str(page_detail.get("passwordEncrypt", "")).lower() == "true"
                        and p_exp and p_mod):
                    use_encrypt, exp, mod = True, p_exp, p_mod

        # ---- 门户公钥 pinning（TOFU，pin_pubkey 可关）----
        # 威胁模型：门户身份只靠"私网 IP 可达"判定，任何能在该 IP 应答的设备
        # （伪装 AP / 同网段攻击者）都能下发自己的 RSA 公钥，收到密文后用自有
        # 私钥解开 → 凭据被盗。缓解：首次成功登录时把公钥指纹记入 state.json，
        # 之后指纹不一致（或已记录指纹但页面突然不加密）一律拒发凭据。
        # 已知局限：TOFU 首登窗口无法防御（首登即在伪造网络时仍会中招）；
        # 学校更换密钥时会拒绝登录，按报错提示删除 state.json 的
        # portal_pubkey_fp 字段（或把 pin_pubkey 置 false）即可恢复。
        pin_on = bool(self.cfg.get("pin_pubkey", True))
        fp = None
        if pin_on:
            pinned = load_state().get("portal_pubkey_fp")
            if not use_encrypt:
                if pinned:
                    return {"result": "fail",
                            "_hint": "门户公钥校验失败",
                            "message": "门户登录页未启用加密，与首次记录不符"
                                       "（疑似伪装门户）。如确认学校变更，请删除"
                                       " state.json 的 portal_pubkey_fp 字段后重试，"
                                       "或将 pin_pubkey 设为 false"}
            elif exp and mod:
                fp = hashlib.sha256(
                    (exp.strip().lower() + ":" + mod.strip().lower())
                    .encode("ascii")).hexdigest()
                if pinned and pinned != fp:
                    return {"result": "fail",
                            "_hint": "门户公钥指纹变化",
                            "message": "门户 RSA 公钥指纹与首次记录不一致"
                                       "（疑似伪装门户/中间人）。如确认学校更换密钥，"
                                       "删除 state.json 的 portal_pubkey_fp 字段后重试，"
                                       "或将 pin_pubkey 设为 false"}

        # 服务选择
        service = service_hint or self.cfg.get("service", "default")
        if service == "auto":
            service = self._auto_service(query_string, info=page_detail)

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
            # TOFU：首次成功登录后落盘公钥指纹（仅在成功路径记录，
            # 避免把"失败场景下看到的公钥"误当成基准）
            if pin_on and fp and not load_state().get("portal_pubkey_fp"):
                try:
                    update_state({"portal_pubkey_fp": fp})
                except Exception:
                    pass
        else:
            msg = res.get("message", "")
            for code, hint in ERR_HINT.items():
                if code in msg:
                    res["_hint"] = hint
                    break
            blocked = self.auth_blocked_reason(query_string)
            if blocked:
                res["_blocked"] = blocked
                res.setdefault("_hint", blocked)
        return res

    def auth_blocked_reason(self, query_string=""):
        """门户口径上“账号密码自动认证不可能成功”的原因；可自动认证返回 ""。

        只会在这条路径（登录已失败）里问一次 pageInfo，正常登录不多打请求。
        三个来源都是门户自己 pageInfo 里的开关：
          * isToCasPage   ：已改成 CAS/统一身份认证（跳转登录页）
          * isCheckSmsAuth：登录要短信二次验证
          * validCodeUrl  ：登录页带验证码（JS 里 validCodeUrl 非空才显示）
        """
        info = self.page_info(query_string)
        if not isinstance(info, dict):
            return ""
        if str(info.get("isToCasPage", "")).lower() == "true":
            return "门户已改为 CAS/统一身份认证登录，无法用账号密码自动认证"
        if str(info.get("isCheckSmsAuth", "")).lower() == "true":
            return "门户要求短信二次验证，无法自动认证"
        if str(info.get("validCodeUrl") or "").strip():
            return "门户登录页已启用验证码，无法自动认证"
        return ""

    def _auto_service(self, query_string="", info=None):
        info = (self.page_info(query_string) if info is None else info) or {}
        if not isinstance(info, dict):
            info = {}
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

def _sanitize_config(cfg):
    """配置类型/范围兜底：坏值回退默认或钳到合法区间，
    避免守护循环被非法配置拖死（如 detect_targets 被写成字符串、
    interval 为负数导致 select 抛错空转等）。"""
    for k in ("portal_port", "interval", "retry_interval", "max_interval",
              "offline_interval", "timeout", "probe_timeout",
              "keepalive_interval", "verify_interval", "recheck_interval",
              "logout_pause"):
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CONFIG[k]
    cfg["interval"] = max(5, cfg["interval"])
    cfg["retry_interval"] = max(5, cfg["retry_interval"])
    cfg["max_interval"] = max(60, cfg["max_interval"])
    cfg["offline_interval"] = max(5, cfg["offline_interval"])
    cfg["timeout"] = min(max(2, cfg["timeout"]), 60)
    cfg["probe_timeout"] = min(max(1, cfg["probe_timeout"]), 30)
    cfg["keepalive_interval"] = max(30, cfg["keepalive_interval"])
    cfg["verify_interval"] = max(2, cfg["verify_interval"])
    cfg["recheck_interval"] = max(5, cfg["recheck_interval"])
    # 0 = 关闭静默期；上限 24h，防止手写配置把守护“永久禁用”
    cfg["logout_pause"] = min(max(0, cfg["logout_pause"]), 86400)
    targets = cfg.get("detect_targets")
    if not isinstance(targets, list):
        targets = None  # 非列表（如字符串 "x" 是 truthy）一律按缺失处理
    else:
        targets = [t for t in targets
                   if isinstance(t, dict) and t.get("host")]
    if not targets:
        cfg["detect_targets"] = json.loads(
            json.dumps(DEFAULT_CONFIG["detect_targets"]))
    if not isinstance(cfg.get("portal_host"), str) or not cfg["portal_host"]:
        cfg["portal_host"] = DEFAULT_CONFIG["portal_host"]
    # 账号：去空白，非字符串一律置空（避免 None 拼进登录表单）
    user = cfg.get("username")
    cfg["username"] = user.strip() if isinstance(user, str) else ""
    # 服务名：只认门户口径的四个值（大小写不敏感），写错就回落 default 而不是
    # 原样 POST 给门户（写错时门户只会回一句“未知服务”，很难自査）。
    svc = cfg.get("service")
    svc = svc.strip() if isinstance(svc, str) else ""
    if svc.lower() in ("default", "auto"):
        cfg["service"] = svc.lower()
    elif svc.upper() in ("DX", "LT", "YD"):
        cfg["service"] = svc.upper()
    else:
        if svc:
            log.warning("配置里的 service=%r 不是合法值，已回落 default"
                        "（可选：default / DX / LT / YD / auto）", svc)
        cfg["service"] = "default"
    # 接口前缀：必须以 / 开头、不以 / 结尾（否则 url() 会拼出
    # http://host:80eportal/... 这种发不出去的地址，而调用方看不出原因）
    base = cfg.get("portal_base")
    base = base.strip() if isinstance(base, str) else ""
    if not base.startswith("/"):
        base = "/" + base
    cfg["portal_base"] = base.rstrip("/") or "/eportal"
    # 登录页兜底参数：只保留字符串型的已知键，其余丢弃，避免脏配置拼出怪 URL。
    # 非字典或空字典（含 false / null / {}）→ 关闭兜底；部分给出时按默认值补齐；
    # wlanuserip / wlanparameter 留空表示"用本机当前地址自动填充"。
    hints = cfg.get("login_page_hints")
    if not isinstance(hints, dict) or not hints:
        cfg["login_page_hints"] = {}
    else:
        merged = dict(DEFAULT_CONFIG["login_page_hints"])
        merged.update({k: v for k, v in hints.items()
                       if isinstance(v, (str, int, float))})
        cfg["login_page_hints"] = {
            k: str(merged.get(k) or "").strip()
            for k in ("wlanacname", "nasip", "wlanuserip",
                      "wlanparameter", "url")
        }
    # 布尔项容错（用户手写 JSON 可能写成字符串 "false"）
    v = cfg.get("pin_pubkey", True)
    cfg["pin_pubkey"] = v if isinstance(v, bool) else True
    return cfg


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except Exception as e:
            log.warning("配置文件读取失败，使用默认配置: %r", e)
    return _sanitize_config(cfg)


def _atomic_write_json(path, obj):
    """原子写 JSON：先写临时文件（flush+fsync）再 os.replace 换位。
    进程被杀/断电也只会留下完整旧文件或完整新文件，不会半截截断。
    （历史隐患：config.json 截断 → load_config 回退默认丢账号；
    state.json 截断 → load_state 返回 {} 丢 events/first_run_at。）"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_config(cfg):
    try:
        _atomic_write_json(CONFIG_PATH, cfg)
    except Exception as e:
        log.error("保存配置失败: %r", e)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


@contextlib.contextmanager
def _state_lock(timeout=3.0):
    """跨进程文件锁（O_EXCL 锁文件）：守护与 UI 进程都会写 state.json，
    保护"读-改-写"不被并发冲掉（丢 events/userIndex）。
    超时或创建失败降级为不加锁（fail-open，防锁文件残留把写永久卡死）。"""
    lockp = STATE_PATH + ".lock"
    fh = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fh = os.open(lockp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield
    finally:
        if fh is not None:
            try:
                os.close(fh)
            except OSError:
                pass
            try:
                os.remove(lockp)
            except OSError:
                pass


def save_state(state):
    """整体覆盖写 state.json（原子写，见 _atomic_write_json）。

    ⚠️ 除非你就是要删字段（如 clear_session），否则一律用 update_state()
    合并写——整体替换会丢掉 first_run_at（首跑时间戳）与 events（最近事件条）
    等由其它模块维护的字段。历史 bug：tkinter 回退 UI 登录成功时整写，
    把守护记录的 events 清空（界面"最近事件"条随之消失）。
    注：全新部署判定 first_deploy() 只看 state.json 文件是否存在、不看内容，
    所以整写不会让"空文件夹提醒"重现；但字段丢失本身就是 bug，必须合并写。
    """
    try:
        _atomic_write_json(STATE_PATH, state)
    except Exception:
        pass


def update_state(patch, drop=()):
    """合并式更新 state.json：保留其它字段（如 first_run_at 部署标记、
    守护写入的 userIndex 等），避免整字典覆盖互相冲掉。
    drop 为需删除的字段名元组（如登录成功后清除 last_error）。
    读-改-写全程持 _state_lock，防守护与 UI 并发互踩。"""
    with _state_lock():
        st = load_state()
        for k in drop:
            st.pop(k, None)
        st.update(patch)
        save_state(st)


def clear_session():
    """下线/重置时清除会话字段（userIndex/loginTime/last_error），
    保留 first_run_at 等部署标记，避免重新触发全新部署判定。"""
    with _state_lock():
        st = load_state()
        for k in ("userIndex", "loginTime", "last_error"):
            st.pop(k, None)
        save_state(st)


# ---- “手动下线”静默期 ----
# 用户显式下线（界面「下线」/CLI logout）后，守护不得马上把他登回去：
# 旧行为是 30s 一轮发现未认证就重登（实测 0.5.4/0.6.0 均 14~36s），
# “下线”按钮形同虚设。静默期存 state.json（跨守护重启生效），
# 执行 login（界面「立即连接」/CLI）会立即清除。
LOGOUT_PAUSE_KEY = "logout_pause_until"


def set_logout_pause(seconds):
    """设置/清除手动下线静默期（秒）。seconds<=0 表示清除。"""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        with _state_lock():
            st = load_state()
            if st.pop(LOGOUT_PAUSE_KEY, None) is not None:
                save_state(st)
        return 0
    until = int(time.time()) + seconds
    update_state({LOGOUT_PAUSE_KEY: until})
    return until


def logout_pause_left():
    """手动下线静默期剩余秒数（已过期/未设置返回 0）。"""
    try:
        until = int(load_state().get(LOGOUT_PAUSE_KEY) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, until - int(time.time()))


def clear_logout_pause():
    """解除手动下线静默期（用户显式要求联网时调用）。返回是否真的清过。

    所有"用户主动要联网"的入口都要走这里：CLI login、界面「立即连接」、
    tkinter 回退界面的登录按钮。少了任何一处，用户下线后手动连上、随后一
    掉线，守护就会因为静默期还在而不自动重连（最多 10 分钟）。
    """
    if not logout_pause_left():
        return False
    set_logout_pause(0)
    log.info("已解除手动下线静默期")
    return True


# ---- 跨进程累计计数（守护重启不归零）----
_STATS_WRITE_AT = 0.0


def save_stats_totals(totals, min_gap=60.0):
    """把“跨进程累计计数”写进 state.json；默认 60s 内最多写一次。

    为什么需要：`stats` 是进程内字典，守护每重启一次就归零，心跳里的
    “累计登录成功=0”会让人误以为从来没认证过（真实排查中误导过两次）。
    限流是为了登录失败风暴时不把磁盘写爆。
    """
    global _STATS_WRITE_AT
    now = time.time()
    if min_gap > 0 and now - _STATS_WRITE_AT < min_gap:
        return False
    _STATS_WRITE_AT = now
    clean = {}
    for k, v in (totals or {}).items():
        try:
            clean[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    update_state({"stats": clean})
    return True


# 状态事件类型 → 展示文案（供 UI 即时状态条复用）
EVENT_LABEL = {
    "lost": "掉线重连",
    "restored": "恢复在线",
    # "offline" 现在同时覆盖"真不在校园网"与"公网探测无结论"两种情形，
    # 这里用中性词；具体原因由事件自带的 msg 说明。
    "offline": "网络异常",
    "login_ok": "认证成功",
    "login_fail": "认证失败",
    "kickout": "会话失效",
}


def network_state_text(online, portal=None):
    """把 (公网探测结果, 门户可达性) 翻成不误导的状态文案。

    ⚠️ check_online() 的 None 只是"公网探测没有任何结论"（例如未认证时 DNS
    被挡、三个探测目标全部传输层失败），**不等于"网络不可达"**：校园门户是
    裸 IP，此时往往仍然可达。旧文案把 None 一律写成"网络不可达"，Issue #1
    里那句反复出现的误报就是它；而 issue 报告者实际能手动认证成功，也说明
    门户是好的。

    portal 为 None 表示"没有探测门户可达性"（只给出保守措辞）。
    文案为单一事实来源，供 cmd_status / cmd_test / 界面状态卡复用。
    """
    if online is True:
        return "已认证/可上网"
    if online is False:
        return "未认证（被门户劫持）"
    if portal is True:
        return "公网探测无结论（门户可达）"
    if portal is False:
        return "不在校园网（门户不可达）"
    return "公网探测无结论"


def record_event(kind, msg):
    """记录最近一次状态事件（掉线/重连/恢复/登录结果），写 state.json
    events 环形缓冲（保留最近 8 条），UI 轮询 /api/status 时即时展示。

    去重规则：与最后一条同 kind 且同 msg 且在 60s 内 → 只更新时间戳，
    防止"连续登录失败"这类事件风暴刷屏。其余场景（状态翻转/登录结果）
    天然低频，不会给 state.json 造成写盘压力。"""
    with _state_lock():
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


# ============================ 系统通知（右下角） ============================
# 守护是无人值守进程，用户不会盯着日志；关键异常（如门户认证异常自动修复）
# 需要让用户"看得见"一次。这里用托盘气球通知（Win10/11 自动转为右下角
# 系统通知并进入通知中心），纯 ctypes 实现、零第三方依赖、零子进程。

NOTIFY_AUMID = "CampusNetAuth.Tray"
_NOTIFY_FLAG = {"aumid_set": False, "lock": None}
_NOTIFY_CLS = {"atom": 0, "cls": None, "proc": None}
# 同标题+正文的通知节流（秒）：避免异常持续时刷屏
NOTIFY_DEDUP_SEC = 60
_notify_last = {}


def _ensure_notify_aumid():
    """设置进程级 AppUserModelID —— Win10 1607+ / Win11 的硬性前提。

    没有 AUMID 的进程调用 Shell_NotifyIcon 发气球通知会被系统**静默丢弃**
    （既不显示、也不进通知中心，API 却返回成功），这是本项目踩过的坑。
    同一进程只需设置一次；失败不抛异常（退化为"通知不可见"，不影响认证）。
    """
    if _NOTIFY_FLAG["aumid_set"] or not sys.platform.startswith("win"):
        return
    try:
        if _NOTIFY_FLAG["lock"] is None:
            _NOTIFY_FLAG["lock"] = 1
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(NOTIFY_AUMID))
        _NOTIFY_FLAG["aumid_set"] = True
    except Exception:  # noqa: BLE001
        pass


def _notify_worker(title, message, timeout=10):
    """通知工作线程：注册消息窗口 → 加托盘图标 → 弹通知 → 延时清理。

    全部包在 try 里：任何一步失败都静默退出（通知只是锦上添花，绝不能
    因为托盘 API 异常影响守护循环或留下残留图标/窗口）。
    """
    _ensure_notify_aumid()
    try:
        import ctypes.wintypes as _wt
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x0, 0x1, 0x2
        NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
        NIIF_INFO = 0x1
        IDI_INFORMATION = 32516
        WM_USER, WM_DESTROY = 0x0400, 0x0002
        HWND_MESSAGE = _wt.HWND(-3)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, _wt.HWND, _wt.UINT,
                                     _wt.WPARAM, _wt.LPARAM)

        class _WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", _wt.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", _wt.HINSTANCE), ("hIcon", _wt.HICON),
                ("hCursor", _wt.HANDLE), ("hbrBackground", _wt.HBRUSH),
                ("lpszMenuName", _wt.LPCWSTR), ("lpszClassName", _wt.LPCWSTR),
            ]

        class _NID(ctypes.Structure):
            _fields_ = [
                ("cbSize", _wt.DWORD), ("hWnd", _wt.HWND),
                ("uID", _wt.UINT), ("uFlags", _wt.UINT),
                ("uCallbackMessage", _wt.UINT), ("hIcon", _wt.HICON),
                ("szTip", _wt.WCHAR * 128), ("dwState", _wt.DWORD),
                ("dwStateMask", _wt.DWORD), ("szInfo", _wt.WCHAR * 256),
                ("uTimeout", _wt.UINT), ("szInfoTitle", _wt.WCHAR * 64),
                ("dwInfoFlags", _wt.DWORD), ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", _wt.HICON),
            ]

        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [_wt.HWND, _wt.UINT, _wt.WPARAM,
                                          _wt.LPARAM]

        def _proc(hwnd, msg, wparam, lparam):
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        cb = WNDPROC(_proc)
        hinst = kernel32.GetModuleHandleW(None)
        cls = "CampusNetAuthNotify"
        wc = _WNDCLASSW()
        wc.lpfnWndProc = cb
        wc.hInstance = hinst
        wc.lpszClassName = cls
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return
        hwnd = user32.CreateWindowExW(0, cls, cls, 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, hinst, None)
        if not hwnd:
            return

        nid = _NID()
        nid.cbSize = ctypes.sizeof(_NID)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE
        nid.uCallbackMessage = WM_USER + 1
        nid.hIcon = user32.LoadIconW(None, _wt.LPCWSTR(IDI_INFORMATION))
        nid.szTip = APP_NAME
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            user32.DestroyWindow(hwnd)
            return
        try:
            nid.uFlags = NIF_INFO
            nid.dwInfoFlags = NIIF_INFO
            nid.uTimeout = max(5, int(timeout)) * 1000
            nid.szInfoTitle = str(title)[:63]
            nid.szInfo = str(message)[:255]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            # 保持图标 + 消息泵存活，确保通知送达并被系统登记
            msg = _wt.MSG()
            t0 = time.time()
            while time.time() - t0 < max(3, int(timeout)) + 2:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.05)
        finally:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.DestroyWindow(hwnd)
    except Exception as e:  # noqa: BLE001
        log.debug("系统通知发送失败（已忽略）: %r", e)


def logout_stale_session(client, user_index, username, password):
    """主动下线，用于清理门户侧的"残留会话"。

    背景：学校门户偶发异常时会出现"登录返回 success、但会话并未真正生效"
    （账号页有效期可能显示 1970 之类的异常值）。此时上一次的失效会话仍挂在
    门户上，直接重登会被当作"已在线"而无效，必须先把残留会话注销掉。

    返回 (ok, detail)：ok 表示门户确认下线；任何异常都被吞掉（返回 False），
    绝不让清理动作影响到守护循环。
    """
    try:
        r = client.logout(user_index, force_by_cred=True,
                          username=username, password=password)
        ok = (isinstance(r, dict)
              and str(r.get("result", "")).lower() == "success")
        return ok, safe_message(r)
    except Exception as e:  # noqa: BLE001
        return False, "下线异常: %r" % e


def notify_system(title, message, timeout=10, dedup=True):
    """右下角系统通知（后台线程执行，立即返回，绝不阻塞守护循环）。

    dedup=True 时，同标题+正文在 NOTIFY_DEDUP_SEC 内只弹一次，
    避免"异常持续期间"刷屏。返回 True 表示已派发（不代表一定能显示）。
    """
    if not sys.platform.startswith("win"):
        return False
    key = (str(title), str(message))
    if dedup:
        now = time.time()
        if now - _notify_last.get(key, 0) < NOTIFY_DEDUP_SEC:
            return False
        _notify_last[key] = now
        if len(_notify_last) > 32:  # 防无界增长
            for k in list(_notify_last)[:-16]:
                _notify_last.pop(k, None)
    try:
        t = threading.Thread(target=_notify_worker,
                             args=(title, message, timeout), daemon=True)
        t.start()
        log.info("已发送系统通知：%s - %s", title, message)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("系统通知线程启动失败（已忽略）: %r", e)
        return False


def notify_daemon_test(timeout=3.0):
    """让「守护进程」发一条测试通知 —— 验证"界面不在"时的通知通道。

    为什么需要单独测：通知是**按进程**发的（AUMID、托盘图标都挂在发出进程
    上），界面能弹不代表无人值守的守护也能弹。这里通过守护控制通道发
    NOTIFY 指令，由守护在自己的进程里调用 notify_system 并回执 "OK"。

    返回 (ok, detail)。
    """
    try:
        s = socket.create_connection(("127.0.0.1", SINGLETON_PORT),
                                     timeout=timeout)
    except OSError as e:
        return False, "守护未运行（控制通道不可用：%s）" % e.__class__.__name__
    try:
        s.sendall(b"NOTIFY")
        try:
            ack = s.recv(16).decode("utf-8", "replace").strip()
        except OSError:
            return False, "守护未回执（可能正忙于本轮探测，请稍后重试）"
        if ack.upper() == "OK":
            return True, "守护已发送测试通知"
        return False, "守护回执异常：%r" % ack
    except OSError as e:
        return False, "指令发送失败：%s" % e.__class__.__name__
    finally:
        try:
            s.close()
        except OSError:
            pass


# ================= 守护版本握手 / 陈旧检测 / 就地升级 =================
# 为什么需要这套机制：守护不是独立程序，而是"同一个 exe + daemon 参数"。
# 用户升级时只换 exe 文件，**已经在跑的守护进程**仍然是旧版本的代码镜像，
# 新功能（残留会话自愈、系统通知等）不会生效——而用户不会知道要去手动
# 重启/卸载重装组件。
#
# 更硬的一层：Windows 下**运行中的 exe 文件是锁定的**（实测覆写报
# WinError 32），用户连"把新 exe 拖进文件夹"这一步都会失败。但实测
# **重命名是允许的**，所以本模块的升级助手用「旧 exe 改名让位 → 新 exe
# 就位」的两步换位来绕开锁。

VER_ACK_TIMEOUT = 3.0
# 自动重启守护的冷却时间（秒）：防止"判定持续失败 → 反复重启"的循环
STALE_REFRESH_COOLDOWN = 60.0
_STALE_REFRESH = {"last": 0.0}
UPGRADE_BACKUP_SUFFIX = ".old.exe"


def self_exe_path():
    """当前进程对应的 exe 绝对路径（normcase 以便跨大小写比较）。

    打包态 = 自身；开发态 = BASE_DIR 下的 CampusNetAuth.exe。
    用于判断"正在跑的守护是不是同一个文件"——用户可能同时存在多份部署。
    """
    if getattr(sys, "frozen", False):
        return os.path.normcase(os.path.abspath(sys.executable))
    return os.path.normcase(os.path.join(BASE_DIR, APP_NAME + ".exe"))


def _ver_key(text):
    """把 "0.4.0" 归一成可比较的元组；无法解析返回 ()。"""
    try:
        parts = [int(x) for x in str(text).strip().split(".")]
    except (TypeError, ValueError):
        return ()
    return tuple(parts[:4])


# ---- GitHub Release 更新检查（只检查，不下载、不替换文件）----
_UPDATE_CACHE = {"t": 0.0, "data": None}
_UPDATE_LOCK = threading.Lock()
_UPDATE_FETCH_LOCK = threading.Lock()   # 同一时刻只允许一个网络请求在跑
UPDATE_STATE_KEY = "update_last"        # state.json 里保存最近一次检查结果


def _release_version(tag):
    """把 Release 标签 "V0.6.0" / "v0.6.0" 归一成 "0.6.0"。"""
    text = str(tag or "").strip()
    return text[1:] if text[:1] in ("v", "V") else text


def _release_url_allowed(url):
    """只允许打开本仓库的 Release 页面，避免把任意 URL 交给浏览器。"""
    return (isinstance(url, str)
            and url.startswith("https://github.com/%s/releases/" % GITHUB_REPO))


def _default_update_info():
    return {
        "ok": False, "current": APP_VERSION, "latest": "", "available": False,
        "url": GITHUB_RELEASES_URL, "name": "", "published_at": "",
        "checked_at": 0, "error": "",
    }


def _fmt_checked_at(ts):
    """把 checked_at 时间戳格式化为 "MM-DD HH:MM"（本地时区）。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "未知"


def _save_update_state(info):
    """把最近一次检查结果落盘，供**新进程**（status / upgrade --check）读取。

    只存检查结论；current / available 在读取时按当前 APP_VERSION 重算，
    避免升级后仍显示"有新版本"。
    """
    rec = {k: info.get(k) for k in
           ("ok", "latest", "available", "url", "name",
            "published_at", "checked_at", "error")}
    try:
        update_state({UPDATE_STATE_KEY: rec})
    except Exception as e:  # noqa: BLE001 落盘失败不影响检查结果
        log.debug("保存更新检查结果失败: %r", e)


def get_update_info():
    """读取最近一次 Release 检查结果：内存缓存 → state.json → 默认值。

    绝不发网络请求。缓存为空时用落盘记录"预热"，这样 CLI 新进程也能看到
    界面进程上次查到的结果，而不是永远显示"未检查"。
    """
    with _UPDATE_LOCK:
        if _UPDATE_CACHE["data"] is None:
            rec = load_state().get(UPDATE_STATE_KEY)
            if isinstance(rec, dict):
                data = _default_update_info()
                data.update({k: rec[k] for k in data if k in rec})
                data["current"] = APP_VERSION
                latest = str(data.get("latest") or "").strip()
                data["available"] = bool(latest) and (
                    _ver_key(latest) > _ver_key(APP_VERSION))
                _UPDATE_CACHE["data"] = data
                _UPDATE_CACHE["t"] = float(data.get("checked_at") or 0)
        data = _UPDATE_CACHE["data"]
        return dict(data) if data is not None else _default_update_info()


def update_check_due(now=None):
    """距离上次检查是否该重查（成功 6 小时、失败 5 分钟）。返回 (due, info)。

    只做判定、不发请求；由界面决定"当前在线时才真正去查"。
    """
    info = get_update_info()
    checked = int(info.get("checked_at") or 0)
    if not checked:
        return True, info
    interval = UPDATE_CHECK_TTL if info.get("ok") else UPDATE_CHECK_FAIL_TTL
    return ((now if now is not None else time.time()) - checked) >= interval, info


def check_latest_release(force=False, timeout=5.0):
    """查询 GitHub 最新正式 Release，返回可 JSON 序列化的结果。

    只使用标准库；失败不抛异常，由调用方决定是否提示。成功缓存 6 小时，
    失败缓存 5 分钟，避免界面反复轮询时打满 GitHub 的匿名 API 限额。
    结果同时落盘 state.json，CLI（新进程）也能读到上次检查结论。
    """
    now = time.time()
    with _UPDATE_LOCK:
        cached = _UPDATE_CACHE["data"]
        if cached is not None and not force:
            ttl = UPDATE_CHECK_TTL if cached.get("ok") else UPDATE_CHECK_FAIL_TTL
            if now - _UPDATE_CACHE["t"] < ttl:
                return dict(cached)
    if not _UPDATE_FETCH_LOCK.acquire(blocking=False):
        # 界面自动重查与手动点击撞车：只保留一个请求，另一个直接复用当前结果
        return get_update_info()
    try:
        info = _default_update_info()
        try:
            req = urllib.request.Request(
                GITHUB_API_LATEST,
                headers={
                    "User-Agent": "CampusNetAuth/" + APP_VERSION,
                    "Accept": "application/vnd.github+json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            if not isinstance(payload, dict):
                raise ValueError("GitHub API 返回格式异常")
            latest = _release_version(payload.get("tag_name"))
            if not _ver_key(latest):
                raise ValueError("无法解析 Release 版本号: %r"
                                 % (payload.get("tag_name"),))
            info.update({
                "ok": True,
                "latest": latest,
                "available": _ver_key(latest) > _ver_key(APP_VERSION),
                "name": str(payload.get("name") or ""),
                "published_at": str(payload.get("published_at") or ""),
            })
            html_url = payload.get("html_url")
            if _release_url_allowed(html_url):
                info["url"] = html_url
            log.debug("Release 检查：当前 %s / 最新 %s / 可用更新=%s",
                      APP_VERSION, latest, info["available"])
        except Exception as e:  # noqa: BLE001 网络/解析失败都按"检查失败"处理
            info["error"] = "%s: %s" % (e.__class__.__name__, e)
            log.debug("检查 GitHub Release 失败: %r", e)
        info["checked_at"] = int(time.time())
        _save_update_state(info)
        with _UPDATE_LOCK:
            _UPDATE_CACHE["t"] = time.time()
            _UPDATE_CACHE["data"] = dict(info)
        return info
    finally:
        _UPDATE_FETCH_LOCK.release()


def open_release_page():
    """用系统浏览器打开缓存的 Release 页面（只接受本仓库地址）。"""
    info = get_update_info()
    url = info.get("url") or GITHUB_RELEASES_URL
    if url != GITHUB_RELEASES_URL and not _release_url_allowed(url):
        log.warning("拒绝打开非本仓库 Release 地址: %r", url)
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url))
    except Exception as e:  # noqa: BLE001
        log.warning("打开 Release 页面失败: %r", e)
        return False


def _parse_ver_ack(text):
    """解析守护回执 "OK <version>\\t<exe_path>" → (version, path)。"""
    t = (text or "").strip()
    if not t.upper().startswith("OK"):
        return None, None
    body = t[2:].strip()
    ver, _sep, path = body.partition("\t")
    ver = ver.strip()
    if not ver:
        return None, None
    return ver, path.strip()


def daemon_info(timeout=VER_ACK_TIMEOUT):
    """向运行中的守护查询版本与自身路径（控制通道 VER 指令）。

    返回 (ok, version, exe_path, detail)：
      * ok=True  → 回执正常，version / exe_path 有效；
      * ok=False → 无回执或无法解析。⚠️ 无回执的**主因是旧版守护**——
        早于 0.5.0 的守护根本不认识 VER 指令，会把连接当噪声吞掉；
        次因是守护正忙于一轮探测，尚未回到 _wait()。调用方需结合
        state.json 的 daemon_version 兜底，别只看这里。
    """
    try:
        s = socket.create_connection(("127.0.0.1", SINGLETON_PORT),
                                     timeout=timeout)
    except OSError as e:
        return False, None, None, "守护未运行（%s）" % e.__class__.__name__
    try:
        s.sendall(b"VER")
        try:
            raw = s.recv(1024).decode("utf-8", "replace")
        except OSError:
            return False, None, None, "无回执（旧版守护，或正忙于探测）"
        if not raw.strip():
            # 旧守护收到不认识的指令会直接关连接 → recv 立刻拿到 EOF。
            # 这是"守护比 0.5.0 还旧"的确定性信号，措辞要能让人看懂。
            return False, None, None, "无回执（旧版守护，不认识 VER 指令）"
        ver, path = _parse_ver_ack(raw)
        if not ver:
            return False, None, None, "回执无法解析：%r" % raw[:64]
        return True, ver, path, ""
    except OSError as e:
        return False, None, None, "指令发送失败：%s" % e.__class__.__name__
    finally:
        try:
            s.close()
        except OSError:
            pass


def daemon_stale():
    """判断运行中的守护是否"旧版本 / 别处的程序"。

    返回 (stale, version, reason)。stale=False 且 version=None 表示守护没在跑。
    判定优先级：
      1) 控制通道 VER 实时回执（权威）；
      2) 无回执时回退 state.json 的 daemon_version（守护启动时落盘）。
         旧版守护不会写这个字段 → 天然被判定为陈旧，正是想要的结果。
    """
    if not daemon_running():
        return False, None, "守护未运行"
    ok, ver, path, detail = daemon_info()
    if ok:
        if ver != APP_VERSION:
            return True, ver, "守护版本 %s ≠ 当前 %s" % (ver, APP_VERSION)
        if path and path != self_exe_path():
            return True, ver, "守护运行的是另一份程序：%s" % path
        return False, ver, ""
    recorded = load_state().get("daemon_version")
    if recorded and recorded == APP_VERSION:
        # 守护没回话但落盘记录显示它就是当前版本 → 判为"正忙"，不动它
        return False, recorded, ""
    return True, recorded, detail or "守护未报告版本（早于 0.5.0）"


def ensure_daemon_fresh(force=False):
    """守护在跑旧版本时自动重启，让新版本立即生效。

    返回 (restarted, reason)。带冷却，避免反复重启。
    适用于：用户用"改名让位"的方式换了 exe（旧守护仍活着），
    或同时存在多份部署、端口被另一份的守护占着。
    """
    stale, ver, reason = daemon_stale()
    if not stale:
        return False, ""
    now = time.time()
    if not force and now - _STALE_REFRESH["last"] < STALE_REFRESH_COOLDOWN:
        return False, "冷却中（%s）" % reason
    _STALE_REFRESH["last"] = now
    log.warning("后台守护不是当前版本（%s），自动重启使其生效…", reason)
    if not stop_daemon():
        log.warning("停止旧守护未完全成功，仍继续尝试启动")
    if not start_daemon():
        log.error("守护重启失败（启动异常）")
        return False, reason
    record_event("daemon_refresh",
                 "后台守护已自动重启（%s → %s）" % (ver or "旧版", APP_VERSION))
    log.info("守护已重启为新版本 %s", APP_VERSION)
    return True, reason


# ---- 就地升级（解决"守护运行时 exe 被锁无法替换"）----

class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD)]


def pe_version(path):
    """静态读取 PE 文件的版本资源，返回 (version, product)。

    ⚠️ 刻意**不执行**候选文件：执行来路不明的 exe 只为了问一句版本号，
    风险与收益完全不成比例。这里用 Win32 版本 API 直接读资源，零执行。
    读不到（非 PE / 无版本资源）返回 (None, None)。

    版本号归一为 3 段（我们只写 x.y.z，资源里是 x.y.z.0）。
    """
    if not os.path.exists(path):
        return None, None
    try:
        ver_dll = ctypes.WinDLL("version")
        ver_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        ver_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR,
                                                    ctypes.c_void_p]
        size = ver_dll.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None, None
        buf = ctypes.create_string_buffer(size)
        if not ver_dll.GetFileVersionInfoW(path, 0, size, buf):
            return None, None

        def _query(sub):
            ptr = ctypes.c_void_p()
            ln = wintypes.UINT()
            if ver_dll.VerQueryValueW(buf, sub, ctypes.byref(ptr),
                                      ctypes.byref(ln)) and ptr.value:
                return ptr, ln.value
            return None, 0

        ver_out = None
        top, ln = _query("\\")
        if top and ln >= ctypes.sizeof(_VS_FIXEDFILEINFO):
            ffi = ctypes.cast(top, ctypes.POINTER(_VS_FIXEDFILEINFO)).contents
            ver_out = "%d.%d.%d" % (ffi.dwFileVersionMS >> 16,
                                    ffi.dwFileVersionMS & 0xFFFF,
                                    ffi.dwFileVersionLS >> 16)

        product = None
        tv, tln = _query("\\VarFileInfo\\Translation")
        if tv and tln >= 4:
            lang, cp = ctypes.cast(
                tv, ctypes.POINTER(ctypes.c_ushort * 2)).contents
            sub = "\\StringFileInfo\\%04x%04x\\ProductName" % (lang, cp)
            pv, pln = _query(sub)
            if pv:
                product = ctypes.wstring_at(pv, pln).strip().rstrip("\x00")
        return ver_out, product
    except Exception as e:  # noqa: BLE001
        log.debug("读取版本资源失败（%s）: %r", os.path.basename(path), e)
        return None, None


def upgrade_backup_path():
    """就地升级时旧版本让位后的路径（下一次启动自动清理）。"""
    exe = daemon_exe() if getattr(sys, "frozen", False) else \
        os.path.join(BASE_DIR, APP_NAME + ".exe")
    return exe + UPGRADE_BACKUP_SUFFIX


def cleanup_upgrade_backup():
    """删除上次升级留下的 .old.exe（此时旧进程早已退出，文件不再被占用）。"""
    p = upgrade_backup_path()
    if not os.path.exists(p):
        return False
    try:
        os.remove(p)
        log.info("已清理上次升级的旧版本备份：%s", os.path.basename(p))
        return True
    except OSError:
        # 旧界面进程可能还映射着它，下次启动再试即可，不值得报错
        log.debug("暂无法清理 %s（可能仍被占用）", os.path.basename(p))
        return False


# 绝不能被当成"升级候选"的同前缀文件名（卸载器 / 历史 daemon exe 副本）。
# ⚠️ 定义在函数内部：这两个常量在本函数之后才声明，模块级求值会 NameError。


def find_upgrade_candidate():
    """在程序目录里找"版本比当前更高"的 CampusNetAuth exe。

    覆盖用户最常见的两种落地方式：
      * 从 GitHub 直接下载 → 与现有 exe 同名，Windows 自动存成
        `CampusNetAuth (1).exe`（拖进同一目录时）；
      * 自己改名成 `CampusNetAuth.new.exe` 再放进来。

    排除自身、升级备份 `*.old.exe`，并要求版本严格高于当前。
    返回 (path, version)；没有候选返回 (None, None)。
    """
    if not getattr(sys, "frozen", False):
        return None, None      # 开发态不做自我替换
    cur = self_exe_path()
    cur_key = _ver_key(APP_VERSION)
    # 卸载器 / 历史 daemon 副本与主程序同前缀，绝不能被当成"更高版本的升级
    # 候选"（误判会把用户点「立即升级」变成把组件换到主程序位置上）
    skip = (UNINSTALL_EXE_NAME.lower(), DAEMON_EXE_NAME.lower())
    best = (None, None, ())
    try:
        names = os.listdir(BASE_DIR)
    except OSError:
        return None, None
    for name in names:
        low = name.lower()
        if not low.endswith(".exe"):
            continue
        if not low.startswith(APP_NAME.lower()):
            continue
        if low.endswith(UPGRADE_BACKUP_SUFFIX.lower()):
            continue
        if low in skip:
            continue
        p = os.path.join(BASE_DIR, name)
        if os.path.normcase(os.path.abspath(p)) == cur:
            continue
        ver, product = pe_version(p)
        if not ver:
            continue
        # 双重校验：版本资源必须自报是 CampusNetAuth，避免误吞同名软件
        if product and product != APP_NAME:
            continue
        key = _ver_key(ver)
        if len(key) < 3 or key <= cur_key:
            continue
        if key > best[2]:
            best = (p, ver, key)
    return best[0], best[1]


def apply_upgrade(new_path=None):
    """把更高版本的 exe 就地替换到 CampusNetAuth.exe 并重启守护。

    返回 (ok, message)。步骤刻意保守：
      1. 校验候选（版本资源必须更高，且自报 CampusNetAuth）；
      2. 停守护 —— 运行中的 exe 被锁（WinError 32），不停就换不了；
      3. 现 exe 改名 `.old.exe` 让位（重命名在运行中是允许的，实测）；
      4. 新 exe 就位；
      5. 原本在跑守护的话，用新版本重新拉起。
    旧版本保留为 `.old.exe`，下次启动 `cleanup_upgrade_backup()` 自动删。
    界面进程自己仍映射着旧镜像，故需用户关闭并重新打开界面。
    """
    if not getattr(sys, "frozen", False):
        return False, "开发态不支持就地升级（请直接重新构建）"
    target = self_exe_path()
    if not new_path:
        new_path, ver = find_upgrade_candidate()
        if not new_path:
            return False, "程序目录里没有找到更高版本的程序文件"
    else:
        ver, product = pe_version(new_path)
        if not ver:
            return False, "无法读取该文件的版本信息，可能不是有效的程序文件"
        if product and product != APP_NAME:
            return False, "该文件的 ProductName 是 %r，不是 %s" % (product,
                                                                 APP_NAME)
        if _ver_key(ver) <= _ver_key(APP_VERSION):
            return False, "该文件版本 %s 不高于当前 %s" % (ver, APP_VERSION)
    if not os.path.exists(new_path):
        return False, "升级文件不存在：%s" % new_path

    was_running = daemon_running()
    backup = upgrade_backup_path()
    log.info("开始就地升级：%s → %s（当前 %s）", APP_VERSION, ver, target)
    if was_running and not stop_daemon():
        return False, "无法停止后台守护，升级已中止（可手动停止后重试）"
    # 上一次的备份若还在（通常已被启动清理），先腾位
    try:
        if os.path.exists(backup):
            os.remove(backup)
    except OSError:
        pass
    try:
        os.replace(target, backup)          # 旧 exe 让位（运行中也允许）
        os.replace(new_path, target)        # 新 exe 就位
    except OSError as e:
        # 尽力回滚，别把用户撂在"没有 exe"的状态
        try:
            if not os.path.exists(target) and os.path.exists(backup):
                os.replace(backup, target)
        except OSError:
            pass
        log.error("就地升级失败：%r", e)
        return False, "替换文件失败：%s（可能仍被占用）" % e
    log.info("升级文件已就位，旧版本备份为 %s", os.path.basename(backup))
    if was_running and not start_daemon():
        return True, ("程序已升级到 %s，但后台守护未能自动启动，"
                      "请在界面里手动启动" % ver)
    record_event("upgrade", "程序已升级 %s → %s" % (APP_VERSION, ver))
    return True, ("已升级到 %s。请关闭并重新打开管理界面，"
                  "新版本才会完全生效" % ver)


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
    """守护启动时记录自身 PID + 父进程 PID，便于管理界面精确停止它。

    为什么要记父 PID：onefile 打包下"守护"其实是**两个同映像进程**——
    PyInstaller 引导父进程 P0（解压 _MEI、等子进程退出后清理）与应用子进程
    P1（真正跑 python 代码、持有单例端口）。只按端口杀 P1 时，P0 仍会短暂
    存活并**持有 exe 文件句柄**：用户看到"守护已经关了，任务管理器里还有一个
    CampusNetAuth 在跑"，而且它占着 exe → 删目录/换文件报"文件正在使用中"
    （WinError 32），这也是历史卸载失败的根因之一。记下 P0 的 PID 才能在
    停止时把它一起收干净。

    格式：第一行自身 PID，第二行父 PID（旧的单行格式仍可读）。
    """
    try:
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write("%d\n%d\n" % (os.getpid(), os.getppid()))
    except Exception:
        pass


def remove_pid():
    try:
        if os.path.exists(PID_PATH):
            os.remove(PID_PATH)
    except Exception:
        pass


def read_pid_family():
    """读 daemon.pid → (pid, ppid)。任一不可解析取 None；文件不存在 (None, None)。

    兼容历史单行格式（老守护只写自身 PID）。"""
    if not os.path.exists(PID_PATH):
        return None, None
    try:
        with open(PID_PATH, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.read().splitlines()]
    except Exception:
        return None, None

    def _num(s):
        try:
            v = int(s)
            return v if v > 0 else None
        except Exception:
            return None
    pid = _num(lines[0]) if lines else None
    ppid = _num(lines[1]) if len(lines) > 1 else None
    return pid, ppid


def read_pid():
    """返回仍然存活的守护进程 PID，否则 None。

    ⚠️ 只验"PID 存活"不足以信任：pid 文件可能是硬杀、断电或"整目录复制"留下
    的陈迹，而 Windows 会**复用 PID** —— 盲信会去杀一个完全无关的用户进程
    （老实现正是如此，属于真实存在的误杀风险）。

    判定：存活 + 映像确属本程序（打包态）；开发态/取不到映像路径时沿用旧行为
    （放行），因为那时无法用映像路径认领，而开发环境不存在多份部署混跑。
    """
    pid, _ = read_pid_family()
    if not pid:
        return None
    if not _proc_alive(pid):
        return None
    if _own_exe_path() is not None and _proc_is_own(pid) is False:
        # PID 已被系统复用给别的进程（或 pid 文件来自另一份部署）
        log.debug("daemon.pid 指向的 PID=%s 映像不属于本程序，忽略", pid)
        return None
    return pid


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


def clean_pyi_env():
    """剥离 PyInstaller onefile 的阶段标记环境变量（_MEIPASS2 / _PYI*）。

    onefile 的第一段引导进程拉起第二段（本进程）时会把 _MEIPASS2 写进环境，
    第二段据此跳过解压、直接用第一段解压好的 _MEI 临时目录。本进程再用
    Popen 启动同 exe 的子进程（守护 / 诊断）时若原样继承这些变量，子进程的
    引导器同样会跳过解压、复用本进程的 _MEI 目录。后果：本进程退出后引导
    进程删除该目录，而子进程仍映射着其中 python313.dll 等文件 → 弹
    "Failed to remove temporary directory" 警告框，目录被掏空。
    剥离后子进程解压出自己的私有 _MEI，退出清理互不干扰。
    """
    env = dict(os.environ)
    for k in [k for k in env if k == "_MEIPASS2" or k.startswith("_PYI")]:
        env.pop(k, None)
    return env


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


# ---------- 进程归属判定（防 PID 复用误杀 / 认领本程序进程族） ----------
# 为什么需要这一套：
#   ① **PID 复用**：daemon.pid 可能是硬杀、断电、整目录复制留下的陈迹，其中
#      记录的 PID 早被系统分配给别的用户进程。老实现只验"PID 存活"就杀，
#      等于随机杀人（真实风险，非理论问题）。
#   ② **onefile 双进程**：打包后守护 = 引导父进程 P0（解压 _MEI、等子进程）
#      + 应用子进程 P1（跑 python、持单例端口）。只按端口杀 P1 会留下 P0，
#      它持有 exe 文件句柄 → 用户看到"守护关了还有一个 CampusNetAuth 在跑"，
#      且删目录/换文件报 WinError 32（卸载失败的根因）。
# 判定依据只有"映像文件全路径 == 当前 exe 文件"：精确到文件，**绝不按映像名
# 判定**（同名多份部署很常见，按名杀会误伤另一份实例）。

class _PROCESSENTRY32W(ctypes.Structure):
    """PROCESSENTRY32W（Toolhelp32 进程快照行）"""
    _fields_ = [("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def _enum_processes():
    """进程快照 → [(pid, ppid, exe_name_lower)]；失败返回 []。

    纯 PInvoke，零子进程：绝不能改用 tasklist/wmic（GUI 进程起 CUI 子进程会
    闪终端窗口，见 _port_pid 的长注释）。
    """
    out = []
    if os.name != "nt":
        return out
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return out
        try:
            pe = _PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                out.append((int(pe.th32ProcessID),
                            int(pe.th32ParentProcessID),
                            (pe.szExeFile or "").lower()))
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return []
    return out


def _proc_image_path(pid):
    """进程映像文件的规范全路径（normcase 绝对路径）；取不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf,
                                                  ctypes.byref(size)):
                return None
            p = buf.value
            return os.path.normcase(os.path.abspath(p)) if p else None
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def _proc_alive(pid):
    """PID 是否存活（Windows 用 OpenProcess 存在性，POSIX 用信号 0）。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _own_exe_path():
    """本程序自身 exe 的规范路径；开发态返回 None（无法用映像路径认领）。

    刻意只认**当前 exe 文件**：同目录的 `CampusNetAuth (1).exe`（用户下载的
    新版本）算"另一份部署"，不参与本程序进程族判定 —— 免得把用户手动放进
    来、可能正在跑的新版本进程当自己人杀掉。
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        return os.path.normcase(os.path.abspath(sys.executable))
    except Exception:
        return None


def _proc_is_own(pid):
    """PID 是否确属本程序：True 是 / False 不是 / None 无法判定。

    None 的两种来源：开发态（映像=python.exe，不能用路径认领）、进程已退出
    或权限不足取不到映像路径。调用方自行决定 None 是保守拒绝还是沿用旧行为。
    """
    own = _own_exe_path()
    if own is None:
        return None
    img = _proc_image_path(pid)
    if img is None:
        return None
    return img == own


def _kill_if_own(pid, what="进程"):
    """只在"确认是本程序进程"时终止 PID（开发态判不了归属，放行）。

    返回是否已确认该进程不再存在。这条守卫防的是 PID 复用误杀：pid 文件里的
    陈旧 PID 可能已属于别人的进程，盲杀等于随机杀人。

    ⚠️ 三种结果必须分开说：
      * 确认不是本程序（False）—— 真的可能是 PID 被复用，要用户能看懂；
      * 无法判定（None，权限不足/进程已退出）—— 不能诬赖成“PID 被复用”，
        否则日志会误导排查方向（实测踩过：升级后 stop 报这句，其实是
        取不到映像路径）。
    """
    if not _proc_alive(pid):
        return True
    own = _own_exe_path()
    if own is not None:
        mine = _proc_is_own(pid)
        if mine is False:
            log.warning("拒绝终止%s PID=%s：映像不属于本程序（PID 被复用？）",
                        what, pid)
            return False
        if mine is None:
            # 取不到映像路径（权限/已退出）——安全起見不杀，但不要误导
            log.debug("跳过终止%s PID=%s：无法确认进程归属", what, pid)
            return False
    return _kill_pid(pid)


def own_processes():
    """以当前 exe 为映像的全部进程 [(pid, ppid, name)]（不含本进程）。

    开发态返回 []（python.exe 太泛，认领会误伤其他 python 程序）。
    """
    out = []
    own = _own_exe_path()
    if own is None:
        return out
    me = os.getpid()
    own_name = os.path.basename(own)
    for pid, ppid, name in _enum_processes():
        if pid == me or name != own_name:
            continue          # 先用映像名快速过滤，再做全路径确认
        if _proc_image_path(pid) == own:
            out.append((pid, ppid, name))
    return out


def daemon_family_pids():
    """守护进程族 PID（去重、已滤掉非本程序进程）。

    来源互补：① daemon.pid 记录的"应用子进程 + 引导父进程"；② 单例端口
    占用者及其父进程。pid 文件被删/写失败时靠端口；守护应用进程已被杀、
    只剩引导父进程时靠 pid 文件里记的父 PID。
    """
    cands = list(read_pid_family())
    owner = _port_pid(SINGLETON_PORT)
    if owner:
        cands.append(owner)
        for pid, ppid, _n in _enum_processes():
            if pid == owner:
                cands.append(ppid)
                break
    out = []
    for c in cands:
        if not c or c in out or not _proc_alive(c):
            continue
        if _own_exe_path() is not None and _proc_is_own(c) is not True:
            continue
        out.append(c)
    return out


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


def stop_daemon(wait_timeout=8.0):
    """停止后台守护，返回是否彻底停止（**幂等**：本来没在跑也算成功）。

    步骤刻意保守：
      1. 优雅停止：连单例端口发 STOP，守护自行退出（零残留）；
      2. STOP 不管用（守护正忙于一轮探测，最长 8s 才回到可中断等待）→ 按端口
         占用者 PID 强杀，**先验映像归属**（防 PID 复用误杀）；
      3. 收尾整个守护进程族：onefile 的引导父进程会持有 exe 文件句柄，只停
         应用子进程会留下它 —— 用户看到"守护关了还有一个 CampusNetAuth 在
         跑"，且它占着 exe 使卸载/替换报 WinError 32。
    兼容旧版 Daemon.exe 副本启动的守护（同样监听单例端口）。
    """
    family = daemon_family_pids()
    if not family and not daemon_running():
        remove_pid()
        return True                       # 幂等：没在跑 = 已经是停止状态

    graceful = _stop_daemon_graceful() if daemon_running() else False

    # 等守护自己退出（端口释放）
    deadline = time.time() + (6.0 if graceful else 0.5)
    while time.time() < deadline:
        if not daemon_running():
            break
        time.sleep(0.3)

    if daemon_running():
        # STOP 没生效：按端口占用者强杀（归属校验后才动手）
        leftover = _port_pid(SINGLETON_PORT)
        if leftover:
            _kill_if_own(leftover, "守护")

    # 等端口真正释放（强杀是异步的）
    deadline = time.time() + max(0.0, float(wait_timeout))
    while time.time() < deadline and daemon_running():
        time.sleep(0.2)

    # 收尾进程族：引导父进程、以及刚被强杀仍在退出中的子进程。
    # ⚠️ 顺序很关键：onefile 的引导父进程在子进程退出后会**自己**收尾
    # （清掉 _MEI 解包目录 + 以子进程的退出码退出，所以优雅停止时我们看到的
    # rc 就是 0）。所以先让它自然退出，只有赖着不走才强杀——强杀会跳过它的
    # _MEI 清理（留下解包残渣），还会把退出码变成 1（被看门狗/冒烟当成
    # "非优雅退出"，历史上就踩过这个坑）。
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not daemon_running() and not daemon_family_pids():
            break
        time.sleep(0.2)

    for p in dict.fromkeys(list(family) + daemon_family_pids()):
        _kill_if_own(p, "守护进程族")

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not daemon_running() and not daemon_family_pids():
            break
        time.sleep(0.2)

    alive_pids = daemon_family_pids()
    alive = daemon_running() or bool(alive_pids)
    if alive:
        # ⚠️ 停不干净时**保留 pid 文件**：下次停止/卸载才有线索找到残留的
        # 引导父进程（老实现无论成败都删，等于把唯一的线索丢掉了）
        log.error("守护停止未彻底：仍有进程存活 %s（pid 文件已保留）", alive_pids)
    else:
        remove_pid()
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
    # ---- 残留会话自愈（学校门户偶发：登录返回 success 但会话未生效）----
    stale_streak = 0       # 连续触发次数（>=3 时退避，避免高频敲门户）
    stale_notified = False  # 本段异常期是否已提醒过（每次异常只提醒一次）

    # ---- 运行统计与心跳节流 ----
    started_at = time.time()
    stats = {"loops": 0, "login_ok": 0, "login_fail": 0,
             "reconnects": 0, "state_flips": 0, "keepalive_ok": 0,
             "stale_recover": 0}
    # 累计计数落盘：stats 是本进程计数，守护每重启一次就归零，心跳里的
    # “累计登录成功=0”会让人误以为从未认证过（实测误导过排查）。
    # totals 是跨进程累计，只记“结果类”事件。
    _TOTAL_KEYS = ("login_ok", "login_fail", "reconnects", "stale_recover")
    try:
        _saved = load_state().get("stats")
        totals = {k: int((_saved or {}).get(k) or 0) for k in _TOTAL_KEYS}
    except (TypeError, ValueError):
        totals = {k: 0 for k in _TOTAL_KEYS}
    def _bump(key, n=1):
        """同时记本进程与跨进程累计（写盘限流 60s，避免登录风暴刷盘）。"""
        stats[key] = stats.get(key, 0) + n
        totals[key] = totals.get(key, 0) + n
        try:
            save_stats_totals(totals)
        except Exception:
            pass

    pause_iv = int(cfg.get("logout_pause", 600))
    pause_logged = 0          # 已提示过的静默期截止时间（避免每轮刷屏）
    blocked_reason = ""       # 门户侧要求验证码/短信/CAS 时的结论（空=无）
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

    # 版本落盘：控制通道 VER 是实时权威判据，但守护可能正忙于一轮探测而
    # 未能及时回执。此时界面回退读取这份记录来判断"守护是不是当前版本"——
    # 旧版守护不会写这个字段，于是天然被判为陈旧并触发自动重启（升级自愈）。
    update_state({"daemon_version": APP_VERSION,
                  "daemon_exe": self_exe_path(),
                  "daemon_started": time.strftime("%Y-%m-%d %H:%M:%S")},
                 drop=())

    def _hb():
        """在线静默期的心跳日志，证明守护存活（不刷屏）"""
        nonlocal last_heartbeat
        now = time.time()
        if now - last_heartbeat >= HB_INTERVAL:
            last_heartbeat = now
            log.info("存活心跳 | 已运行 %s | 在线 | 累计登录成功=%d 失败=%d"
                     "（本次运行 +%d/+%d）",
                     _fmt_uptime(now - started_at),
                     totals["login_ok"], totals["login_fail"],
                     stats["login_ok"], stats["login_fail"])

    import select as _select

    def _wait(sec):
        """可中断等待：阻塞至超时或收到指令
        （STOP=优雅退出 / CHECK=唤醒复核 / NOTIFY=发测试通知）。

        select 可被 socket 数据即时打断，比 time.sleep 多了"立即响应"能力：
        UI 手动操作后发 CHECK，等待中的守护立刻醒来复核，不等完整周期。

        ⚠️ 空连接（daemon_running() 存活探测就是"连接即关、零数据"）绝不能
        截断等待——否则 UI 每 20s 轮询一次 /api/status 就把守护提前唤醒做
        一整轮探测，interval 检测周期名存实亡、公网探测目标被白打。空数据
        或未知指令一律吞掉、继续等剩余时间；只有 STOP / CHECK 才真正打断。

        NOTIFY 也不打断等待（只借这条通道让"守护进程自己"发一条通知，
        用于验证无人值守时的通知通道），处理完继续等剩余时间。
        VER 同理：只回一句"我是谁 + 我是哪个版本"，用于判定守护是否需要
        因升级而自动重启，同样不打断等待。
        """
        deadline = time.time() + max(0.0, float(sec))
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            try:
                _ready, _, _ = _select.select([lock], [], [], remain)
                if not _ready:
                    return None
                _conn, _ = lock.accept()
                try:
                    _data = _conn.recv(16).decode("utf-8", "replace").strip().upper()
                    if _data == "VER":
                        # 版本握手：让界面/CLI 判断"正在跑的守护是不是当前版本"。
                        # 不认识该指令的旧守护会把它当噪声吞掉，调用方据"无回执"
                        # 判定为陈旧，进而自动重启——这正是升级自愈的入口。
                        try:
                            _conn.sendall(("OK %s\t%s\n" % (
                                APP_VERSION, self_exe_path())).encode("utf-8"))
                        except OSError:
                            pass
                    elif _data == "NOTIFY":
                        # 通知测试：由守护进程自己发出（模拟"界面不在"的
                        # 无人值守场景），验证托盘通知通道是否被系统放行
                        log.info("收到通知测试指令，守护发送测试通知")
                        notify_system(
                            "校园网无感认证（守护）",
                            "通知测试：看到这条说明无人值守通知通道正常",
                            dedup=False)
                        try:
                            _conn.sendall(b"OK")  # 回执：让调用方确认真被处理
                        except OSError:
                            pass
                finally:
                    _conn.close()
            except Exception:
                return None
            if _data == "STOP":
                log.info("收到停止指令，守护优雅退出")
                return "STOP"
            if _data == "CHECK":
                # UI 手动登录/操作后发来：结束等待，立即进入本轮探测
                log.info("收到唤醒指令，立即复核网络状态")
                return "CHECK"
            # 空连接 / 未知数据：存活探测或噪声，吞掉并继续等待剩余时间

    while True:
        try:
            stats["loops"] += 1

            # 门户守卫：仅当"尚未确认在校园网"时才探测门户；已确认（portal_ok）
            # 则跳过 —— 外网在线必然说明校园网在线，无需每轮重复探测门户
            # （在线路径每轮从 4 次 HTTP 降到 3 次并发）。
            # 校外网络（家里/热点/公共 WiFi）门户不可达，守护静默等待、绝不认证。
            if not portal_ok:
                if not client.portal_reachable():
                    wait = min(offline_interval * min(fail_streak + 1, 10),
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
                        signal = _wait(_seg)
                        if signal == "STOP":
                            return 0
                        if signal == "CHECK":
                            break
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

            online, login_page = client.check_authentication()
            # ---- 状态事件上浮：仅在状态翻转时记录（首轮不产生假事件）----
            if online != prev_online:
                if online is True and prev_online is False:
                    record_event("restored", "网络已恢复在线")
                elif online is False and prev_online is True:
                    # 手动下线的静默期内不要记成"正在自动重连"——那是假消息
                    if logout_pause_left():
                        record_event("offline", "手动下线中，静默期内不会自动重连")
                    else:
                        record_event("lost", "检测到未认证，正在自动重连…")
                elif online is None and prev_online is not None:
                    record_event("offline", "公网探测无结论，等待复核门户状态…")
                prev_online = online
            if online:
                if fail_streak:
                    log.info("网络已恢复")
                fail_streak = 0
                # 已在线：复位残留会话自愈计数（下次异常可再提醒一次）
                stale_streak = 0
                stale_notified = False
                # 持久化当前 userIndex，便于 logout 命令在"开机即在线"时也能用
                try:
                    info = client.interface("getOnlineUserInfo")
                    if isinstance(info, dict) and info.get("userIndex"):
                        if persisted_index != info["userIndex"]:
                            persisted_index = info["userIndex"]
                            with _state_lock():
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
                portal_ok = client.portal_reachable()
                wait = min(offline_interval * min(fail_streak + 1, 10),
                           max_interval)
                if fail_streak == 0:
                    log.info("公网探测无结论，%s，等待重试…",
                             "门户可达但页面不满足认证条件" if portal_ok
                             else "校园网门户不可达")
                elif fail_streak % 10 == 1:
                    log.info("公网探测仍无结论：已连续 %d 次，退避等待 %ss",
                             fail_streak, wait)
                fail_streak += 1
                # 退避等待分段快探：每 recheck_iv 秒复核一次外网连通性，
                # 网络恢复立即回到在线检测，不等完整退避周期
                _slept = 0
                while _slept < wait:
                    _seg = min(recheck_iv, wait - _slept)
                    signal = _wait(_seg)
                    if signal == "STOP":
                        return 0
                    if signal == "CHECK":
                        break
                    _slept += _seg
                    if client.check_online()[0] is not None:
                        log.info("公网探测已有结果，提前结束退避，立即复核")
                        break
                continue

            # ---- 用户主动下线后的静默期：不自动登回 ----
            left = logout_pause_left() if pause_iv > 0 else 0
            if left:
                # 按“截止时刻”去重：剩多少秒每轮都在变，拿它比会每轮刷一条日志
                deadline = int(time.time()) + left
                if pause_logged != deadline:
                    pause_logged = deadline
                    log.info("处于「手动下线」静默期（剩 %ds），不自动登录；"
                             "点界面「立即连接」或执行 login 可提前恢复",
                             left)
                if _wait(min(left, interval)) == "STOP":
                    return 0
                continue
            pause_logged = 0

            log.info("检测到未认证，开始登录…")
            res = client.login(username, cred, login_page=login_page)
            if res.get("result") == "success":
                _bump("login_ok")
                if fail_streak:
                    _bump("reconnects")
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
                    # 门户说"登录成功"、实际仍未认证 —— 典型"残留会话"
                    # （学校门户偶发：账号页有效期显示 1970 之类的异常值，
                    # 上次的失效会话还挂在门户上）。此时**直接重登是无效的**：
                    # 门户认为该账号已在线，返回 success 但不建立新会话，
                    # 于是陷入"每 5 秒登录一次却永远上不了网"的死循环。
                    # 处置：先主动下线清掉残留会话，下一轮再重新认证。
                    stale_streak += 1
                    stats["stale_recover"] += 1
                    log.warning("登录后复核未通过（疑似门户残留会话/认证异常，"
                                "连续第 %d 次），先主动下线清理再重新认证…",
                                stale_streak)
                    _lo_ok, _lo_msg = logout_stale_session(
                        client, res.get("userIndex"), username, cred)
                    log.info("主动下线（清理残留会话）%s：%s",
                             "成功" if _lo_ok else "未成功", _lo_msg)
                    if not stale_notified:
                        # 同一段异常期内只提醒一次，避免刷屏
                        stale_notified = True
                        record_event("portal_fix",
                                     "检测到门户认证异常，已自动退出并重新认证")
                        notify_system(
                            "校园网认证异常",
                            "检测到门户认证异常，已自动退出并重新认证")
                    # 连续多次仍失败说明不是残留会话问题（如账号被限制），
                    # 按失败次数退避，别高频敲门户
                    backoff = (min(retry * stale_streak, max_interval)
                               if stale_streak >= 3 else 2)
                    if _wait(backoff) == "STOP":
                        return 0
                    continue
                # 复核通过：清空残留会话自愈计数（下次异常可再提醒一次）
                stale_streak = 0
                stale_notified = False
                blocked_reason = ""
                if _wait(interval) == "STOP":
                    return 0
            else:
                _bump("login_fail")
                fail_streak += 1
                # 门户要求验证码/短信/CAS：账号密码自动认证不可能成功，别让它
                # 按 30s→600s 无休止地敲下去，也不要每轮都说一次。
                blocked = str(res.get("_blocked") or "")
                if blocked:
                    if blocked != blocked_reason:
                        blocked_reason = blocked
                        log.error("无法自动认证：%s", blocked)
                        record_event("blocked", "无法自动认证：%s" % blocked)
                        update_state({"last_error": {
                            "t": int(time.time()), "msg": blocked}})
                        notify_system("校园网需要人工认证", blocked)
                    wait = max(max_interval, 1800)
                    log.error("按门户改造处理：%ds 后再试一次（其间不会反复提交"
                              "凭据）", wait)
                    if _wait(wait) == "STOP":
                        return 0
                    continue
                blocked_reason = ""
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
                     "登录失败=%d | 自动重连=%d | 网络状态切换=%d | "
                     "残留会话自愈=%d",
                     _fmt_uptime(time.time() - started_at),
                     stats["loops"], stats["login_ok"], stats["login_fail"],
                     stats["reconnects"], stats["state_flips"],
                     stats["stale_recover"])
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


# ====================== 单文件资源与历史遗留组件 ======================
# 0.6.0 起自启 / 守护 / 界面全部由同一个 exe 承担，不再释放 VBS 组件。
# VBS / Daemon.exe 名称仅用于识别并清理旧版本遗留文件。

DAEMON_EXE_NAME = "CampusNetAuthDaemon.exe"
DAEMON_VBS_NAME = "CampusNetAuthDaemon.vbs"
UI_VBS_NAME = "CampusNetAuthUI.vbs"
# 内置卸载器（微 exe，约 15KB）：随主 exe 一起打包（--add-data），首次打开
# 界面时释放到程序目录。之所以必须是**独立文件**而不是"主 exe 的某个参数"：
# Windows 上正在运行的 exe 文件被系统锁定，无法删除自己；只有另一个 exe
# 才能把主 exe（以及它的整目录）彻底删干净。
UNINSTALL_EXE_NAME = "CampusNetAuthUninstall.exe"

# 程序自身产生的全部文件（foreign_files 里排除，避免把自家产物当"无关文件"）
_PRODUCT_FILES = (
    DAEMON_EXE_NAME, DAEMON_VBS_NAME, UI_VBS_NAME, UNINSTALL_EXE_NAME,
    "config.json", "cred.bin", "state.json",
    "campusnet.log", "daemon.pid", "_ui_trace.log", "_ui_crash.log",
    # 自启启动器 vbs 启动失败时写的容错日志（vbs 侧写，之前漏登记 →
    # 全新部署横幅会把它当成"无关文件"）
    "autostart.log",
)
# 轮转日志（RotatingFileHandler backupCount=3）、原子写残渣、升级备份：
# 卸载时必须一并清掉，否则"看起来删了、其实留了一地"
_PRODUCT_GLOBS = (
    "campusnet.log.*", "state.json.lock", "config.json.tmp",
    "state.json.tmp", "*.old.exe",
)
# 判定"是否全新部署"只认部署产物（不认运行时文件 log/pid，
# 否则首次启动瞬间就会被视为已部署，空文件夹提醒失效）
_DEPLOY_MARKERS = (
    DAEMON_EXE_NAME, DAEMON_VBS_NAME, UI_VBS_NAME,
    "config.json", "cred.bin", "state.json",
)


def _asset_path(name):
    """内置资源路径：打包态在 PyInstaller 解包目录（--add-data），
    开发态直接读脚本所在目录的源文件（卸载器则读 build_assets\\，
    它由 build_uninstaller.bat 编译产出，开发态也能验证释放链路）。"""
    if getattr(sys, "frozen", False):
        p = os.path.join(getattr(sys, "_MEIPASS", ""), name)
        if os.path.exists(p):
            return p
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, name)
    if os.path.exists(p):
        return p
    return os.path.join(here, "build_assets", name)


def uninstaller_path():
    """内置卸载器在程序目录里的路径。"""
    return os.path.join(BASE_DIR, UNINSTALL_EXE_NAME)


def uninstaller_available():
    """卸载器是否已就位（界面据此启用「彻底卸载」入口）。"""
    return os.path.exists(uninstaller_path())


def ensure_uninstaller(force=False):
    """把内置卸载器释放到程序目录（每次打开界面时调用，幂等）。

    * 已存在且与内置资源**大小一致** → 视为已就绪（升级换了主 exe 时大小
      会变 → 自动刷新，避免留下旧版卸载器）；
    * 内置资源缺失（构建时没打进）→ 只记日志并返回 False，绝不生成空文件；
    * 卸载器正在运行（文件被占用）导致复制失败 → 视为已就绪（在用就是可用）。
    返回卸载器是否可用。
    """
    src = _asset_path(UNINSTALL_EXE_NAME)
    dst = uninstaller_path()
    if not os.path.exists(src):
        log.warning("内置卸载器缺失（打包时未打入 %s）；请用 build_exe.bat "
                    "重新构建", UNINSTALL_EXE_NAME)
        return False
    if not force:
        try:
            if (os.path.exists(dst)
                    and os.path.getsize(dst) == os.path.getsize(src)):
                return True
        except OSError:
            pass
    try:
        shutil.copy2(src, dst)
        log.info("已释放卸载器: %s", dst)
        return True
    except Exception as e:  # noqa: BLE001
        # 卸载器正在运行时文件被占用，复制失败属正常（旧版仍在用即已就绪）
        log.debug("释放卸载器失败（可能正在运行）: %r", e)
        return os.path.exists(dst)


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
    with _state_lock():
        st = load_state()
        if "first_run_at" not in st:
            st["first_run_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_state(st)


def enable_autostart_service():
    """一键开启自启服务：写 Run 键 + 确保守护在跑（不再安装 VBS 组件）。

    返回 (ok, message)。自启写入成功但守护启动失败时仍返回 True，
    因为开机自启已生效；由界面提示用户手动点「启动守护」。
    """
    if not install_autostart():
        return False, "写入开机自启注册表失败，详见日志"
    try:
        if not daemon_running():
            start_daemon()
    except Exception as e:
        log.warning("自启已启用，但启动守护失败: %r", e)
        return True, "开机自启已启用，但守护启动失败，请点「启动守护」重试"
    return True, "开机自启已启用，后台守护已启动"


def disable_autostart_service():
    """关闭自启服务：只删 Run 键，当前运行中的守护不受影响。"""
    ok = uninstall_autostart()
    return ok, ("开机自启已关闭（当前守护未受影响）" if ok
                else "移除开机自启失败，详见日志")


def daemon_exe():
    """返回守护进程可执行文件路径。

    * 打包态：直接返回自身 exe（守护 = 同一 CampusNetAuth.exe + daemon 参数）。
    * 开发态：保持 pythonw 命名副本逻辑。
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
    """以无窗口方式启动守护，返回是否成功。"""
    if daemon_running():
        return True
    try:
        exe = daemon_exe()
        if getattr(sys, "frozen", False):
            # 打包态：守护 = 同一 CampusNetAuth.exe + "daemon" 参数；
            # CREATE_NO_WINDOW 保证无黑窗（不再需要 Daemon.exe 命名副本）
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            subprocess.Popen([exe, "daemon"], cwd=BASE_DIR,
                             creationflags=flags, close_fds=True,
                             env=clean_pyi_env())
        else:
            script = os.path.abspath(__file__)
            subprocess.Popen(
                [exe, script, "daemon"], cwd=BASE_DIR,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                close_fds=True, env=clean_pyi_env())
        return True
    except Exception as e:
        log.error("守护启动失败: %r", e)
        return False


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_cmd():
    """注册表 Run 键里存放的启动命令。

    打包态直接运行自身 exe 的 daemon 子命令（--windowed 构建本身无控制台，
    不再需要 VBS 中转）。开发态仍是 pythonw + 脚本参数。
    """
    if getattr(sys, "frozen", False):
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
    # 前置校验：Run 键直接指向自身 exe，exe 必须真实存在。
    if getattr(sys, "frozen", False):
        if not os.path.exists(os.path.abspath(sys.executable)):
            log.error("安装开机自启失败: 当前 exe 路径不存在")
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
    """自启链路健康检查：Run 键 → exe（兼容旧版 VBS 启动器）文件完整性。

    返回 (ok, reason)：
    * 未启用自启 / Run 键读不到 → (True, "")（未安装不属异常，不打扰用户）
    * Run 键指向的 exe / vbs 缺失 → (False, 原因)。这正是"开机弹
      80070002 / 系统找不到指定的文件"报错的根因场景，前端据此显示红色警示条，
      引导用户重新启用自启，而不是等下次开机再弹窗。
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
        # 旧版通过 VBS 启动器自启：文件齐全即视为健康（启动时会被自动迁移）
        exe_path = os.path.join(os.path.dirname(ref), "CampusNetAuth.exe")
        if not os.path.exists(ref):
            return False, "开机自启指向的启动器已丢失：" + ref
        if not os.path.exists(exe_path):
            return False, ("开机自启将失败：启动器目录缺少 CampusNetAuth.exe，"
                           "请把 exe 与 " + DAEMON_VBS_NAME + " 放在同一文件夹，"
                           "或在界面重新启用开机自启")
        return True, ""
    if not os.path.exists(ref):
        return False, "开机自启指向的程序已丢失：" + ref
    return True, ""


def repair_autostart_cmd():
    """自愈 Run 键命令：迁移旧版 VBS 启动器 / 修正被搬走的程序路径。

    重写条件（其余一律不动作）：
    * Run 键指向的文件已不存在（目录被移动或删除）；
    * Run 键指向当前目录（旧版 wscript+VBS 或旧格式命令）→ 统一升级为
      `"<当前 exe>" daemon`。
    指向"另一份仍然存在的安装"时不动手：测试副本 / 多副本实例不得劫持
    用户真实安装的自启。仅打包态生效（开发态的 pythonw 路径不在此列）。
    """
    if not getattr(sys, "frozen", False):
        return False
    want = _autostart_cmd()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as k:
            try:
                value, _ = winreg.QueryValueEx(k, TASK_NAME)
            except FileNotFoundError:
                return False  # 未启用自启
        cur = (value or "").strip()
        if not cur or cur == want:
            return False
        m = re.search(r'"([^"]+)"', cur) or re.match(r"(\S+)", cur)
        ref = os.path.abspath(m.group(1).strip()) if m else ""
        same_dir = os.path.dirname(ref).lower() == os.path.abspath(BASE_DIR).lower()
        if ref and os.path.exists(ref) and not same_dir:
            return False  # 另一份仍然存在的安装，不动
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, TASK_NAME, 0, winreg.REG_SZ, want)
        log.info("开机自启命令已自动修正: %r -> %s", cur, want)
        return True
    except Exception as e:
        log.debug("自启命令自愈跳过: %r", e)
        return False


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
            try:
                cfg["portal_port"] = int(host.split(":")[1])
            except ValueError:
                print("! 端口 '%s' 不是数字，保持原端口 %s"
                      % (host.split(":")[1], cfg["portal_port"]))

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
    # 显式登录（CLI login）= 用户现在就要联网 → 立刻解除“手动下线”静默期，
    # 否则守护会继续按静默期拦住后续自动重连（界面「立即连接」走
    # clear_logout_pause()，路径不同但语义一致）。
    clear_logout_pause()
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
    # 任务计划场景必须"尊重"用户的手动下线：把刚下线的连接又登回去，
    # 会让「下线」在两个入口之间自相矛盾。显式要联网请用 login。
    left = logout_pause_left()
    if left:
        log.info("处于「手动下线」静默期（剩 %ds），本次跳过；执行 login 可提前恢复",
                 left)
        return 0
    cred = CredentialStore().load()
    if not cfg["username"] or not cred:
        log.error("尚未配置账号密码")
        return 1
    client = PortalClient(cfg)
    online, login_page = client.check_authentication()
    if online:
        log.info("已在线，无需操作")
        return 0
    if online is None:
        log.info("公网探测无结论，门户页面未满足认证条件，本次跳过")
        return 0
    res = client.login(cfg["username"], cred, login_page=login_page)
    if res.get("result") == "success":
        log.info("登录成功")
        return 0
    log.error("登录失败: %s", res.get("message", ""))
    return 2


def cmd_status(args):
    cfg = load_config()
    client = PortalClient(cfg)
    online, loc = client.check_online()
    # 探测无结论时补一句门户可达性：否则会把"探测不到公网"误报成"网络不可达"
    portal = client.portal_reachable() if online is None else None
    print("门户地址 : %s" % client.url(""))
    print("账号     : %s" % (cfg.get("username") or "(未配置)"))
    print("服务     : %s" % cfg.get("service"))
    print("网络状态 : %s" % network_state_text(online, portal))
    print("开机自启 : %s" % ("已启用" if autostart_status() else "未启用"))
    print("守护进程 : %s" % (
        "运行中(PID=%s)" % daemon_pid() if daemon_running() else "已停止"))
    if daemon_running():
        # 守护版本：换 exe 后老守护仍跑旧代码，这里一眼能看出来
        _ok, _dv, _dp, _dd = daemon_info()
        if _ok:
            _mark = "" if _dv == APP_VERSION else "  ← 旧版本，建议执行 upgrade"
            print("守护版本 : v%s%s" % (_dv, _mark))
        else:
            print("守护版本 : 未知（%s）" % _dd)
    print("凭据文件 : %s" % ("已存在" if os.path.exists(CRED_PATH) else "缺失"))
    _upd = get_update_info()
    _when = ("，检查于 " + _fmt_checked_at(_upd.get("checked_at"))
             if _upd.get("checked_at") else "")
    if not _upd.get("checked_at"):
        print("远程更新 : 未检查（执行 update 检查）")
    elif not _upd.get("ok"):
        print("远程更新 : 检查失败（%s%s）"
              % (_upd.get("error") or "未知", _when))
    elif _upd.get("available"):
        print("远程更新 : 发现 v%s（%s%s）"
              % (_upd.get("latest"), _upd.get("url"), _when))
    else:
        print("远程更新 : 已是最新（v%s%s）" % (_upd.get("latest"), _when))
    # 登录页兜底：网关不做 302 劫持时靠它自己拼登录页，填错了很难自查
    hints = cfg.get("login_page_hints") or {}
    if isinstance(hints, dict) and hints.get("nasip") and hints.get("wlanacname"):
        print("登录页兜底: 已配置 (wlanacname=%s nasip=%s)"
              % (hints["wlanacname"], hints["nasip"]))
    else:
        print("登录页兜底: 未配置")
    left = logout_pause_left()
    if left:
        print("下线静默 : 剩余 %ds（守护不会自动登回；「立即连接」或 login 可恢复）"
              % left)
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


def cmd_upgrade(args):
    """守护版本自愈 / 就地升级。

    两种用途：
      * `upgrade --check`：只体检——守护是不是当前版本、目录里有没有新版本，
        不改动任何文件；
      * `upgrade`：把目录里更高版本的 exe 就地换上来并重启守护。
        之所以需要程序自己动手：守护运行时 exe 被系统锁住（覆写报 WinError 32），
        用户手动替换会失败；而重命名是允许的，故用"旧文件让位"绕开。
    """
    if getattr(args, "check", False):
        stale, ver, reason = daemon_stale()
        print("当前版本 : %s" % APP_VERSION)
        if daemon_running():
            ok, dv, dp, dd = daemon_info()
            print("守护进程 : 运行中(PID=%s)" % daemon_pid())
            print("守护版本 : %s" % ("v%s" % dv if ok else "未知（%s）" % dd))
            print("版本一致 : %s" % ("是" if not stale else "否 —— %s" % reason))
        else:
            print("守护进程 : 已停止")
        up, up_ver = find_upgrade_candidate()
        print("升级候选 : %s" % ("%s（v%s）" % (os.path.basename(up), up_ver)
                                if up else "未发现（把新 exe 放进程序目录即可）"))
        # 远端最新版本（读上次检查结论，不发网络请求；新进程靠 state.json 预热）
        _upd = get_update_info()
        if _upd.get("ok") and _upd.get("checked_at"):
            print("远端最新 : v%s（%s；检查于 %s）" % (
                _upd.get("latest"),
                "有更新，执行 update 查看下载页" if _upd.get("available")
                else "已是最新",
                _fmt_checked_at(_upd.get("checked_at"))))
        else:
            print("远端最新 : 未检查（执行 update 检查 GitHub Release）")
        return 0

    ok, msg = apply_upgrade()
    # 用中文而非 ✓/✗：Windows 控制台代码页是 GBK 时，✓(U+2713) 无法编码，
    # 会在升级**已成功**之后抛 UnicodeEncodeError 打断输出（0.5.3 实测）
    print(("升级成功：" if ok else "升级失败：") + msg)
    return 0 if ok else 1


def cmd_update(args):
    """检查 GitHub 最新正式 Release（只检查，不下载、不替换文件）。"""
    info = check_latest_release(force=getattr(args, "force", False))
    print("当前版本 : %s" % APP_VERSION)
    if not info.get("ok"):
        print("检查失败 : %s" % (info.get("error") or "未知错误"))
        return 1
    print("最新版本 : %s" % (info.get("latest") or "(未知)"))
    if info.get("available"):
        print("可用更新 : 是%s" % (
            "（%s）" % info["name"] if info.get("name") else ""))
        print("下载页面 : %s" % info.get("url"))
        if getattr(args, "open", False):
            open_release_page()
    else:
        print("可用更新 : 否（已是最新）")
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

    def _after_logout():
        """下线成功后的统一收尾：清会话 + 进入“手动下线”静默期。

        不设静默期的话，守护 30s 内就会把你登回去（实测 14~36s），
        “下线”按钮等于没用。静默期长度取 config.json 的 logout_pause
        （默认 600s，0=关闭）；点「立即连接」或执行 login 会立刻解除。
        """
        clear_session()
        pause = int(cfg.get("logout_pause", 600) or 0)
        if pause > 0:
            set_logout_pause(pause)
            log.info("已进入手动下线静默期 %d 分钟：守护不会自动登回"
                     "（「立即连接」或 login 可提前恢复）", max(1, pause // 60))
        else:
            log.info("提示：logout_pause=0，守护可能很快自动重新登录")

    # 1) 持久化的 userIndex（守护每次登录都会写入 state.json）
    ui = load_state().get("userIndex")
    r = by_index(ui)
    if r is True:
        log.info("已下线（userIndex 方式）")
        _after_logout()
        return 0
    if r is not True:
        steps.append("userIndex 方式: " + safe_message(r))

    # 2) 在线时，门户会随会话返回当前 userIndex，取回后再下线
    info = client.interface("getOnlineUserInfo")
    if isinstance(info, dict) and info.get("userIndex"):
        r = by_index(info["userIndex"])
        if r is True:
            log.info("已下线（会话方式）")
            _after_logout()
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
            log.info("已下线（凭据方式）")
            _after_logout()
            return 0
        steps.append("凭据方式: " + safe_message(res))

    log.error("下线失败：%s", "；".join(steps) or safe_message(info))
    log.error("可打开 %s 在网页端手动注销", client.url("/"))
    return 2


def cmd_diagnose(args):
    """门户抓取诊断（常驻监控 + 自动取证）。

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
    ok, msg = enable_autostart_service()
    print(("已启用：" if ok else "启用失败：") + msg)
    return 0 if ok else 1


def cmd_uninstall(args):
    ok, msg = disable_autostart_service()
    print(("已关闭：" if ok else "关闭失败：") + msg)
    return 0 if ok else 1


def cmd_uninstaller(args):
    """释放内置卸载器并启动它（在它自己的窗口里确认后彻底清理）。

    ⚠️ 用 DETACHED_PROCESS 启动：卸载器会在确认后**连本进程一起收掉**
    （本进程就是程序目录里的 CampusNetAuth.exe），必须脱离父子关系，
    否则本进程被终止时它会被连带影响。
    """
    attach_console()
    if not ensure_uninstaller(force=True):
        print("内置卸载器不可用：打包时未打入 %s，请重新构建"
              % UNINSTALL_EXE_NAME)
        return 1
    exe = uninstaller_path()
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen([exe], cwd=BASE_DIR, creationflags=flags,
                         close_fds=True)
    except Exception as e:
        log.error("启动卸载器失败: %r", e)
        print("启动卸载器失败: %r" % (e,))
        return 1
    print("卸载程序已启动：%s" % exe)
    return 0


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
        # 每次界面启动重置 trace（模式 "w"）：该文件仅供本次运行的冒烟/
        # 排障使用，追加会让它无限增长；smoke 测试是启动后轮询读取，不受影响。
        with open(os.path.join(BASE_DIR, "_ui_trace.log"), "w",
                  encoding="utf-8") as f:
            f.write("cmd_ui entered argv=%r frozen=%s\n" %
                    (sys.argv, getattr(sys, "frozen", False)))
    except Exception:
        pass
    force_utf8_console()
    hide_console()  # 双击 exe 时消除黑窗；从 cmd 前台运行时不动父控制台
    repair_autostart_cmd()  # 目录被搬走/改名后自愈自启指向（静默，失败无感）
    # 「打开主 exe 即自动释放卸载器」：Windows 下运行中的 exe 无法删除自己，
    # 彻底卸载必须有独立的卸载程序。这里静默释放到 exe 同目录，失败无感
    # （不阻断界面启动）。
    try:
        ensure_uninstaller()
    except Exception as e:  # noqa: BLE001
        log.debug("释放卸载器失败: %r", e)
    # ---- HTML UI（pywebview）优先 ----
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
        log.error("请改用带 tkinter 的 Python 解释器重试"
                  "（python.org 官方安装包自带 tkinter）")
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
    # 无结论时用上面那次门户连通探测的结果（第 2 步刚测过，不重复发请求）
    print("当前状态    : %s" % (
        {True: "已在线", False: "未认证"}[online] if online is not None
        else network_state_text(None, portal=(r.status > 0))))

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

    # --version 放在 argparse 之前手写处理：frozen(--windowed) 下 stdout 需要
    # 先 attach_console 才能落到父控制台，argparse 的 action=version 会错过时机
    if len(sys.argv) == 2 and sys.argv[1] in ("--version", "-V", "version"):
        attach_console()
        print("CampusNetAuth " + APP_VERSION)
        return 0

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
    sub.add_parser("uninstaller",
                   help="释放并打开彻底卸载程序（清空本目录全部产物）"
                   ).set_defaults(func=cmd_uninstaller)
    p_diag = sub.add_parser("diagnose",
                            help="门户抓取诊断（持续监控 + 自动取证）")
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
    p_up = sub.add_parser("upgrade",
                          help="就地升级到程序目录里的新版本并重启守护")
    p_up.add_argument("--check", action="store_true",
                      help="只体检（守护版本 + 升级候选 + 上次远端检查结论），"
                           "不改动任何文件")
    p_up.set_defaults(func=cmd_upgrade)
    p_update = sub.add_parser("update",
                              help="检查 GitHub 最新 Release（只检查，不下载）")
    p_update.add_argument("--force", action="store_true",
                          help="跳过缓存，立即重新检查")
    p_update.add_argument("--open", action="store_true",
                          help="发现更新时用浏览器打开下载页面")
    p_update.set_defaults(func=cmd_update)
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

    # 清理上次就地升级留下的 .old.exe（18+ MB）。旧版只有 docstring 写了
    # “下次启动自动删”，实际从未调用 → 备份文件永久堆在程序目录（实测
    # 装完 4 天后仍在，且它本身就是一份可执行副本，杀软误报时会被当成
    # “另一份程序”）。放在日志初始化之后才能把结果写进日志。
    if getattr(sys, "frozen", False):
        cleanup_upgrade_backup()

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
