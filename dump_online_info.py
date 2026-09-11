# -*- coding: utf-8 -*-
"""门户在线信息完整抓取（一次性诊断脚本）。

用途：打印 getOnlineUserInfo 的**完整** JSON 响应，确认学校门户返回的
"过期时间"字段名与异常值（现象：登录成功但显示 1970 年过期，导致认证无效）。

在 **iHBUT 校园网内**运行：
    python _t_online_info_dump.py

脚本只读不写：不修改配置、不删文件；若当前未在线会用已保存的账号尝试一次登录
（用于复现"登录成功但过期时间异常"的场景）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campusnet as cn  # noqa: E402

# 疑似"过期/有效期"字段的关键字（大小写不敏感）
TIME_HINTS = ("expire", "expired", "valid", "deadline", "endtime", "end_date",
              "enddate", "deadline", "accountfee", "fee", "time", "date")
# 1970-01-01 附近 / 明显早于现在的秒级时间戳下限（2020-01-01）
TS_MIN = 1577836800


def scan_time_fields(obj, prefix=""):
    """递归扫描所有字段，标注时间类字段与可疑值。"""
    rows = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = ("%s.%s" % (prefix, k)) if prefix else str(k)
            kl = str(k).lower()
            if isinstance(v, (dict, list)):
                rows += scan_time_fields(v, path)
                continue
            suspect = False
            note = ""
            if any(h in kl for h in TIME_HINTS):
                suspect = True
                s = str(v).strip()
                if s in ("", "None", "null"):
                    note = "空值"
                else:
                    try:
                        num = float(s)
                        if num <= 0:
                            note = "非正数（<=0）→ 会渲染成 1970-01-01"
                        elif num < TS_MIN:
                            note = "时间戳过早（<2020-01-01）"
                        elif num > 32503680000:
                            note = "毫秒级时间戳"
                        else:
                            note = "疑似正常（%s）" % time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime(num))
                    except ValueError:
                        if "1970" in s or "1900" in s:
                            note = "字符串含 1970/1900 → 渲染异常"
            if suspect or note:
                rows.append((path, str(v)[:60], note))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            rows += scan_time_fields(v, "%s[%d]" % (prefix, i))
    return rows


def main():
    relogin = "--relogin" in sys.argv
    cfg = cn.load_config()
    client = cn.PortalClient(cfg)
    cred = cn.CredentialStore().load()

    print("=" * 62)
    print("门户      : %s" % client.url(""))
    print("账号      : %s" % (cfg.get("username") or "(未配置)"))
    print("当前时间  : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 62)

    online, loc = client.check_online()
    print("外网在线  : %s（location=%s）" % (online, loc))

    ui = cn.load_state().get("userIndex")
    print("state.userIndex: %s" % (ui or "(无)"))

    if relogin:
        print("\n[--relogin] 先主动下线，再重新登录以复现完整链路")
        ok, msg = cn.logout_stale_session(client, ui, cfg["username"], cred)
        print("  下线: %s / %s" % (ok, msg))
        time.sleep(1)

    if relogin or not online:
        print("\n[尝试登录]")
        res = client.login(cfg["username"], cred)
        brief = {k: v for k, v in res.items() if k != "userIndex"}
        print("登录响应（已去 userIndex，长度 %d）:" % len(brief))
        print(json.dumps(brief, ensure_ascii=False, indent=2, default=str)[:2000])
        print("返回 userIndex: %s" % (res.get("userIndex") or "(无)"))
        print("\n>>> login 响应时间字段扫描 <<<")
        for path, val, note in scan_time_fields(res):
            print("  %-28s = %-24s %s" % (path, val, note))
        ui = res.get("userIndex") or ui
        time.sleep(2)

    print("\n" + "=" * 62)
    print("getOnlineUserInfo 完整响应")
    print("=" * 62)
    info = client.interface("getOnlineUserInfo",
                           {"userIndex": ui} if ui else None)
    print(json.dumps(info, ensure_ascii=False, indent=2,
                     default=str)[:4000])

    print("\n" + "=" * 62)
    print("时间类字段扫描（怀疑点）")
    print("=" * 62)
    rows = scan_time_fields(info)
    if not rows:
        print("(未发现时间类字段)")
    for path, val, note in rows:
        flag = "⚠ " if ("1970" in note or "非正数" in note or "过早" in note
                        or "空值" in note or "1900" in note) else "  "
        print("%s%-28s = %-24s %s" % (flag, path, val, note))

    print("\n提示：把以上完整输出（尤其『时间类字段扫描』段）一并反馈，")
    print("      即可确定判断规则与字段名。")


if __name__ == "__main__":
    main()
