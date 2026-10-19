import contextlib
import types
import unittest
from unittest import mock

import campusnet as cn
from tests.test_core import EXP, MOD, _login_html


LOGIN_PATH = "/eportal/index.jsp?token=TEST-ONLY&mac=SYNTHETIC"
LOGIN_HTML = _login_html(EXP, MOD)


class ReplayLimit(BaseException):
    pass


class DaemonReplay:
    def __init__(self, online=None, page=True, commands=None):
        self.cfg = dict(cn.DEFAULT_CONFIG, username="dummy-user",
                        portal_host="portal.test", detect_targets=[],
                        retry_interval=20, offline_interval=60,
                        recheck_interval=10, max_interval=120)
        self.client = cn.PortalClient(self.cfg)
        self.page = (self.client.url(LOGIN_PATH), LOGIN_HTML) if page else (None, None)
        self.client.discover_login_page = mock.Mock(return_value=self.page)
        self.client.interface = mock.Mock(return_value={})
        self.client.portal_reachable = mock.Mock(return_value=True)
        self.client.login = mock.Mock(return_value={"result": "fail", "message": "TEST-FAIL"})
        self.client.online_info = mock.Mock(return_value={})
        self.probes = list(online) if isinstance(online, list) else [online]
        self.probe_count = 0
        self.client.check_online = mock.Mock(side_effect=self.probe)
        self.commands = list(commands) if commands is not None else ["STOP"]
        self.now = 1000.0
        self.waits = []
        self.lock = mock.Mock()
        self.connections = []
        self.log = mock.Mock()
        self.logout = mock.Mock(return_value=(True, "TEST-ONLY"))
        self.notifications = mock.Mock()
        self.outcome = None

    def probe(self):
        self.probe_count += 1
        if self.probe_count > 100:
            raise ReplayLimit("bounded offline replay")
        online = self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]
        return online, None

    def select(self, readers, writers, errors, timeout):
        self.waits.append(timeout)
        if not self.commands:
            raise ReplayLimit("bounded offline waits")
        command = self.commands.pop(0)
        if command is None:
            self.now += timeout
            return [], [], []
        connection = mock.Mock()
        connection.recv.return_value = command.encode("utf-8")
        self.connections.append(connection)
        self.lock.accept.return_value = (connection, ("TEST-ONLY", 0))
        return [self.lock], [], []

    def run(self):
        with contextlib.ExitStack() as stack:
            patches = {
                "PortalClient": mock.Mock(return_value=self.client),
                "acquire_singleton": mock.Mock(return_value=self.lock),
                "write_pid": mock.Mock(), "remove_pid": mock.Mock(),
                "_start_addr_change_watcher": mock.Mock(),
                "update_state": mock.Mock(), "save_state": mock.Mock(),
                "load_state": mock.Mock(return_value={}),
                "record_event": mock.Mock(), "self_exe_path": mock.Mock(return_value="TEST-EXE"),
                "logout_stale_session": self.logout,
                "notify_system": self.notifications, "log": self.log,
                "http_request": mock.Mock(side_effect=AssertionError("unexpected HTTP")),
            }
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(cn, name, value))
            stack.enter_context(mock.patch.object(cn.atexit, "register"))
            stack.enter_context(mock.patch.object(cn.time, "time", side_effect=lambda: self.now))
            stack.enter_context(mock.patch("select.select", side_effect=self.select))
            try:
                self.outcome = cn.run_daemon(self.cfg, "dummy-password")
            except ReplayLimit:
                self.outcome = "replay-limit"
        if self.outcome == 0:
            self.log.exception.assert_not_called()
        return self.outcome


