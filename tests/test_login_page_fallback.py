#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录页兜底发现（0.5.4）。

背景：有的校园网关在未认证时不做 302 劫持（未认证阶段 DNS 直接被黑洞），
于是"跟随重定向拿登录页"这条路走不通；而锐捷 ePortal 只在请求带上认证设备
参数（wlanuserip / wlanacname / nasip / wlanparameter / url）时才下发学生
登录页，其它情况只回 `alert('WEB认证设备未注册…')` 或后台管理台页面。
这里覆盖三条兜底规则：自拼 queryString、拒绝管理台页面、密码加密参数改按
pageInfo 补齐。全部离线，不联网。
"""
import urllib.parse
import unittest
from unittest import mock

import campusnet as cn
from tests.test_core import EXP, MOD, _login_html, _StateDirMixin

# 与锐捷 ePortal 新版登录页同形：加密开关/公钥都留空，真值由页面 JS 从
# pageInfo 拉下来再填（真实的 172.16.54.18 登录页就是这么给的）
_PLAIN_LOGIN_HTML = (
    '<html><body><form name="loginForm">'
    '<input id="username" name="username" type="text" value="">'
    '<input id="pwd" name="pwd" type="password" value="">'
    '<input id="passwordEncrypt" name="passwordEncrypt" value="false" type="hidden">'
    '<input id="publicKeyExponent" name="publicKeyExponent" value="" type="hidden">'
    '<input id="publicKeyModulus" name="publicKeyModulus" value="" type="hidden">'
    '</form></body></html>')

# 门户根 /eportal/ 上的后台管理登录页（form action=./admin.do?method=login）
_ADMIN_HTML = (
    '<html><body><form name="loginForm" method="post" '
    'action="./admin.do?method=login">'
    '<input id="username" name="username" type="text" value="">'
    '<input id="pwd" name="pwd" type="password" value="">'
    '</form></body></html>')

_NOT_REGISTERED_HTML = (
    "<script>alert('WEB认证设备未注册，请确认SAM+/portal/设备上的参数是否一致');"
    "</script>")


class TestLoginPageHtmlDetection(unittest.TestCase):
    def test_student_login_page_is_recognised(self):
        self.assertTrue(cn.looks_like_eportal_login(_login_html(EXP, MOD)))
        self.assertTrue(cn.looks_like_eportal_login(_PLAIN_LOGIN_HTML))

    def test_admin_console_page_is_not_a_login_page(self):
        self.assertFalse(cn.looks_like_eportal_login(_ADMIN_HTML))

    def test_error_pages_are_not_login_pages(self):
        self.assertFalse(cn.looks_like_eportal_login(_NOT_REGISTERED_HTML))
        self.assertFalse(cn.looks_like_eportal_login(""))
        self.assertFalse(cn.looks_like_eportal_login(None))


class TestLoginPageQuery(unittest.TestCase):
    def client(self, hints):
        cfg = dict(cn.DEFAULT_CONFIG, portal_host="portal.test",
                   detect_targets=[], pin_pubkey=False)
        cfg["login_page_hints"] = hints
        return cn.PortalClient(cn._sanitize_config(cfg))

    def test_configured_hints_produce_query_string(self):
        client = self.client({"wlanacname": "logic", "nasip": "10.253.0.17",
                              "wlanuserip": "10.9.1.2",
                              "wlanparameter": "74-3a-f4-ca-f3-eb"})
        parsed = urllib.parse.parse_qs(client.login_page_query(),
                                       keep_blank_values=True)
        self.assertEqual(parsed["wlanuserip"], ["10.9.1.2"])
        self.assertEqual(parsed["wlanacname"], ["logic"])
        self.assertEqual(parsed["nasip"], ["10.253.0.17"])
        self.assertEqual(parsed["wlanparameter"], ["74-3a-f4-ca-f3-eb"])
        self.assertEqual(parsed["url"], [""])

    def test_local_address_is_used_when_hints_leave_it_empty(self):
        client = self.client({"wlanacname": "logic", "nasip": "10.253.0.17"})
        with mock.patch.object(cn, "local_lan_ipv4",
                               return_value="10.9.124.113") as ip, \
                mock.patch.object(cn, "local_mac_text",
                                  return_value="74-3a-f4-ca-f3-eb") as mac:
            parsed = urllib.parse.parse_qs(client.login_page_query())
        ip.assert_called_once_with("portal.test")
        mac.assert_called_once()
        self.assertEqual(parsed["wlanuserip"], ["10.9.124.113"])
        self.assertEqual(parsed["wlanparameter"], ["74-3a-f4-ca-f3-eb"])

    def test_missing_nasip_or_net_name_disables_synthesis(self):
        self.assertEqual(self.client({"nasip": "10.253.0.17"}).login_page_query(), "")
        self.assertEqual(self.client({"wlanacname": "logic"}).login_page_query(), "")
        self.assertEqual(self.client({}).login_page_query(), "")

    def test_no_local_ip_means_no_synthesis(self):
        client = self.client({"wlanacname": "logic", "nasip": "10.253.0.17"})
        with mock.patch.object(cn, "local_lan_ipv4", return_value=""):
            self.assertEqual(client.login_page_query(), "")

    def test_partial_user_config_keeps_default_hint_fields(self):
        cfg = cn._sanitize_config(dict(cn.DEFAULT_CONFIG,
                                       login_page_hints={"nasip": "10.1.2.3"}))
        self.assertEqual(cfg["login_page_hints"]["nasip"], "10.1.2.3")
        self.assertEqual(cfg["login_page_hints"]["wlanacname"],
                         cn.DEFAULT_CONFIG["login_page_hints"]["wlanacname"])

    def test_empty_or_non_dict_hints_disable_synthesis(self):
        for value in (False, None, "logic", [], {}):
            with self.subTest(value=value):
                cfg = cn._sanitize_config(dict(cn.DEFAULT_CONFIG,
                                       login_page_hints=value))
                self.assertEqual(cfg["login_page_hints"], {})


class TestSynthesizedLoginPageDiscovery(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(cn.DEFAULT_CONFIG, portal_host="portal.test",
                        detect_targets=[], pin_pubkey=False,
                        login_page_hints={"wlanacname": "logic",
                                          "nasip": "10.253.0.17",
                                          "wlanuserip": "10.9.124.113",
                                          "wlanparameter": "74-3a-f4-ca-f3-eb"})
        self.client = cn.PortalClient(cn._sanitize_config(self.cfg))
        self.requests = []
        self.routes = {}

    def request(self, method, host, port, path, **kwargs):
        self.requests.append(path)
        status, headers, body = self.routes.get(path, (404, {}, ""))
        return cn.HttpResponse(status, headers, body.encode("utf-8"),
                               self.client.url(path))

    def discover(self, location=None, online=None):
        with mock.patch.object(self.client, "check_online",
                               return_value=(online, location)), \
                mock.patch.object(cn, "http_request", side_effect=self.request):
            return self.client.discover_login_page()

    def synthesized_path(self):
        return "%s/index.jsp?%s" % (self.client.base,
                                    self.client.login_page_query())

    def test_synthesis_used_when_gateway_gives_no_redirect(self):
        # 真实环境的三个"看起来像入口"的地址：成功页 / 设备未注册脚本 /
        # 后台管理登录页，全都不能当登录页用
        self.routes["/"] = (302, {"Location": "/eportal/redirectortosuccess.jsp"}, "")
        self.routes["/eportal/redirectortosuccess.jsp"] = (200, {}, "<html>ok</html>")
        self.routes["/eportal/index.jsp"] = (200, {}, _NOT_REGISTERED_HTML)
        self.routes["/eportal/"] = (200, {}, _ADMIN_HTML)
        path = self.synthesized_path()
        self.routes[path] = (200, {}, _PLAIN_LOGIN_HTML)

        url, html = self.discover()
        self.assertEqual(url, self.client.url(path))
        self.assertEqual(html, _PLAIN_LOGIN_HTML)
        self.assertEqual(self.requests[-1], path)
        self.assertIn(urllib.parse.urlparse(url).query, path)

    def test_admin_console_alone_never_authorizes_login(self):
        self.routes["/"] = (302, {"Location": "/eportal/redirectortosuccess.jsp"}, "")
        self.routes["/eportal/"] = (200, {}, _ADMIN_HTML)
        self.cfg["login_page_hints"] = {}
        client = cn.PortalClient(cn._sanitize_config(self.cfg))
        self.client = client
        self.assertEqual(self.discover(), (None, None))

    def test_synthesis_skipped_without_hints(self):
        self.cfg["login_page_hints"] = {}
        self.client = cn.PortalClient(cn._sanitize_config(self.cfg))
        self.assertEqual(self.discover(), (None, None))
        self.assertEqual(self.requests, ["/", "/eportal/index.jsp", "/eportal/"])

    def test_redirect_location_still_wins_over_synthesis(self):
        path = "/eportal/index.jsp?wlanuserip=10.9.1.2&nasip=10.253.0.17&mac=SYN"
        self.routes[path] = (200, {}, _login_html(EXP, MOD))
        url, html = self.discover(location=self.client.url(path))
        self.assertEqual(url, self.client.url(path))
        self.assertEqual(self.requests, [path])

    def test_synthesis_failure_logs_actionable_hint(self):
        with mock.patch.object(self.client, "check_online",
                               return_value=(None, None)), \
                mock.patch.object(cn, "http_request", side_effect=self.request), \
                mock.patch.object(cn, "log") as log:
            self.assertEqual(self.client.discover_login_page(), (None, None))
        messages = " ".join(str(c.args[0]) for c in log.info.call_args_list)
        self.assertIn("login_page_hints", messages)


class TestCryptoFromPageInfo(_StateDirMixin):
    """登录页把加密参数留空时，按门户页面同样的方式从 pageInfo 补齐。"""

    def setUp(self):
        _StateDirMixin.setUp(self)
        self.cfg = dict(cn.DEFAULT_CONFIG, portal_host="portal.test",
                        detect_targets=[], pin_pubkey=False)
        self.client = cn.PortalClient(self.cfg)
        self.url = self.client.url("/eportal/index.jsp?wlanuserip=10.9.124.113")

    def login(self, html, page_info, url=None):
        self.posts = []
        self.plaintexts = []

        def request(method, host, port, path, **kwargs):
            self.posts.append((method, path, kwargs.get("body", "")))
            return cn.HttpResponse(200, {}, b'{"result":"success",'
                                              b'"userIndex":"u1"}', self.url)

        def encrypt(plain, exp, mod):
            self.plaintexts.append(plain)
            return "TEST-CIPHER"

        with mock.patch.object(self.client, "discover_login_page",
                               return_value=(url or self.url, html)), \
                mock.patch.object(cn, "http_request", side_effect=request), \
                mock.patch.object(cn, "eportal_rsa_encrypt",
                                  side_effect=encrypt), \
                mock.patch.object(self.client, "page_info",
                                  return_value=page_info) as info:
            result = self.client.login("dummy-user", "dummy-password")
        return result, info

    def test_page_info_supplies_encryption_for_new_style_login_page(self):
        result, info = self.login(_PLAIN_LOGIN_HTML,
                                  {"passwordEncrypt": "true",
                                   "publicKeyExponent": EXP,
                                   "publicKeyModulus": MOD})
        self.assertEqual(result.get("result"), "success")
        info.assert_called_once_with("wlanuserip=10.9.124.113")
        payload = urllib.parse.parse_qs(self.posts[-1][2])
        self.assertEqual(payload["passwordEncrypt"], ["true"])
        # 密文 != 明文，且不是明文直传
        self.assertNotEqual(urllib.parse.unquote(payload["password"][0]),
                            "dummy-password")

    def test_page_info_saying_plain_still_sends_plaintext(self):
        result, _ = self.login(_PLAIN_LOGIN_HTML,
                               {"passwordEncrypt": "false"})
        payload = urllib.parse.parse_qs(self.posts[-1][2])
        self.assertEqual(payload["passwordEncrypt"], ["false"])
        self.assertEqual(urllib.parse.unquote(payload["password"][0]),
                         "dummy-password")

    def test_missing_mac_in_query_falls_back_to_portal_default(self):
        # 门户页面 login_bch.js：queryString 没有 mac 时按 111111111 参与加密
        result, _ = self.login(_login_html(EXP, MOD), None)
        self.assertEqual(result.get("result"), "success")
        self.assertEqual(self.plaintexts,
                         [cn.reverse_str("dummy-password>111111111")])


if __name__ == "__main__":
    unittest.main()
