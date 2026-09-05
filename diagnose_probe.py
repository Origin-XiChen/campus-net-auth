# -*- coding: utf-8 -*-
"""
diagnose_probe.py — 门户状态持续监控 + 认证过程自动抓取

用途：启动后**常驻运行**，每隔 N 秒轮询一次门户（iHBUT 锐捷 ePortal），
持续记录状态变化（可达/不可达、在线/未认证），并在以下时机自动取证：

  * 出现"未认证"状态时 → 抓取登录页证据（RSA 公钥/模数、passwordEncrypt、
    queryString/mac、input 字段、Set-Cookie）
  * 检测到"未认证 → 已认证"转换（即你手动完成认证）→ 抓取会话证据
    （getOnlineUserInfo 无参/带参对比、userIndex、用户名/IP/套餐），
    并**自动输出完整报告** capture_result.json + capture_report.txt

它不登录、不碰密码，只在一旁观察门户。认证由你手动完成（浏览器打开
门户登录页登录，或使用你自己的方式）。

用法：
  python diagnose_probe.py                  # 持续监控，认证完成后出报告并退出
  python diagnose_probe.py --keep           # 出报告后继续监控（Ctrl+C 停止）
  python diagnose_probe.py --interval 3     # 轮询间隔（秒，默认 5）
  python diagnose_probe.py --max-wait 60    # 最长等待认证（分钟，默认 30；0=无限）
  python diagnose_probe.py --dry 3          # 只跑 3 轮就输出当前报告（内部自测用）

安全约定：
  * 全程不登录、不读取/写入密码，输出文件绝不包含任何凭据
  * 只用假密码探测 logoutByUserIdAndPass 参数名，正常会被拒绝
"""
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import campusnet  # noqa: E402
from campusnet import (PortalClient, force_utf8_console, load_config,
                       load_state)  # noqa: E402

# ⚠️ 报告路径必须落在用户熟悉的"exe 同目录/probe/"下，而不是 PyInstaller
# onefile 的解压临时目录（%TEMP%\_MEIxxxxxx）。campusnet.BASE_DIR 在 frozen
# 下就是 exe 同目录（见 _base_dir()）；这里保持一致。
OUT_DIR = os.path.join(campusnet.BASE_DIR, "probe")
try:
    os.makedirs(OUT_DIR, exist_ok=True)
except Exception:
    pass
OUT_JSON = os.path.join(OUT_DIR, "capture_result.json")
OUT_TXT = os.path.join(OUT_DIR, "capture_report.txt")

# ------------------------- 插桩 -------------------------

EVENTS = []
_SENSITIVE_KEYS = ("password", "operatorPwd")


def _sanitize_body(body):
    """去掉表单体中的敏感字段值，避免密码落盘"""
    if not body:
        return None
    text = body if isinstance(body, str) else body.decode("utf-8", "replace")
    for key in _SENSITIVE_KEYS:
        text = re.sub(r"(?i)(%s=[^&]*)" % re.escape(key), key + "=***", text)
    return text[:400]


def _summarize_headers(hdrs):
    keys = ("set-cookie", "location", "content-type", "content-length",
            "server", "date", "connection")
    out = {}
    for k, v in hdrs.items():
        if k.lower() in keys:
            out[k] = v
    return out


def _req_headers(headers):
    out = {}
    for k, v in (headers or {}).items():
        if k.lower() in ("referer", "cookie", "content-type",
                         "x-requested-with", "user-agent"):
            out[k] = v
    return out


_orig_http = campusnet.http_request


def _traced(method, host, port, path, headers=None, body=None, timeout=8,
            cookie_jar=None):
    r = _orig_http(method, host, port, path, headers=headers, body=body,
                   timeout=timeout, cookie_jar=cookie_jar)
    EVENTS.append({
        "ts": datetime.datetime.now().strftime("%H:%M:%S"),
        "method": method,
        "url": "http://%s:%d%s" % (host, port, path)[:180],
        "req_headers": _req_headers(headers),
        "req_body": _sanitize_body(body),
        "status": r.status,
        "resp_headers": _summarize_headers(r.headers),
        "resp_body": r.text()[:300],
    })
    return r


campusnet.http_request = _traced

# ------------------------- 取证 -------------------------

STATE_NAMES = {True: "在线", False: "未认证", None: "网络不可达"}
TIMELINE = []


def _stamp():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _last_event_headers(pred):
    """取最近一条满足条件的 HTTP 事件的响应头"""
    for e in reversed(EVENTS):
        if pred(e):
            return e
    return None


def capture_login_page(client):
    """未认证时抓取登录页证据（Q1/Q5）"""
    url, html = client.discover_login_page()
    if not html:
        return None
    fields = campusnet.parse_inputs(html)
    ev = _last_event_headers(
        lambda e: e["method"] == "GET" and
        ("publicKeyExponent" in e["resp_body"] or "pwd" in e["resp_body"]))
    return {
        "url": url,
        "passwordEncrypt": fields.get("passwordEncrypt", ""),
        "publicKeyExponent": fields.get("publicKeyExponent", ""),
        "publicKeyModulus": fields.get("publicKeyModulus", ""),
        "inputs": sorted(fields.keys()),
        "queryString": (url.split("?", 1)[1] if "?" in url else ""),
        "set_cookie": (ev or {}).get("resp_headers", {}).get("Set-Cookie", ""),
    }