class TestDaemonRecovery(unittest.TestCase):
    def test_unknown_probes_with_valid_page_attempt_login_once(self):
        replay = DaemonReplay()
        self.assertEqual(replay.run(), 0)
        replay.client.login.assert_called_once_with(
            "dummy-user", "dummy-password", login_page=replay.page)
        self.assertEqual(replay.waits, [20])
        replay.logout.assert_not_called()

    def test_unreachable_portal_waits_on_first_round_without_login(self):
        replay = DaemonReplay(page=False)
        replay.client.portal_reachable.return_value = False
        self.assertEqual(replay.run(), 0)
        self.assertEqual(replay.client.portal_reachable.call_count, 1)
        replay.client.login.assert_not_called()
        self.assertEqual(replay.waits, [10])

    def test_unknown_probes_invalid_page_back_off_without_fake_recovery(self):
        replay = DaemonReplay(page=False, commands=[None] * 6 + ["STOP"])
        self.assertEqual(replay.run(), 0)
        replay.client.login.assert_not_called()
        self.assertEqual(replay.waits, [10] * 7)
        self.assertEqual(replay.client.discover_login_page.call_count, 2)
        messages = [str(call.args[0]) for call in replay.log.info.call_args_list]
        self.assertFalse(any("门户已恢复" in message for message in messages))

    def test_unknown_to_false_breaks_segmented_backoff(self):
        replay = DaemonReplay(online=[None, None, False, False], page=False,
                              commands=[None, None, "STOP"])
        self.assertEqual(replay.run(), 0)
        replay.client.login.assert_called_once()
        self.assertEqual(replay.waits, [10, 10, 40])

    def test_unknown_backoff_grows_and_caps_without_reset(self):
        replay = DaemonReplay(page=False, commands=[None] * 30 + ["STOP"])
        self.assertEqual(replay.run(), 0)
        self.assertEqual(replay.client.discover_login_page.call_count, 4)
        replay.client.login.assert_not_called()
        self.assertEqual(replay.now, 1300.0)
        self.assertEqual(replay.waits, [10] * 31)

    def test_failed_attempts_fetch_fresh_page_and_keep_retry_backoff(self):
        replay = DaemonReplay(commands=[None, "STOP"])
        self.assertEqual(replay.run(), 0)
        self.assertEqual(replay.client.discover_login_page.call_count, 2)
        self.assertEqual(replay.client.login.call_count, 2)
        self.assertEqual(replay.waits, [20, 40])

    def test_check_also_interrupts_unreachable_portal_wait(self):
        replay = DaemonReplay(commands=["CHECK", "STOP"])
        replay.client.portal_reachable.side_effect = [False, True]
        self.assertEqual(replay.run(), 0)
        replay.client.login.assert_called_once()
        self.assertEqual(replay.waits, [10, 20])

    def test_already_online_does_not_login_or_logout(self):
        replay = DaemonReplay(online=True)
        self.assertEqual(replay.run(), 0)
        replay.client.login.assert_not_called()
        replay.logout.assert_not_called()
        self.assertEqual(replay.waits, [30])

    def test_check_wakes_unknown_page_recovery_immediately(self):
        replay = DaemonReplay(page=False, commands=["CHECK", "STOP"])
        page = (replay.client.url(LOGIN_PATH), LOGIN_HTML)
        replay.client.discover_login_page.side_effect = [(None, None), page]
        self.assertEqual(replay.run(), 0)
        replay.client.login.assert_called_once_with(
            "dummy-user", "dummy-password", login_page=page)
        self.assertEqual(replay.waits, [10, 40])

    def test_notify_ver_and_noise_do_not_interrupt_unknown_wait(self):
        replay = DaemonReplay(page=False,
                              commands=["NOTIFY", "VER", "", "UNKNOWN", "STOP"])
        self.assertEqual(replay.run(), 0)
        self.assertEqual(replay.client.discover_login_page.call_count, 1)
        replay.client.login.assert_not_called()
        replay.notifications.assert_called_once()
        self.assertTrue(replay.connections[1].sendall.call_args.args[0].startswith(b"OK "))
        self.assertTrue(all(connection.close.called for connection in replay.connections))

    def test_success_keeps_post_login_verification_and_interval(self):
        replay = DaemonReplay(commands=[None, "STOP"])
        replay.client.login.return_value = {"result": "success"}
        self.assertEqual(replay.run(), 0)
        replay.client.login.assert_called_once()
        self.assertEqual(replay.waits, [5, 30])
        replay.logout.assert_not_called()

    def test_existing_stale_session_recovery_is_preserved(self):
        replay = DaemonReplay(online=False, commands=[None, "STOP"])
        replay.client.login.return_value = {"result": "success"}
        self.assertEqual(replay.run(), 0)
        replay.logout.assert_called_once()
        self.assertEqual(replay.waits, [5, 2])


