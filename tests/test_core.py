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


if __name__ == "__main__":
    unittest.main()