def capture_online_session(client, user_index):
    """在线时抓取会话证据（Q4）"""
    noarg = client.interface("getOnlineUserInfo")
    withidx = None
    if user_index:
        withidx = client.interface("getOnlineUserInfo", {"userIndex": user_index})
    return {"noarg": noarg, "with_userIndex": withidx,
            "userIndex": user_index}


def probe_logout_param(client, username):
    """Q7：假密码分别试 'pass' / 'password'"""
    fake = "PROBE_INVALID_PASSWORD_8x"
    r1 = client.interface("logoutByUserIdAndPass",
                          {"userId": username, "pass": fake})
    r2 = client.interface("logoutByUserIdAndPass",
                          {"userId": username, "password": fake})
    return {"with_pass": r1, "with_password": r2}


def _brief(obj):
    if obj is None:
        return "None"
    if isinstance(obj, dict):
        if "_raw" in obj:
            return "(非 JSON) %s" % obj["_raw"]
        keep = {k: v for k, v in obj.items()
                if k not in ("password", "operatorPwd")}
        return json.dumps(keep, ensure_ascii=False)[:160]
    return str(obj)[:160]


# ------------------------- 报告 -------------------------


def build_report(meta, login_page, session, logout_param, timeline):
    L = []
    A = L.append
    A("=" * 62)
    A("CampusNetAuth 门户抓取报告（持续监控）")
    A("=" * 62)
    A("时间      : %s" % meta["time"])
    A("门户      : %s" % meta["portal"])
    A("账号      : %s" % meta["username_masked"])
    A("服务      : %s" % meta["service"])
    A("观察时长  : %s" % meta["duration"])
    A("认证方式  : 手动（脚本未登录、未接触密码）")
    A("")

    A("[1] 状态时间线")
    for t in timeline:
        A("  %s  %s -> %s%s" % (t["ts"], t["from"], t["to"],
                                ("  「%s」" % t["note"]) if t.get("note") else ""))
    A("")

    A("[2] 登录页证据（认证前抓取，Q1/Q5）")
    if login_page:
        lp = login_page
        A("  URL       : %s" % lp["url"])
        A("  queryString: %s" % lp["queryString"][:140])
        A("  Set-Cookie: %s" % (lp["set_cookie"] or "(无 —— 门户未种 Cookie)"))
        A("  passwordEncrypt = %s" % lp["passwordEncrypt"])
        A("  publicKeyExponent = %s" % lp["publicKeyExponent"][:40])
        A("  publicKeyModulus  = %s..." % lp["publicKeyModulus"][:40])
        A("  input 字段: %s" % ", ".join(sorted(lp["inputs"])))
    else:
        A("  （监控期间未观察到未认证状态，未抓到登录页）")
    A("")

    A("[3] 认证后会话证据（Q4）")
    if session:
        s = session
        A("  userIndex: %s" % (s["userIndex"] or "(无)"))
        A("  getOnlineUserInfo 无参          : %s" % _brief(s["noarg"]))
        A("  getOnlineUserInfo 带 userIndex  : %s"
          % _brief(s["with_userIndex"]))
    else:
        A("  （未观察到在线会话）")
    A("")

    A("[4] logoutByUserIdAndPass 参数名（Q7）")
    if logout_param:
        A("  用 'pass'     : %s" % _brief(logout_param["with_pass"]))
        A("  用 'password' : %s" % _brief(logout_param["with_password"]))
        A("  → 若 'pass' 返回『用户名或密码错误』而 'password' 为空响应，"
          "则参数名是 pass（现有代码正确）。")
    A("")

    A("[5] 关键 HTTP 事件明细（%d 条，仅列门户相关）" % len(EVENTS))
    shown = 0
    for i, e in enumerate(EVENTS, 1):
        if not ("InterFace.do" in e["url"] or "eportal" in e["url"]
                or e["method"] != "POST"):
            continue
        shown += 1
        A("  #%02d %s %s" % (i, e["method"], e["url"]))
        A("      status=%s" % e["status"])
        if e["req_headers"]:
            A("      req_hdrs: %s" % json.dumps(e["req_headers"],
                                                ensure_ascii=False))
        if e["req_body"]:
            A("      req_body: %s" % e["req_body"])
        if e["resp_headers"]:
            A("      resp_hdrs: %s" % json.dumps(e["resp_headers"],
                                                 ensure_ascii=False))
        if e["resp_body"]:
            A("      body: %s" % e["resp_body"].replace("\n", " "))
    if shown == 0:
        A("  （无门户接口请求被记录）")
    A("")
    A("=" * 62)
    A("说明：本报告不包含密码。认证由你手动完成，脚本全程未登录。")
    return "\n".join(L)