class TestOnceRecovery(unittest.TestCase):
    def run_once(self, online, page=True):
        self.cfg = dict(cn.DEFAULT_CONFIG, username="dummy-user", portal_host="portal.test")
        self.client = cn.PortalClient(self.cfg)
        self.page = (self.client.url(LOGIN_PATH), LOGIN_HTML) if page else (None, None)
        self.client.check_online = mock.Mock(return_value=(online, None))
        self.client.interface = mock.Mock(return_value={})
        self.client.discover_login_page = mock.Mock(return_value=self.page)
        self.client.login = mock.Mock(return_value={"result": "success"})
        with mock.patch.object(cn, "load_config", return_value=self.cfg), \
                mock.patch.object(cn, "CredentialStore") as store, \
                mock.patch.object(cn, "PortalClient", return_value=self.client), \
                mock.patch.object(cn, "log"), \
                mock.patch.object(cn, "http_request", side_effect=AssertionError("unexpected HTTP")):
            store.return_value.load.return_value = "dummy-password"
            return cn.cmd_once(types.SimpleNamespace())

    def test_unknown_probes_with_valid_page_attempt_login(self):
        self.assertEqual(self.run_once(None), 0)
        self.client.login.assert_called_once_with(
            "dummy-user", "dummy-password", login_page=self.page)

    def test_unknown_without_valid_page_does_not_login(self):
        self.assertEqual(self.run_once(None, page=False), 0)
        self.client.login.assert_not_called()

    def test_online_does_not_login(self):
        self.assertEqual(self.run_once(True), 0)
        self.client.login.assert_not_called()


