#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行时卫生（0.6.1）：配置校验 / 手动下线静默期 / 验证码拦截 / 累计计数 /
按网卡取 MAC / 进程归属措辞。全部离线，不联网、不碰真实状态文件。
"""
import os
import unittest
from unittest import mock

import campusnet as cn
from tests.test_core import _StateDirMixin
from tests.test_portal_recovery import DaemonReplay


# ---------------- 配置校验 ----------------

class TestSanitizeConfig(_StateDirMixin):
    def san(self, **kw):
        cfg = dict(cn.DEFAULT_CONFIG)
        cfg.update(kw)
        return cn._sanitize_config(cfg)

    def test_service_normalised_or_falls_back(self):
        self.assertEqual(self.san(service="yd")["service"], "YD")
        self.assertEqual(self.san(service=" Auto ")["service"], "auto")
        self.assertEqual(self.san(service="DEFAULT")["service"], "default")
        # 写错的值（比如写成中文）原样 POST 给门户只会换来含糊的报错
        self.assertEqual(self.san(service="移动")["service"], "default")
        self.assertEqual(self.san(service=None)["service"], "default")

    def test_portal_base_gets_leading_slash(self):
        # 漏斜杠会拼出 http://host:80eportal/InterFace.do?... 这种发不出去的地址
        self.assertEqual(self.san(portal_base="eportal")["portal_base"],
                         "/eportal")
        self.assertEqual(self.san(portal_base="/eportal/")["portal_base"],
                         "/eportal")
        self.assertEqual(self.san(portal_base="/")["portal_base"], "/eportal")
        self.assertEqual(self.san(portal_base="")["portal_base"], "/eportal")

    def test_username_stripped(self):
        self.assertEqual(self.san(username="  2311711106 ")["username"],
                         "2311711106")
        self.assertEqual(self.san(username=None)["username"], "")

    def test_logout_pause_clamped(self):
        self.assertEqual(self.san(logout_pause=-5)["logout_pause"], 0)
        self.assertEqual(self.san(logout_pause="900")["logout_pause"], 900)
        self.assertEqual(self.san(logout_pause=10 ** 9)["logout_pause"], 86400)
        self.assertEqual(self.san(logout_pause="abc")["logout_pause"],
                         cn.DEFAULT_CONFIG["logout_pause"])


# ---------------- 手动下线静默期 ----------------

class TestLogoutPause(_StateDirMixin):
    def test_roundtrip(self):
        self.assertEqual(cn.logout_pause_left(), 0)
        cn.set_logout_pause(600)
        self.assertGreater(cn.logout_pause_left(), 500)
        cn.set_logout_pause(0)
        self.assertEqual(cn.logout_pause_left(), 0)

    def test_expired_pause_is_zero(self):
        cn.update_state({cn.LOGOUT_PAUSE_KEY: 1})     # 1970 年就过期了
        self.assertEqual(cn.logout_pause_left(), 0)

    def test_logout_writes_pause_and_login_clears_it(self):
        cfg = dict(cn.DEFAULT_CONFIG, logout_pause=600)
        client = mock.Mock()
        client.logout.return_value = {"result": "success"}
        client.url.return_value = "http://portal.test/"
        with mock.patch.object(cn, "load_config", return_value=cfg), \
                mock.patch.object(cn, "CredentialStore") as store, \
                mock.patch.object(cn, "PortalClient", return_value=client), \
                mock.patch.object(cn, "load_state",
                                  return_value={"userIndex": "TEST-ONLY"}), \
                mock.patch.object(cn, "clear_session"):
            store.return_value.load.return_value = "pw"
            self.assertEqual(cn.cmd_logout(mock.Mock()), 0)
        self.assertGreater(cn.logout_pause_left(), 500)

        # 「立即连接」/CLI login = 用户现在要联网 → 立刻解除
        cfg2 = dict(cfg, username="u")
        client2 = mock.Mock()
        client2.check_online.return_value = (True, None)
        with mock.patch.object(cn, "load_config", return_value=cfg2), \
                mock.patch.object(cn, "CredentialStore") as store, \
                mock.patch.object(cn, "PortalClient", return_value=client2):
            store.return_value.load.return_value = "pw"
            cn.cmd_login(mock.Mock(force=False))
        self.assertEqual(cn.logout_pause_left(), 0)


class TestDaemonHonoursLogoutPause(unittest.TestCase):
    def test_pause_blocks_login_until_it_expires(self):
        replay = DaemonReplay(commands=[None, "STOP"])
        # 第一轮：静默期内（120s）→ 不登录；第二轮：已解除 → 正常认证
        with mock.patch.object(cn, "logout_pause_left",
                               side_effect=[120, 0, 0]):
            self.assertEqual(replay.run(), 0)
        replay.client.login.assert_called_once()
        # 第一段等待按 min(静默剩余, 检测周期) 走，不会一口气睡 120s
        self.assertEqual(replay.waits, [30, 20])


# ---------------- 验证码 / 短信 / CAS 拦截 ----------------

class TestAuthBlocked(unittest.TestCase):
    def client(self):
        return cn.PortalClient(dict(cn.DEFAULT_CONFIG,
                                    portal_host="portal.test",
                                    detect_targets=[]))

    def test_reasons(self):
        cases = (
            ({"validCodeUrl": "/eportal/validcode?rnd=1"},
             "验证码"),
            ({"isCheckSmsAuth": "true"}, "短信"),
            ({"isToCasPage": "true"}, "CAS"),
        )
        for info, expect in cases:
            with self.subTest(info=info):
                c = self.client()
                with mock.patch.object(c, "page_info", return_value=info):
                    self.assertIn(expect, c.auth_blocked_reason("qs"))

    def test_normal_portal_not_blocked(self):
        c = self.client()
        with mock.patch.object(c, "page_info",
                               return_value={"validCodeUrl": "",
                                             "isCheckSmsAuth": "false",
                                             "isToCasPage": "false"}):
            self.assertEqual(c.auth_blocked_reason("qs"), "")
        with mock.patch.object(c, "page_info", return_value=None):
            self.assertEqual(c.auth_blocked_reason("qs"), "")

    def test_login_failure_marks_blocked(self):
        c = self.client()
        url = c.url("/eportal/index.jsp?wlanuserip=1.2.3.4&mac=SYN")
        html = ('<html><input id="pwd"><input id="passwordEncrypt" '
                'value="false"></html>')
        with mock.patch.object(c, "discover_login_page",
                               return_value=(url, html)), \
                mock.patch.object(cn, "http_request",
                                  return_value=cn.HttpResponse(
                                      200, {}, b'{"result":"fail",'
                                               b'"message":"ERR_BAD"}', url)), \
                mock.patch.object(c, "auth_blocked_reason",
                                  return_value="门户登录页已启用验证码，无法自动认证"):
            res = c.login("u", "p")
        self.assertEqual(res["result"], "fail")
        self.assertIn("验证码", res["_blocked"])
        self.assertIn("验证码", res["_hint"])

    def test_daemon_notifies_once_and_backs_off_long(self):
        replay = DaemonReplay(commands=[None, "STOP"])
        replay.client.login.return_value = {
            "result": "fail", "message": "x",
            "_blocked": "门户登录页已启用验证码，无法自动认证"}
        self.assertEqual(replay.run(), 0)
        self.assertEqual(replay.notifications.call_count, 1)
        # 长退避：不再按 30s 敲门户（max(max_interval, 1800)）
        self.assertTrue(all(w >= 1800 for w in replay.waits), replay.waits)


# ---------------- 累计计数落盘 ----------------

class TestCumulativeStats(_StateDirMixin):
    def test_totals_are_persisted_across_processes(self):
        cn._STATS_WRITE_AT = 0.0
        self.assertTrue(cn.save_stats_totals({"login_ok": 1, "login_fail": 0}))
        self.assertEqual(cn.load_state()["stats"]["login_ok"], 1)
        # 落盘的累计值不依赖进程：重新读出来就是 1（重启守护不会归零）
        self.assertTrue(cn.save_stats_totals({"login_ok": 2, "bad": "x"},
                                            min_gap=0))
        self.assertEqual(cn.load_state()["stats"], {"login_ok": 2})

    def test_write_is_throttled(self):
        cn._STATS_WRITE_AT = 0.0
        self.assertTrue(cn.save_stats_totals({"login_ok": 1}))
        self.assertFalse(cn.save_stats_totals({"login_ok": 2}))
        self.assertEqual(cn.load_state()["stats"]["login_ok"], 1)


# ---------------- 按网卡取 MAC ----------------

class TestMacByInterface(unittest.TestCase):
    def test_pick_mac_for_ip(self):
        ifaces = [("10.9.1.2", "74-3a-f4-ca-f3-eb"),
                  ("192.168.1.5", "aa-bb-cc-dd-ee-01")]
        self.assertEqual(cn.pick_mac_for_ip(ifaces, "10.9.1.2"),
                         "74-3a-f4-ca-f3-eb")
        self.assertEqual(cn.pick_mac_for_ip(ifaces, "10.0.0.9"), "")
        self.assertEqual(cn.pick_mac_for_ip([], "10.9.1.2"), "")
        self.assertEqual(cn.pick_mac_for_ip(None, "10.9.1.2"), "")
        self.assertEqual(cn.pick_mac_for_ip(ifaces, ""), "")

    def test_login_page_query_prefers_interface_mac(self):
        cfg = cn._sanitize_config(dict(
            cn.DEFAULT_CONFIG, portal_host="portal.test", detect_targets=[],
            login_page_hints={"wlanacname": "logic", "nasip": "10.253.0.17"}))
        client = cn.PortalClient(cfg)
        with mock.patch.object(cn, "local_lan_ipv4", return_value="10.9.1.2"), \
                mock.patch.object(cn, "local_mac_for_ip",
                                  return_value="74-3a-f4-ca-f3-eb") as by_ip, \
                mock.patch.object(cn, "local_mac_text",
                                  return_value="74-3a-f4-ca-f3-ec") as by_uuid:
            qs = client.login_page_query()
        by_ip.assert_called_once_with("10.9.1.2")
        by_uuid.assert_not_called()
        self.assertIn("wlanparameter=74-3a-f4-ca-f3-eb", qs)

    def test_falls_back_to_uuid_when_interface_unknown(self):
        cfg = cn._sanitize_config(dict(
            cn.DEFAULT_CONFIG, portal_host="portal.test", detect_targets=[],
            login_page_hints={"wlanacname": "logic", "nasip": "10.253.0.17"}))
        client = cn.PortalClient(cfg)
        with mock.patch.object(cn, "local_lan_ipv4", return_value="10.9.1.2"), \
                mock.patch.object(cn, "local_mac_for_ip", return_value=""), \
                mock.patch.object(cn, "local_mac_text",
                                  return_value="74-3a-f4-ca-f3-ec"):
            qs = client.login_page_query()
        self.assertIn("wlanparameter=74-3a-f4-ca-f3-ec", qs)

    def test_enumeration_never_raises(self):
        # 真机/CI 结果都可能是空列表，但绝不能抛
        self.assertIsInstance(cn._enum_ipv4_ifaces(), list)


# ---------------- 进程归属措辞 / 升级备份清理 ----------------

class TestProcessOwnership(unittest.TestCase):
    def test_unknown_ownership_is_not_reported_as_pid_reuse(self):
        with mock.patch.object(cn, "_proc_alive", return_value=True), \
                mock.patch.object(cn, "_own_exe_path", return_value="/x.exe"), \
                mock.patch.object(cn, "_proc_is_own", return_value=None), \
                mock.patch.object(cn, "_kill_pid") as kill, \
                mock.patch.object(cn, "log") as log:
            self.assertFalse(cn._kill_if_own(4242, "守护"))
        kill.assert_not_called()
        log.warning.assert_not_called()          # 不是“PID 被复用”
        self.assertTrue(log.debug.called)

    def test_confirmed_foreign_process_warns(self):
        with mock.patch.object(cn, "_proc_alive", return_value=True), \
                mock.patch.object(cn, "_own_exe_path", return_value="/x.exe"), \
                mock.patch.object(cn, "_proc_is_own", return_value=False), \
                mock.patch.object(cn, "_kill_pid") as kill, \
                mock.patch.object(cn, "log") as log:
            self.assertFalse(cn._kill_if_own(4242, "守护"))
        kill.assert_not_called()
        self.assertIn("不属于本程序", log.warning.call_args.args[0])

    def test_upgrade_backup_is_removed(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "CampusNetAuth.exe.old.exe")
        with open(path, "wb") as f:
            f.write(b"OLD")
        try:
            with mock.patch.object(cn, "upgrade_backup_path",
                                   return_value=path):
                self.assertTrue(cn.cleanup_upgrade_backup())
            self.assertFalse(os.path.exists(path))
            # 已经不存在 → 幂等返回 False，不抛
            with mock.patch.object(cn, "upgrade_backup_path",
                                   return_value=path):
                self.assertFalse(cn.cleanup_upgrade_backup())
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            os.rmdir(d)


if __name__ == "__main__":
    unittest.main()
