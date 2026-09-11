#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心逻辑离线单元测试（零网络、零第三方依赖，CI 可跑）。

RSA 黄金向量：明文取自 README「为什么可信」一节记录的 7 组 Node security.js
对拍用例；期望值由当前实现（此前已与官方 JS 逐字节对拍一致）生成并冻结，
用于防回归——任何对加密/反转/打包路径的改动都会立刻报警。
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import campusnet as cn  # noqa: E402

# 与 cmd_test 相同的固定 1024-bit 自检密钥
EXP = "10001"
MOD = ("9c2899b8ceddf9beafad2db8e431884a79fd9b9c881e459c0e1963984779d66"
       "12222cee814593cc458845bbba42b2d3474c10b9d31ed84f256c6e3a1c795e68"
       "e18585b84650076f122e763289a4bcb0de08762c3ceb591ec44d764a69817318"
       "fbce09d6ecb0364111f6f38e90dc44ca89745395a17483a778f1cc8dc990d87c3")

# (用例名, 明文, 期望密文)。明文即 README 表格中的 7 组对拍输入。
RSA_GOLDEN = [
    ("basic",
     "Test@123456>7AB3405C36D0",
     "53e81967e9134aceddaddce7eb27e4d41349fcda920dc3f282e5b6da45ddc8f1"
     "0c9dfa72da42fcebb35978d86291834b2bd68e42291d3b31a5099c33fab3bf14"
     "2f61b0b873f8e89bf87c4197988e08b158722ea016182b730797ec5014761e70"
     "52c2f2876bcda4d7a97c1d4948f2042fc448bdbc5aa3898bb6ad6e88da72947b"),
    ("empty_mac",
     "abc123>",
     "6b7e5d4294d176ce5bc23f35e18ec00f9bb1f9215b6094088e4d671aa51ae143"
     "30e71284bdb2d5626eaab702d21daf2dc73ea3e33b512717d7acf70b1a721878"
     "25d822311677c654d845b2a00e5ca137a8de0d4484d2639ad0d4c6f2799fb7a5"
     "c32dc0a092be1c88a40b83491862e52a5bca711c34f596adf15b6bfc8d2564d4"),
    ("single_char",
     "a",
     "2955aea7a5fb7ab2952139fedda5863de4b0e1f2c3b4c6d26567c40d1d169264"
     "f44bdf649d9a2cd44cf3a01ee57ca44118ed5057a456549ce9bc9136924977f4"
     "da3b736df9cfc621bc6c22a709d6d5de0fa7055ddeb615a92f4c114662d87616"
     "1dff6a28397dd14adf3051852c62c31e7f38053450da8961fea1d99c699dafa8"),
    ("numeric_user",
     "1234567890>7AB3405C36D0",
     "4e869191a3ab3722078f0ecf52efc5a773ad120a799239e9d849e5a188ee619e"
     "ec144f0e277b851047f94fe2c3eca7c191e532e887aacc7930ed6b83574fc3b3"
     "df6480497f756cacc01b063fa529a774f9c237b50826d6ed1bdc8837ff80c227"
     "f673f66ccdff67fddccbe78758ef78e69e0041af75faf4580f0e2e76c1002210"),
    ("symbols",
     "P@ssw0rd!#$%^&*()>00-11-22-33-44-55",
     "98539f063fff57d44a9cb00525df4ddd42b64dc6e7b4c3eccebad70506ba55d914"
     "c27be9c3b82b099af856965775965273dd0ee19a6e4a445365e4587739e9e00115"
     "dd4e1c50295ae96b4d8758871501e08105a7f87229bb01432db0a1957df780540"
     "98d045a08c214c57ab0a22b3803337223ae0a312598c83b6fae78ca8838"),
    ("one_full_block_no_pad",
     "x" * 126,
     "4c253531a57c8b910edf89625d27b806386b42747c77002fbee56a0ba86f7ab69b"
     "4a9530689c1676b1505b99d31018b9c310261e0e6fe5773b362c444eeb3097a0d4"
     "5a24cbba77a0e68ce750a31ed0c7209f4ce23332478f1c5989b8d1850df59b666c"
     "3eb16aeb4e8fbaa80d6d091d6265fd4d521fbc9ea46ff4fbf25f0a512c"),
    ("cross_block",
     "y" * 127,
     "6d9b351bbe1f0d9e02eb8478b2338f88b615cd34b27afbc3c4b40bc75f8c2b2e19"
     "ee15def3686baf36e8af44cd1128fed87c098c52d9a4bc35a8cf2493202e537a03"
     "8d6644987df86402ba30b956526b7d8cd4962384c4d508c0f1ea0dacdc4009fabe"
     "4a63576b4c4bfa829883072be99ac07d3d72d4ebb29491bfde402710de "
     "17fa478ad8f0ca48d433a38e43798071e5c101b57f3e7a5fc4f80cfaad56489741"
     "ecdea38f3764bcb7c8b813158fa795736639f550289f03b8d136005e7ef0739181"
     "91c7f5d9fd110cdd61b0b9b6ae4b2735d08dec3d6e01eef3254d2ff135bfe2c250"
     "0734331f1252b885a76940dfd5c0bebfbbf69faeb1f3868f811d0ed725"),
]