class TestAuthenticationStatus(unittest.TestCase):
    def setUp(self):
        self.client = cn.PortalClient(dict(cn.DEFAULT_CONFIG, portal_host="portal.test",
                                          detect_targets=[], pin_pubkey=False))
        self.url = self.client.url(LOGIN_PATH)

    def check_status(self, html=LOGIN_HTML, url=None, online=None, info=None):
        with mock.patch.object(self.client, "check_online", return_value=(online, None)), \
                mock.patch.object(self.client, "interface", return_value=info), \
                mock.patch.object(self.client, "discover_login_page",
                                  return_value=(url or self.url, html)) as discover, \
                mock.patch.object(cn, "http_request", side_effect=AssertionError("unexpected HTTP")):
            result = self.client.check_authentication()
        return result, discover

    def test_unknown_valid_challenge_returns_only_current_page(self):
        result, discover = self.check_status()
        self.assertEqual(result, (False, (self.url, LOGIN_HTML)))
        discover.assert_called_once_with(detection=(None, None))

    def test_invalid_page_and_missing_query_do_not_authorize_login(self):
        for html, url in (("<html>HTTP 200 only</html>", self.url),
                          ('<input id="pwd">', self.url),
                          (_login_html("", ""), self.url),
                          (LOGIN_HTML, self.client.url("/eportal/index.jsp")),
                          (_login_html(EXP, MOD, encrypt="invalid"), self.url)):
            with self.subTest(html=html[:40], url=url):
                self.assertEqual(self.check_status(html, url)[0], (None, None))

    def test_explicit_portal_online_state_does_not_fetch_or_login(self):
        result, discover = self.check_status(info={"result": "success", "userIndex": "TEST-ONLY"})
        self.assertEqual(result, (True, None))
        discover.assert_not_called()

    def test_untrusted_or_malformed_page_does_not_authorize_login(self):
        for url in (
                "http://other.test/eportal/index.jsp?token=TEST-ONLY",
                "http://portal.test:81/eportal/index.jsp?token=TEST-ONLY",
                "https://portal.test/eportal/index.jsp?token=TEST-ONLY",
                "http://user@portal.test/eportal/index.jsp?token=TEST-ONLY",
                "http://portal.test:invalid/eportal/index.jsp?token=TEST-ONLY",
                "http://[invalid/eportal/index.jsp?token=TEST-ONLY"):
            with self.subTest(url=url):
                self.assertEqual(self.check_status(url=url)[0], (None, None))

    def test_failed_portal_reply_is_not_positive_online_evidence(self):
        result, _ = self.check_status(info={"result": "fail", "userIndex": "TEST-ONLY"})
        self.assertEqual(result, (False, (self.url, LOGIN_HTML)))

    def test_known_public_result_is_preserved_without_portal_discovery(self):
        for online in (False, True):
            with self.subTest(online=online):
                result, discover = self.check_status(online=online)
                self.assertEqual(result, (online, None))
                discover.assert_not_called()

    def test_explicit_unencrypted_form_is_supported(self):
        html = '<input id="passwordEncrypt" value="false"><input id="pwd">'
        self.assertEqual(self.check_status(html)[0], (False, (self.url, html)))

    def test_real_discovery_and_login_reuse_only_current_page(self):
        requests = []

        def request(method, host, port, path, **kwargs):
            requests.append((method, path))
            self.assertEqual((host, port), ("portal.test", 80))
            self.assertIs(kwargs["cookie_jar"], self.client.cookies)
            if path == "/":
                return cn.HttpResponse(302, {"Location": LOGIN_PATH}, b"", self.url)
            if path == LOGIN_PATH:
                return cn.HttpResponse(200, {}, LOGIN_HTML.encode("utf-8"), self.url)
            if method == "GET":
                return cn.HttpResponse(404, {}, b"", self.url)
            self.assertEqual((method, path), ("POST", "/eportal/InterFace.do?method=login"))
            return cn.HttpResponse(200, {}, b'{"result":"success"}', self.url)

        with mock.patch.object(self.client, "check_online", return_value=(None, None)) as probe, \
                mock.patch.object(self.client, "interface", return_value={}), \
                mock.patch.object(cn, "http_request", side_effect=request):
            online, page = self.client.check_authentication()
            self.assertIs(online, False)
            result = self.client.login("dummy-user", "dummy-password", login_page=page)
        self.assertEqual(result["result"], "success")
        probe.assert_called_once()
        self.assertEqual(requests.count(("GET", LOGIN_PATH)), 1)
        self.assertEqual([entry for entry in requests if entry[0] == "POST"],
                         [("POST", "/eportal/InterFace.do?method=login")])

    def test_reused_page_cannot_bypass_existing_key_pinning(self):
        self.client.cfg["pin_pubkey"] = True
        for html in (LOGIN_HTML, _login_html(EXP, MOD, encrypt="false")):
            with self.subTest(encrypted='value="true"' in html), \
                    mock.patch.object(cn, "load_state", return_value={"portal_pubkey_fp": "0" * 64}), \
                    mock.patch.object(cn, "http_request") as request:
                result = self.client.login("dummy-user", "dummy-password",
                                           login_page=(self.url, html))
            self.assertEqual(result["result"], "fail")
            request.assert_not_called()

    def test_http_200_without_challenge_never_submits_credentials(self):
        paths = []

        def request(method, host, port, path, **kwargs):
            self.assertEqual((method, host, port), ("GET", "portal.test", 80))
            paths.append(path)
            return cn.HttpResponse(200, {}, b'<input id="pwd">', self.url)

        with mock.patch.object(self.client, "check_online", return_value=(None, None)), \
                mock.patch.object(self.client, "interface", return_value={}), \
                mock.patch.object(cn, "http_request", side_effect=request):
            self.assertEqual(self.client.check_authentication(), (None, None))
        self.assertTrue(paths)
        self.assertTrue(all(path in ("/", "/eportal/index.jsp", "/eportal/")
                            for path in paths))