# ------------------------- 主循环 -------------------------


def main(argv=None):
    force_utf8_console()
    interval = 5
    max_wait_min = 30
    keep = False
    dry = 0
    args = sys.argv[1:] if argv is None else argv
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--keep":
            keep = True
        elif a == "--interval" and i + 1 < len(args):
            interval = max(1, int(args[i + 1]))
            i += 1
        elif a == "--max-wait" and i + 1 < len(args):
            max_wait_min = int(args[i + 1])
            i += 1
        elif a == "--dry" and i + 1 < len(args):
            dry = int(args[i + 1])
            i += 1
        i += 1

    cfg = load_config()
    client = PortalClient(cfg)
    username = cfg.get("username", "")
    state = load_state()

    meta = {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "portal": "http://%s:%d%s" % (cfg.get("portal_host"),
                                      int(cfg.get("portal_port", 80)),
                                      cfg.get("portal_base", "/eportal")),
        "username_masked": (username[:3] + "***" + username[-2:]) if username
        else "(未配置)",
        "service": cfg.get("service"),
        "duration": "",
    }

    print("=" * 62)
    print("CampusNetAuth 门户监控已启动（每 %ds 轮询）" % interval)
    print("  请手动完成认证：浏览器打开门户登录页登录即可。")
    print("  检测到 未认证 -> 已认证 后，将自动输出报告。")
    print("  按 Ctrl+C 可随时停止。")
    print("=" * 62)

    start = time.time()
    last_state = None          # (reachable, online)
    login_page = None
    session = None
    logout_param = None
    auth_seen = False
    rounds = 0
    last_beat = time.time()

    # 启动时若已在线，提示需要先下线才能观察认证过程
    reachable0 = client.portal_reachable()
    online0, _loc0 = client.check_online()
    if online0 is True and dry == 0:
        print("[提示] 当前已在线，无法观察认证过程。请先下线（")
        print("        python campusnet.py logout 或门户页面注销），")
        print("        再运行本脚本并手动重新认证。")

    try:
        while True:
            rounds += 1
            reachable = client.portal_reachable()
            online, loc = client.check_online()
            cur = (reachable, online)

            if cur != last_state:
                from_name = STATE_NAMES.get(
                    last_state[1], "?") if last_state else "(开始)"
                to_name = STATE_NAMES.get(online, "?")
                note = ""
                if online is False:
                    print("  [%s] 检测到未认证，正在抓取登录页证据…"
                          % _stamp())
                    try:
                        lp = capture_login_page(client)
                        if lp:
                            login_page = lp
                            note = "已抓取登录页证据"
                        else:
                            note = "未抓取到登录页"
                    except Exception as e:
                        note = "抓取登录页异常: %r" % e
                if online is True:
                    ui = (session or {}).get("userIndex") or state.get(
                        "userIndex")
                    try:
                        s = capture_online_session(client, ui)
                        session = s
                        note = "已抓取会话证据"
                        if last_state and last_state[1] is not True:
                            auth_seen = True
                            note += " ★ 检测到认证完成"
                    except Exception as e:
                        note = "抓取会话异常: %r" % e
                    if logout_param is None and username:
                        logout_param = probe_logout_param(client, username)
                        note += " | 已探测 logout 参数名"
                elif online is None:
                    note = "网络不可达"
                TIMELINE.append({"ts": _stamp(), "from": from_name,
                                 "to": to_name, "note": note})
                print("  %s  %s -> %s  %s" % (_stamp(), from_name, to_name,
                                              note))
                last_state = cur

                if auth_seen and not keep:
                    break

            # 心跳：状态长期不变时定期提示，避免看起来像卡死
            if time.time() - last_beat >= 15:
                last_beat = time.time()
                print("  [心跳 %s] 仍在监控，当前：%s。等待手动认证中…"
                      "（Ctrl+C 停止）" % (_stamp(),
                                          STATE_NAMES.get(online, "?")))

            if dry and rounds >= dry:
                break
            if not dry and max_wait_min > 0 and \
                    (time.time() - start) > max_wait_min * 60:
                print("[超时] 等待 %d 分钟未检测到认证，输出当前状态报告"
                      % max_wait_min)
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[停止] 收到中断，输出当前状态报告")

    meta["duration"] = "%d 秒 / %d 轮" % (int(time.time() - start), rounds)

    payload = {
        "meta": meta,
        "timeline": TIMELINE,
        "login_page": login_page,
        "session": session,
        "logout_param": logout_param,
        "events": EVENTS,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    report = build_report(meta, login_page, session, logout_param, TIMELINE)
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write(report)

    print()
    print("=" * 62)
    print("报告已输出：")
    print("  %s" % OUT_JSON)
    print("  %s" % OUT_TXT)
    if auth_seen:
        print("已检测到认证完成，请把 capture_result.json 发给 AI 分析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