class TestRsaEncrypt(unittest.TestCase):
    def test_golden_vectors(self):
        """7 组黄金向量逐字节比对（登录链路最脆弱的环节）。"""
        for name, plain, expected in RSA_GOLDEN:
            with self.subTest(case=name):
                got = cn.eportal_rsa_encrypt(cn.reverse_str(plain), EXP, MOD)
                self.assertEqual(got, expected)

    def test_block_structure(self):
        """1024-bit 密钥：每块 256 hex；126 字符=1 块，127 字符=2 块。"""
        one = cn.eportal_rsa_encrypt("x" * 126, EXP, MOD)
        two = cn.eportal_rsa_encrypt("y" * 127, EXP, MOD)
        self.assertEqual(len(one.split(" ")), 1)
        self.assertEqual(len(one), 256)
        self.assertEqual(len(two.split(" ")), 2)
        for blk in two.split(" "):
            self.assertEqual(len(blk), 256)

    def test_empty_modulus_rejected(self):
        with self.assertRaises(ValueError):
            cn.eportal_rsa_encrypt("a", EXP, "0")


class TestUtf16Reverse(unittest.TestCase):
    def test_units_bmp(self):
        self.assertEqual(cn._utf16_code_units("ab中"), [0x61, 0x62, 0x4E2D])

    def test_units_surrogate_pair(self):
        # 😀 = U+1F600 → JS UTF-16 代理对 D83D DE00
        self.assertEqual(cn._utf16_code_units("😀"), [0xD83D, 0xDE00])

    def test_reverse_matches_py_for_bmp(self):
        s = "abc中文123!@#"
        self.assertEqual(cn.reverse_str(s), s[::-1])

    def test_reverse_splits_surrogates_like_js(self):
        # JS split("").reverse() 会拆开代理对；Python s[::-1] 不会
        got = cn.reverse_str("a😀b")
        self.assertEqual(got, "b" + chr(0xDE00) + chr(0xD83D) + "a")
        self.assertNotEqual(got, "a😀b"[::-1])


class TestEncodingHelpers(unittest.TestCase):
    def test_dq_double_encodes(self):
        self.assertEqual(cn.dq("a b"), "a%2520b")
        self.assertEqual(cn.dq("10001"), "10001")

    def test_mask_user(self):
        self.assertEqual(cn.mask_user("1234567890"), "12******90")
        self.assertEqual(cn.mask_user("1234"), "1234")
        self.assertEqual(cn.mask_user(""), "")

    def test_safe_message(self):
        self.assertEqual(cn.safe_message({"message": "m"}), "m")
        self.assertEqual(cn.safe_message({"_raw": "r"}), "r")
        self.assertEqual(cn.safe_message({}, default="d"), "d")
        self.assertEqual(cn.safe_message(None, default="d"), "d")

    def test_parse_inputs(self):
        html = ('<input type="hidden" id="publicKeyExponent" value="10001">'
                '<input type="text" id="userId" value="">'
                '<input name="passwordEncrypt" value="true">')
        fields = cn.parse_inputs(html)
        self.assertEqual(fields["publicKeyExponent"], "10001")
        self.assertEqual(fields["passwordEncrypt"], "true")
        self.assertEqual(fields["userId"], "")


class TestCookieJar(unittest.TestCase):
    def test_roundtrip_and_delete(self):
        jar = cn.CookieJar()
        jar.update("h", 80, ["JSESSIONID=abc; Path=/", "x=1"])
        self.assertEqual(jar.get("h", 80), "JSESSIONID=abc; x=1")
        jar.update("h", 80, ["x=deleted; Max-Age=0"])
        self.assertEqual(jar.get("h", 80), "JSESSIONID=abc")
        self.assertIsNone(jar.get("other", 80))


