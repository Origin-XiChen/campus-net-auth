#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试：配置凭据 -> 启动后台守护 -> 下线 -> 等待守护自动重连

设计要点：
  * 全程在本机窗口运行，断网期间脚本不会中断，结果写入 e2e_result.txt
  * 密码通过 getpass 输入（不回显、不落命令行历史）
用法： python e2e_test.py
"""
import os
import sys
import time
import socket
import getpass
import subprocess
import http.client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from campusnet import (  # noqa: E402
    load_config, save_config, CredentialStore, PortalClient,
    SINGLETON_PORT, LOG_PATH, http_request, setup_logger, log, UA,
    force_utf8_console, daemon_exe,
)

RESULT_FILE = os.path.join(BASE_DIR, "e2e_result.txt")
WAIT_TIMEOUT = 150
POLL_INTERVAL = 3


def say(msg=""):
    print(msg, flush=True)


def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def quick_online(timeout=5):
    """快速判断能否上外网（只测一个目标，避免断网时久等）"""
    r = http_request("GET", "www.msftconnecttest.com", 80,
                     "/connecttest.txt", timeout=timeout)
    return r.status == 200 and "Microsoft Connect Test" in r.text()


def main():
    force_utf8_console()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    lines = []

    def rec(msg=""):
        say(msg)
        lines.append(str(msg))

    rec("=" * 62)
    rec(" 校园网无感认证 —— 端到端测试")
    rec("=" * 62)

    cfg = load_config()
    setup_logger(to_console=False, level=cfg.get("log_level", "INFO"))

    # ---------- 1. 凭据 ----------
    cred = CredentialStore().load()
    if not cfg.get("username") or not cred:
        rec()
        rec("[1/5] 首次配置（密码输入不回显）")
        user = input("  账号(学号) [%s]: " % cfg.get("username", "")).strip()
        if user:
            cfg["username"] = user
        pwd = getpass.getpass("  密码: ")
        if not pwd:
            rec("  ! 密码为空，已取消")
            return 1
        if any(ord(c) > 127 for c in pwd):
            rec("  ! 警告：密码含非 ASCII 字符，可能无法与门户加密对齐")
        save_config(cfg)
        CredentialStore().save(pwd)
        rec("  已保存（服务=%s）" % cfg.get("service"))
    else:
        rec()
        rec("[1/5] 凭据已存在，跳过配置（账号=%s）" % cfg["username"])

    cfg = load_config()
    client = PortalClient(cfg)

    # ---------- 2. 启动守护 ----------
    rec()
    rec("[2/5] 启动后台守护进程")
    if port_in_use(SINGLETON_PORT):
        rec("  已有守护实例在运行，直接复用")
    else:
        # 开发态：pythonw（或其命名副本）跑 campusnet.py daemon
        pw = daemon_exe()
        script = os.path.join(BASE_DIR, "campusnet.py")
        try:
            subprocess.Popen([pw, script, "daemon"],
                             cwd=BASE_DIR,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                             close_fds=True)
            rec("  已启动: %s" % pw)
        except Exception as e:
            rec("  ! 守护启动失败: %r" % e)
            rec("  请改为手动运行: pythonw campusnet.py daemon")
            return 1
        time.sleep(3)
        if port_in_use(SINGLETON_PORT):
            rec("  守护已就绪（占用单例端口 %d）" % SINGLETON_PORT)
        else:
            rec("  ! 未检测到守护端口，可能启动失败，请查看 campusnet.log")

    # ---------- 3. 确认当前在线 ----------
    rec()
    rec("[3/5] 检查当前网络状态")
    if not quick_online():
        rec("  当前似乎已处于未认证状态，直接观察守护能否自动登录")
    else:
        rec("  当前已在线，可以进行下线测试")

    # ---------- 4. 下线 ----------
    rec()
    rec("[4/5] 准备下线")
    rec("  接下来网络会中断约 10-60 秒，守护进程会自动重新认证。")
    rec("  测试窗口请保持打开，不要关闭。")
    try:
        input("  按回车开始下线（Ctrl+C 可取消）… ")
    except KeyboardInterrupt:
        rec("\n  已取消")
        return 130

    from campusnet import cmd_logout
    cmd_logout(None)
    rec("  已发送下线请求")

    # ---------- 5. 等待自动重连 ----------
    rec()
    rec("[5/5] 等待守护自动重连（最多 %d 秒）" % WAIT_TIMEOUT)
    start = time.time()
    ok = False
    waited = 0
    while time.time() - start < WAIT_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        waited = int(time.time() - start)
        if quick_online(timeout=4):
            ok = True
            break
        # 进度点
        rec("  … 已等待 %3d 秒（网络尚未恢复）" % waited)

    rec()
    rec("=" * 62)
    if ok:
        rec(" 结果：成功 ✔  守护在约 %d 秒内自动完成重新认证" % waited)
        info = client.interface("getOnlineUserInfo",
                                {"userIndex": __import__("campusnet").load_state().get("userIndex", "")})
        if isinstance(info, dict) and info.get("result") == "success":
            rec("   姓名=%s  IP=%s  服务=%s  用户组=%s"
                % (info.get("userName"), info.get("userIp"),
                   info.get("service"), info.get("userGroup")))
    else:
        rec(" 结果：超时 ✘  %d 秒内网络未恢复" % WAIT_TIMEOUT)
        rec("   排查：查看 campusnet.log 末尾，确认失败原因")
        rec("   应急：打开 http://%s/ 在网页端手动登录" % cfg["portal_host"])
    rec("=" * 62)
    rec(" 日志文件: %s" % LOG_PATH)

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    rec()
    rec("结果已写入: %s" % RESULT_FILE)
    try:
        input("\n按回车关闭… ")
    except Exception:
        pass
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