class _StateDirMixin(unittest.TestCase):
    """把 state/config 路径重定向到仓库根下的临时文件。

    不用 %TEMP%（某些沙箱拦截其删除）也不造深层子目录（沙箱只放行
    仓库根下 1~2 层的新文件），直接放 _test_state/_test_config 前缀文件。"""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pid = os.getpid()
        self._state_file = os.path.join(root, "_test_state_%d.json" % pid)
        self._config_file = os.path.join(root, "_test_config_%d.json" % pid)
        self._patches = {
            "STATE_PATH": self._state_file,
            "CONFIG_PATH": self._config_file,
        }
        self._saved = {k: getattr(cn, k) for k in self._patches}
        for k, v in self._patches.items():
            setattr(cn, k, v)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(cn, k, v)
        # 通配清理本用例 PIＤ 命名的所有临时文件：除了 state/config 本体，
        # 临时换位的 .exe / .new.exe / .old.exe 也在这里兜底（沙箱下
        # os.remove 可能被 shim 拦截，失败就静默跳过，不影响测试结论）。
        root = os.path.dirname(self._state_file)
        base = os.path.basename(self._state_file)[:-len(".json")]
        for name in os.listdir(root):
            if not name.startswith(base):
                continue
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for p in (getattr(self, "_state_file", ""),
                  getattr(self, "_config_file", "")):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


class TestStateStore(_StateDirMixin):
    def test_update_state_merges_and_drops(self):
        cn.save_state({"first_run_at": "t0", "last_error": {"msg": "x"}})
        cn.update_state({"userIndex": "u1"}, drop=("last_error",))
        st = cn.load_state()
        self.assertEqual(st["first_run_at"], "t0")   # 合并写保留旧字段
        self.assertEqual(st["userIndex"], "u1")
        self.assertNotIn("last_error", st)           # drop 生效

    def test_save_state_atomic_no_tmp_left(self):
        cn.save_state({"a": 1})
        self.assertFalse(os.path.exists(cn.STATE_PATH + ".tmp"))
        with open(cn.STATE_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 1})

    def test_record_event_dedup_and_ring(self):
        for _ in range(3):
            cn.record_event("login_fail", "同一错误")
        evs = cn.load_state()["events"]
        self.assertEqual(len(evs), 1)                # 60s 内同 kind+msg 合并
        for i in range(20):
            cn.record_event("login_fail", "错误%d" % i)
        evs = cn.load_state()["events"]
        self.assertEqual(len(evs), 8)                # 环形缓冲上限 8

    def test_clear_session_keeps_deploy_marker(self):
        cn.save_state({"first_run_at": "t0", "userIndex": "u", "loginTime": "l"})
        cn.clear_session()
        st = cn.load_state()
        self.assertEqual(st["first_run_at"], "t0")
        self.assertNotIn("userIndex", st)


class TestSanitizeConfig(_StateDirMixin):
    def test_bad_types_fall_back(self):
        with open(cn.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"interval": -5, "timeout": "abc",
                       "detect_targets": "not-a-list",
                       "pin_pubkey": "false", "portal_host": ""}, f)
        cfg = cn.load_config()
        self.assertGreaterEqual(cfg["interval"], 5)
        self.assertEqual(cfg["timeout"], cn.DEFAULT_CONFIG["timeout"])
        self.assertIsInstance(cfg["detect_targets"], list)
        self.assertTrue(cfg["detect_targets"])
        self.assertTrue(cfg["pin_pubkey"])           # 字符串 "false" 不误判
        self.assertEqual(cfg["portal_host"],
                         cn.DEFAULT_CONFIG["portal_host"])


# ---------------- 门户公钥 pinning（mock 网络，离线）----------------

def _login_html(exp, mod, encrypt="true"):
    return ('<html><body>'
            '<input type="hidden" id="publicKeyExponent" value="%s">'
            '<input type="hidden" id="publicKeyModulus" value="%s">'
            '<input type="hidden" id="passwordEncrypt" value="%s">'
            '</body></html>' % (exp, mod, encrypt))


_LOGIN_URL = ("http://172.16.54.18/eportal/index.jsp?"
              "mac=AABBCCDDEEFF&wlanuserip=1.2.3.4")


class TestPubkeyPinning(_StateDirMixin):
    def _client(self, pin=True):
        cfg = dict(cn.DEFAULT_CONFIG)
        cfg["pin_pubkey"] = pin
        return cn.PortalClient(cfg)

    def _mock_net(self, client, html, resp_body=b'{"result":"success",'
                                                 b'"userIndex":"u1"}'):
        """mock 登录页发现 + 登录 POST；返回 http_request mock 供断言。"""
        m_disc = mock.patch.object(client, "discover_login_page",
                                   return_value=(_LOGIN_URL, html))
        m_http = mock.patch.object(cn, "http_request",
                                   return_value=cn.HttpResponse(
                                       200, {}, resp_body, _LOGIN_URL))
        return m_disc, m_http

    def test_first_success_pins_fingerprint(self):
        client = self._client()
        m_disc, m_http = self._mock_net(client, _login_html(EXP, "AABBCC"))
        with m_disc, m_http:
            res = client.login("user", "pw")
        self.assertEqual(res.get("result"), "success")
        expect = hashlib.sha256(b"10001:aabbcc").hexdigest()
        self.assertEqual(cn.load_state().get("portal_pubkey_fp"), expect)

    def test_mismatch_refuses_before_sending(self):
        cn.save_state({"portal_pubkey_fp": "0" * 64})  # 与页面公钥不符
        client = self._client()
        m_disc, m_http = self._mock_net(client, _login_html(EXP, "AABBCC"))
        with m_disc, m_http as m:
            res = client.login("user", "pw")
        self.assertEqual(res.get("result"), "fail")
        self.assertIn("指纹", res.get("message", ""))
        m.assert_not_called()  # 关键：凭据根本没有发出去

    def test_no_encrypt_with_pin_refused(self):
        cn.save_state({"portal_pubkey_fp": "0" * 64})
        client = self._client()
        m_disc, m_http = self._mock_net(
            client, _login_html(EXP, "AABBCC", encrypt="false"))
        with m_disc, m_http as m:
            res = client.login("user", "pw")
        self.assertEqual(res.get("result"), "fail")
        m.assert_not_called()

    def test_pin_disabled_proceeds(self):
        cn.save_state({"portal_pubkey_fp": "0" * 64})
        client = self._client(pin=False)
        m_disc, m_http = self._mock_net(client, _login_html(EXP, "AABBCC"))
        with m_disc, m_http as m:
            res = client.login("user", "pw")
        self.assertEqual(res.get("result"), "success")
        self.assertTrue(m.called)


class TestCheckOnline(unittest.TestCase):
    """check_online 的在线判定：严格 expect 目标才有决定权（防透明代理误判）"""

    T_STRICT = {"host": "msft", "path": "/ct", "expect": "Microsoft"}
    T_LOOSE = {"host": "baidu", "path": "/", "expect": ""}

    @staticmethod
    def _resp(status, body=b"", headers=None):
        return cn.HttpResponse(status, headers or {}, body, "http://mock/")

    def _client(self, targets):
        cfg = {"portal_host": "172.16.54.18", "portal_port": 80,
               "detect_targets": targets}
        return cn.PortalClient(cfg)

    def _with_net(self, targets, responses):
        """responses: {host: HttpResponse}；按 host 分发 mock http_request"""
        def fake(method, host, port, path, **kw):
            return responses[host]
        client = self._client(targets)
        with mock.patch.object(cn, "http_request", side_effect=fake):
            return client.check_online()

    def test_strict_match_is_online(self):
        r = self._with_net(
            [self.T_STRICT, self.T_LOOSE],
            {"msft": self._resp(200, b"Microsoft Connect Test OK"),
             "baidu": self._resp(200, b"<html>whatever</html>")})
        self.assertEqual(r, (True, None))

    def test_loose_200_not_decisive_with_strict_present(self):
        # 透明代理：所有 host 都回 200，但严格目标内容不符 → 不得判在线
        r = self._with_net(
            [self.T_STRICT, self.T_LOOSE],
            {"msft": self._resp(200, b"proxy interstitial page"),
             "baidu": self._resp(200, b"<html>fake 200</html>")})
        self.assertEqual(r, (False, None))

    def test_loose_only_keeps_legacy_any_200(self):
        # 兜底：配置里没有任何严格 expect 目标时，保留"任意 200 即在线"
        r = self._with_net([self.T_LOOSE],
                           {"baidu": self._resp(200, b"<html></html>")})
        self.assertEqual(r, (True, None))

    def test_portal_redirect_reports_hijack(self):
        r = self._with_net(
            [self.T_LOOSE],
            {"baidu": self._resp(302, b"",
                                  {"Location": "http://172.16.54.18/eportal/"})})
        self.assertEqual(r[0], False)
        self.assertIn("172.16.54.18", r[1])

    def test_all_unreachable_is_none(self):
        r = self._with_net(
            [self.T_STRICT],
            {"msft": self._resp(-1, b"err")})
        self.assertEqual(r, (None, None))


class TestStaleSessionRecovery(unittest.TestCase):
    """残留会话自愈（学校门户偶发：登录返回 success 但会话未生效）。

    现场症状：认证"成功"却上不了网、账号页有效期显示 1970 之类异常值，
    守护若直接重登会陷入无效死循环 → 必须先 logout 清掉残留会话。
    """

    def test_logout_ok_passes_user_index_and_cred(self):
        calls = {}

        class FakePortal:
            def logout(self, ui=None, force_by_cred=False,
                       username=None, password=None):
                calls.update(ui=ui, force=force_by_cred, user=username)
                return {"result": "success", "message": "注销成功"}

        ok, detail = cn.logout_stale_session(
            FakePortal(), "IDX123", "2510331221", "pw")
        self.assertTrue(ok)
        self.assertEqual(calls["ui"], "IDX123")
        self.assertTrue(calls["force"])
        self.assertIn("注销成功", detail)

    def test_logout_fail_result_reported(self):
        class FakePortal:
            def logout(self, *a, **kw):
                return {"result": "fail", "message": "用户已下线"}

        ok, detail = cn.logout_stale_session(FakePortal(), None, "u", "p")
        self.assertFalse(ok)
        self.assertIn("已下线", detail)

    def test_logout_exception_is_swallowed(self):
        class FakePortal:
            def logout(self, *a, **kw):
                raise RuntimeError("portal boom")

        ok, detail = cn.logout_stale_session(FakePortal(), None, "u", "p")
        self.assertFalse(ok)
        self.assertIn("boom", detail)

    def test_logout_uses_cred_fallback_when_no_index(self):
        """没有 userIndex 时必须走"账号密码"兜底下线，否则残留会话清不掉。"""
        seen = {}

        class FakePortal:
            def logout(self, ui=None, force_by_cred=False,
                       username=None, password=None):
                seen.update(ui=ui, force=force_by_cred,
                            username=username, password=password)
                return {"result": "success", "message": "ok"}

        cn.logout_stale_session(FakePortal(), None, "2510331221", "secret")
        self.assertIsNone(seen["ui"])
        self.assertTrue(seen["force"])
        self.assertEqual(seen["username"], "2510331221")
        self.assertEqual(seen["password"], "secret")


class TestSystemNotify(unittest.TestCase):
    """右下角系统通知：去重与派发（不真正弹窗，线程被替换为假实现）。"""

    def setUp(self):
        cn._notify_last.clear()

    def tearDown(self):
        cn._notify_last.clear()

    @unittest.skipUnless(sys.platform.startswith("win"), "通知实现仅 Windows")
    def test_dedup_same_content_within_window(self):
        made = []

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None):
                made.append(args)

            def start(self):
                pass

        with mock.patch.object(cn, "threading",
                               mock.Mock(Thread=FakeThread)):
            r1 = cn.notify_system("t1", "m1")
            r2 = cn.notify_system("t1", "m1")
            r3 = cn.notify_system("t1", "m2")
        self.assertTrue(r1)
        self.assertFalse(r2, "同内容 60s 内应去重")
        self.assertTrue(r3, "不同内容应放行")
        self.assertEqual(len(made), 2)

    @unittest.skipUnless(sys.platform.startswith("win"), "通知实现仅 Windows")
    def test_dispatch_failure_returns_false(self):
        with mock.patch.object(cn.threading, "Thread",
                               side_effect=RuntimeError("no thread")):
            self.assertFalse(cn.notify_system("t", "m"))


class TestNotifyDaemonTest(unittest.TestCase):
    """守护通道通知测试（界面不在时的通知链路，走 47667 控制通道 + 回执）。"""

    def test_no_daemon_returns_false(self):
        with mock.patch.object(cn.socket, "create_connection",
                               side_effect=ConnectionRefusedError()):
            ok, msg = cn.notify_daemon_test()
        self.assertFalse(ok)
        self.assertIn("守护未运行", msg)

    def test_sends_notify_and_accepts_ack(self):
        sent = []

        class FakeSock:
            def sendall(self, b):
                sent.append(b)

            def recv(self, n):
                return b"OK"

            def close(self):
                pass

        with mock.patch.object(cn.socket, "create_connection",
                               return_value=FakeSock()):
            ok, msg = cn.notify_daemon_test()
        self.assertTrue(ok)
        self.assertEqual(sent, [b"NOTIFY"])
        self.assertIn("已发送", msg)

    def test_missing_ack_reports_false(self):
        class FakeSock:
            def sendall(self, b):
                pass

            def recv(self, n):
                raise TimeoutError("no ack")

            def close(self):
                pass

        with mock.patch.object(cn.socket, "create_connection",
                               return_value=FakeSock()):
            ok, msg = cn.notify_daemon_test()
        self.assertFalse(ok)
        self.assertIn("未回执", msg)

    def test_unexpected_ack_reports_false(self):
        class FakeSock:
            def sendall(self, b):
                pass

            def recv(self, n):
                return b"??"

            def close(self):
                pass

        with mock.patch.object(cn.socket, "create_connection",
                               return_value=FakeSock()):
            ok, msg = cn.notify_daemon_test()
        self.assertFalse(ok)
        self.assertIn("回执异常", msg)


class TestDaemonVersionHandshake(unittest.TestCase):
    """守护版本握手：换 exe 后仍在跑的旧守护必须能被识别出来。

    背景：守护 = 同一 exe + daemon 参数，用户升级只换文件，已运行的守护
    仍是旧代码；而且守护运行时 exe 被系统锁住（覆写报 WinError 32）。
    """

    def test_parse_ver_ack_ok(self):
        ver, path = cn._parse_ver_ack("OK 0.5.0\tC:\\app\\CampusNetAuth.exe\n")
        self.assertEqual(ver, "0.5.0")
        self.assertEqual(path, "C:\\app\\CampusNetAuth.exe")

    def test_parse_ver_ack_tolerates_missing_path(self):
        ver, path = cn._parse_ver_ack("OK 0.5.0")
        self.assertEqual(ver, "0.5.0")
        self.assertEqual(path, "")

    def test_parse_ver_ack_rejects_garbage(self):
        for bad in ("", "   ", "??", "OK", "OK "):
            self.assertEqual(cn._parse_ver_ack(bad), (None, None), bad)

    def test_ver_key_orders_numerically(self):
        # 字符串比较会把 "0.10.0" 排在 "0.9.0" 前面，这里必须是数值序
        self.assertLess(cn._ver_key("0.9.0"), cn._ver_key("0.10.0"))
        self.assertGreater(cn._ver_key("1.0"), cn._ver_key("0.99.99"))
        self.assertEqual(cn._ver_key("bad"), ())

    def test_no_daemon_is_not_stale(self):
        with mock.patch.object(cn, "daemon_running", return_value=False):
            stale, ver, reason = cn.daemon_stale()
        self.assertFalse(stale)
        self.assertIsNone(ver)
        self.assertIn("未运行", reason)

    def test_matching_version_is_fresh(self):
        with mock.patch.object(cn, "daemon_running", return_value=True), \
             mock.patch.object(cn, "daemon_info",
                               return_value=(True, cn.APP_VERSION,
                                             cn.self_exe_path(), "")):
            stale, ver, reason = cn.daemon_stale()
        self.assertFalse(stale)
        self.assertEqual(ver, cn.APP_VERSION)
        self.assertEqual(reason, "")

    def test_older_version_is_stale(self):
        with mock.patch.object(cn, "daemon_running", return_value=True), \
             mock.patch.object(cn, "daemon_info",
                               return_value=(True, "0.1.0", "", "")):
            stale, ver, reason = cn.daemon_stale()
        self.assertTrue(stale)
        self.assertEqual(ver, "0.1.0")
        self.assertIn("≠", reason)

    def test_other_path_same_version_is_stale(self):
        """版本相同但跑的是另一份程序（多份部署并存）也要重启。"""
        with mock.patch.object(cn, "daemon_running", return_value=True), \
             mock.patch.object(cn, "daemon_info",
                               return_value=(True, cn.APP_VERSION,
                                             "c:\\other\\campusnetauth.exe", "")):
            stale, ver, reason = cn.daemon_stale()
        self.assertTrue(stale)
        self.assertIn("另一份程序", reason)

    def test_silent_daemon_without_state_record_is_stale(self):
        """旧版守护不认识 VER → 无回执；它也不写 daemon_version → 判陈旧。"""
        with mock.patch.object(cn, "daemon_running", return_value=True), \
             mock.patch.object(cn, "daemon_info",
                               return_value=(False, None, None, "无回执")), \
             mock.patch.object(cn, "load_state", return_value={}):
            stale, ver, reason = cn.daemon_stale()
        self.assertTrue(stale)
        self.assertIsNone(ver)

    def test_silent_daemon_with_matching_state_record_is_fresh(self):
        """守护没来得及回话（正忙于探测），但落盘记录显示就是当前版本 → 不动它。"""
        with mock.patch.object(cn, "daemon_running", return_value=True), \
             mock.patch.object(cn, "daemon_info",
                               return_value=(False, None, None, "无回执")), \
             mock.patch.object(cn, "load_state",
                               return_value={"daemon_version": cn.APP_VERSION}):
            stale, ver, _reason = cn.daemon_stale()
        self.assertFalse(stale)
        self.assertEqual(ver, cn.APP_VERSION)

    def test_daemon_info_no_daemon(self):
        with mock.patch.object(cn.socket, "create_connection",
                               side_effect=ConnectionRefusedError()):
            ok, ver, path, detail = cn.daemon_info()
        self.assertFalse(ok)
        self.assertIsNone(ver)
        self.assertIn("未运行", detail)

    def test_daemon_info_reads_ack(self):
        class FakeSock:
            def sendall(self, b):
                self.sent = b

            def recv(self, n):
                return b"OK 0.5.0\tC:\\app\\CampusNetAuth.exe\n"

            def close(self):
                pass

        with mock.patch.object(cn.socket, "create_connection",
                               return_value=FakeSock()):
            ok, ver, path, _ = cn.daemon_info()
        self.assertTrue(ok)
        self.assertEqual(ver, "0.5.0")
        self.assertEqual(path, "C:\\app\\CampusNetAuth.exe")

    def test_daemon_info_empty_reply_means_old_daemon(self):
        """旧守护收到不认识的 VER 会直接关连接 → recv 拿到 EOF（空串）。"""
        class FakeSock:
            def sendall(self, b):
                pass

            def recv(self, n):
                return b""

            def close(self):
                pass

        with mock.patch.object(cn.socket, "create_connection",
                               return_value=FakeSock()):
            ok, _ver, _path, detail = cn.daemon_info()
        self.assertFalse(ok)
        self.assertIn("旧版守护", detail)


class TestDaemonRefreshGuard(unittest.TestCase):
    """自动重启守护的守卫：只能停不能起时，宁可不做也不能把守护搞没。"""

    def test_nothing_to_do_when_fresh(self):
        with mock.patch.object(cn, "daemon_stale",
                               return_value=(False, None, "")):
            restarted, reason = cn.ensure_daemon_fresh(force=True)
        self.assertFalse(restarted)
        self.assertEqual(reason, "")

    def test_refuses_when_component_missing(self):
        """值守组件缺失 → 停掉就再也起不来，必须拒绝动手。"""
        with mock.patch.object(cn, "daemon_stale",
                               return_value=(True, "0.1.0", "版本不符")), \
             mock.patch.object(cn, "daemon_component_installed",
                               return_value=False), \
             mock.patch.object(cn, "stop_daemon") as stop:
            restarted, reason = cn.ensure_daemon_fresh(force=True)
        self.assertFalse(restarted)
        self.assertEqual(reason, "版本不符")
        stop.assert_not_called()

    def test_restarts_when_stale(self):
        with mock.patch.object(cn, "daemon_stale",
                               return_value=(True, "0.1.0", "版本不符")), \
             mock.patch.object(cn, "daemon_component_installed",
                               return_value=True), \
             mock.patch.object(cn, "stop_daemon", return_value=True), \
             mock.patch.object(cn, "start_daemon", return_value=True), \
             mock.patch.object(cn, "record_event") as ev:
            restarted, _reason = cn.ensure_daemon_fresh(force=True)
        self.assertTrue(restarted)
        self.assertEqual(ev.call_args[0][0], "daemon_refresh")

    def test_reports_failure_when_start_fails(self):
        with mock.patch.object(cn, "daemon_stale",
                               return_value=(True, "0.1.0", "版本不符")), \
             mock.patch.object(cn, "daemon_component_installed",
                               return_value=True), \
             mock.patch.object(cn, "stop_daemon", return_value=True), \
             mock.patch.object(cn, "start_daemon", return_value=False):
            restarted, _reason = cn.ensure_daemon_fresh(force=True)
        self.assertFalse(restarted)

    def test_cooldown_blocks_second_restart(self):
        cn._STALE_REFRESH["last"] = 0.0
        with mock.patch.object(cn, "daemon_stale",
                               return_value=(True, "0.1.0", "版本不符")), \
             mock.patch.object(cn, "daemon_component_installed",
                               return_value=True), \
             mock.patch.object(cn, "stop_daemon", return_value=True), \
             mock.patch.object(cn, "start_daemon", return_value=True), \
             mock.patch.object(cn, "record_event"), \
             mock.patch.object(cn.time, "time", return_value=1000.0):
            first, _ = cn.ensure_daemon_fresh()
            second, reason = cn.ensure_daemon_fresh()
        self.assertTrue(first)
        self.assertFalse(second)          # 冷却期内不再重复重启
        self.assertIn("冷却", reason)


class TestUpgradeHelper(_StateDirMixin):
    """就地升级：解决"守护运行时 exe 被锁，用户手动替换会失败"。"""

    def test_dev_mode_has_no_candidate(self):
        with mock.patch.object(cn.sys, "frozen", False, create=True):
            self.assertEqual(cn.find_upgrade_candidate(), (None, None))

    def test_dev_mode_refuses_apply(self):
        with mock.patch.object(cn.sys, "frozen", False, create=True):
            ok, msg = cn.apply_upgrade()
        self.assertFalse(ok)
        self.assertIn("开发态", msg)

    def test_pe_version_missing_file(self):
        self.assertEqual(cn.pe_version(self._state_file + ".nope"),
                         (None, None))

    def test_pe_version_non_pe_file(self):
        """普通文本文件不是 PE，必须安全返回而不是抛异常。"""
        self.assertEqual(cn.pe_version(cn.__file__), (None, None))

    def test_apply_rejects_lower_version(self):
        """构造一个"版本不高于当前"的候选文件 → 必须拒绝（不能自我降级）。"""
        fake = self._state_file + ".exe"
        with open(fake, "wb") as f:
            f.write(b"MZ" + b"\x00" * 512)
        try:
            with mock.patch.object(cn.sys, "frozen", True, create=True), \
                 mock.patch.object(cn, "pe_version",
                                   return_value=("0.0.1", "CampusNetAuth")):
                ok, msg = cn.apply_upgrade(fake)
            self.assertFalse(ok)
            self.assertIn("不高于", msg)
        finally:
            if os.path.exists(fake):
                os.remove(fake)

    def test_apply_rejects_foreign_product(self):
        fake = self._state_file + ".exe"
        with open(fake, "wb") as f:
            f.write(b"MZ" + b"\x00" * 512)
        try:
            with mock.patch.object(cn.sys, "frozen", True, create=True), \
                 mock.patch.object(cn, "pe_version",
                                   return_value=("9.9.9", "SomeOtherApp")):
                ok, msg = cn.apply_upgrade(fake)
            self.assertFalse(ok)
            self.assertIn("不是 CampusNetAuth", msg)
        finally:
            if os.path.exists(fake):
                os.remove(fake)

    def test_cleanup_backup_noop_when_absent(self):
        with mock.patch.object(cn, "upgrade_backup_path",
                               return_value=self._state_file + ".old.exe"):
            self.assertFalse(cn.cleanup_upgrade_backup())

    def test_apply_swaps_files_and_keeps_backup(self):
        """核心文件换位：旧 exe → .old.exe 让位，新 exe → 原位置。

        之所以不是直接覆写：守护/界面在跑时 exe 被系统锁定（覆写报
        WinError 32），而**重命名是允许的**。
        """
        cur = self._state_file + ".exe"          # 假装是"当前程序"
        new = self._state_file + ".new.exe"      # 候选新版本
        bak = cur + cn.UPGRADE_BACKUP_SUFFIX
        with open(cur, "wb") as f:
            f.write(b"OLD-BINARY")
        with open(new, "wb") as f:
            f.write(b"NEW-BINARY")
        try:
            with mock.patch.object(cn.sys, "frozen", True, create=True), \
                 mock.patch.object(cn, "self_exe_path", return_value=cur), \
                 mock.patch.object(cn, "upgrade_backup_path",
                                   return_value=bak), \
                 mock.patch.object(cn, "daemon_running", return_value=False), \
                 mock.patch.object(cn, "pe_version",
                                   return_value=("9.9.9", "CampusNetAuth")), \
                 mock.patch.object(cn, "record_event"):
                ok, msg = cn.apply_upgrade(new)
            self.assertTrue(ok, msg)
            self.assertEqual(open(cur, "rb").read(), b"NEW-BINARY")
            self.assertEqual(open(bak, "rb").read(), b"OLD-BINARY")
            self.assertFalse(os.path.exists(new))   # 候选已移动，不留副本
            self.assertIn("9.9.9", msg)
        finally:
            for p in (cur, new, bak):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass

    def test_apply_rolls_back_when_second_move_fails(self):
        """换位第二步失败时必须把旧文件放回去，不能把用户撂在"没有 exe"。"""
        cur = self._state_file + ".exe"
        new = self._state_file + ".new.exe"
        bak = cur + cn.UPGRADE_BACKUP_SUFFIX
        with open(cur, "wb") as f:
            f.write(b"OLD-BINARY")
        with open(new, "wb") as f:
            f.write(b"NEW-BINARY")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(a, b):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(32, "in use")
            return real_replace(a, b)

        try:
            with mock.patch.object(cn.sys, "frozen", True, create=True), \
                 mock.patch.object(cn, "self_exe_path", return_value=cur), \
                 mock.patch.object(cn, "upgrade_backup_path",
                                   return_value=bak), \
                 mock.patch.object(cn, "daemon_running", return_value=False), \
                 mock.patch.object(cn, "pe_version",
                                   return_value=("9.9.9", "CampusNetAuth")), \
                 mock.patch.object(cn.os, "replace",
                                   side_effect=flaky_replace):
                ok, _msg = cn.apply_upgrade(new)
            self.assertFalse(ok)
            self.assertTrue(os.path.exists(cur), "旧文件必须被放回原位")
            self.assertEqual(open(cur, "rb").read(), b"OLD-BINARY")
        finally:
            for p in (cur, new, bak):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
