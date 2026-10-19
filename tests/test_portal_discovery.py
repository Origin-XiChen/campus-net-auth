import collections
import unittest
import urllib.parse
from unittest import mock

import campusnet as cn
from tests.test_core import EXP, MOD, _login_html


class TestPortalDiscovery(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(cn.DEFAULT_CONFIG, portal_host="portal.test",
                        detect_targets=[], pin_pubkey=False)
        self.client = cn.PortalClient(self.cfg)
        self.path = "/eportal/index.jsp?mac=SYNTHETIC&token=TEST-ONLY"
        self.url = self.client.url(self.path)
        self.html = _login_html(EXP, MOD)
        self.requests = []
        self.routes = {}

    def request(self, method, host, port, path, **kwargs):
        self.requests.append((method, host, port, path, kwargs))
        self.assertEqual((host, port), ("portal.test", 80))
        self.assertIs(kwargs.get("cookie_jar"), self.client.cookies)
        status, headers, body = self.routes.get(path, (404, {}, ""))
        return cn.HttpResponse(status, headers, body.encode("utf-8"),
                               self.client.url(path))

    def discover(self, location=None, online=False):
        with mock.patch.object(self.client, "check_online",
                               return_value=(online, location)), \
                mock.patch.object(cn, "http_request", side_effect=self.request):
            return self.client.discover_login_page()

    def test_valid_redirect_needs_only_one_page_get(self):
        self.routes[self.path] = (200, {}, self.html)
        self.assertEqual(self.discover(self.url), (self.url, self.html))
        self.assertEqual([item[3] for item in self.requests], [self.path])

    def test_failed_preferred_page_falls_back_immediately(self):
        self.routes["/"] = (302, {"Location": self.path}, "")
        self.routes[self.path] = (200, {}, self.html)
        self.assertEqual(self.discover(self.client.url("/expired")),
                         (self.url, self.html))
        self.assertEqual([item[3] for item in self.requests],
                         ["/expired", "/", self.path])

    def test_no_redirect_accepts_direct_fallback_page_without_refetch(self):
        self.routes["/eportal/index.jsp"] = (200, {}, self.html)
        self.assertEqual(self.discover(online=None),
                         (self.client.url("/eportal/index.jsp"), self.html))
        self.assertEqual([item[3] for item in self.requests],
                         ["/", "/eportal/index.jsp"])

    def test_javascript_relative_redirect_is_resolved(self):
        self.routes["/"] = (200, {}, "<script>location='" + self.path + "'</script>")
        self.routes[self.path] = (200, {}, self.html)
        self.assertEqual(self.discover(), (self.url, self.html))
        self.assertEqual([item[3] for item in self.requests], ["/", self.path])

    def test_relative_initial_redirect_is_resolved(self):
        self.routes[self.path] = (200, {}, self.html)
        self.assertEqual(self.discover(self.path), (self.url, self.html))
        self.assertEqual([item[3] for item in self.requests], [self.path])

    def test_duplicate_candidate_is_not_requested_twice(self):
        self.routes["/"] = (302, {"Location": self.path}, "")
        self.routes["/eportal/index.jsp"] = (302, {"Location": self.path}, "")
        self.assertEqual(self.discover(self.url), (None, None))
        counts = collections.Counter(item[3] for item in self.requests)
        self.assertEqual(counts[self.path], 1)
        self.assertTrue(all(count == 1 for count in counts.values()))

    def test_invalid_candidates_are_not_requested(self):
        for location in ("http://other.test/eportal/index.jsp?token=TEST-ONLY",
                         "http://portal.test.other.test/eportal/",
                         "http://portal.test:81/eportal/",
                         "http://user:password@portal.test/eportal/",
                         "http://portal.test:invalid/eportal/",
                         "http://[broken", "https://portal.test/eportal/",
                         "file:///eportal/index.jsp"):
            with self.subTest(location=location):
                self.requests.clear()
                self.routes["/"] = (200, {}, self.html)
                self.assertEqual(self.discover(location),
                                 (self.client.url("/"), self.html))
                self.assertEqual([item[3] for item in self.requests], ["/"])

    def test_untrusted_fallback_redirect_is_not_requested(self):
        self.routes["/"] = (302, {"Location": "http://other.test/eportal/"}, "")
        self.assertEqual(self.discover(), (None, None))
        self.assertEqual([item[3] for item in self.requests],
                         ["/", "/eportal/index.jsp", "/eportal/"])

    def test_already_online_does_not_fetch_login_page(self):
        self.assertEqual(self.discover(self.url, online=True), (None, None))
        self.assertEqual(self.requests, [])

    def test_malformed_fallback_redirect_does_not_abort_discovery(self):
        self.routes["/"] = (302, {"Location": "http://[broken"}, "")
        self.routes["/eportal/index.jsp"] = (200, {}, self.html)
        self.assertEqual(self.discover(),
                         (self.client.url("/eportal/index.jsp"), self.html))

    def test_configured_custom_port_and_base_are_supported(self):
        self.client = cn.PortalClient(dict(self.cfg, portal_port=8080,
                                          portal_base="/custom"))
        url = self.client.url("/custom/index.jsp?token=TEST-ONLY")
        with mock.patch.object(self.client, "check_online", return_value=(False, url)), \
                mock.patch.object(cn, "http_request", return_value=cn.HttpResponse(
                    200, {}, self.html.encode("utf-8"), url)) as request:
            self.assertEqual(self.client.discover_login_page(), (url, self.html))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[:4],
                         ("GET", "portal.test", 8080, "/custom/index.jsp?token=TEST-ONLY"))

    def test_equivalent_default_ports_and_fragments_are_deduplicated(self):
        self.routes["/"] = (302, {"Location": self.url + "#ignored"}, "")
        self.assertEqual(self.discover(self.url.replace(":80/", "/")), (None, None))
        self.assertEqual(sum(item[3] == self.path for item in self.requests), 1)

    def test_cookie_jar_survives_fallback_discovery(self):
        self.routes["/"] = (302, {"Location": self.path}, "")
        self.routes[self.path] = (200, {}, self.html)
        original_request = self.request

        def request(method, host, port, path, **kwargs):
            if path == "/":
                kwargs["cookie_jar"].update(host, port, ["SESSION=TEST-ONLY; Path=/"])
            else:
                self.assertEqual(kwargs["cookie_jar"].get(host, port),
                                 "SESSION=TEST-ONLY")
            return original_request(method, host, port, path, **kwargs)

        with mock.patch.object(self.client, "check_online", return_value=(None, None)), \
                mock.patch.object(cn, "http_request", side_effect=request):
            self.assertEqual(self.client.discover_login_page(), (self.url, self.html))

    def test_login_keeps_current_query_crypto_and_service(self):
        self.routes[self.path] = (200, {}, self.html)
        self.routes["/eportal/InterFace.do?method=login"] = (
            200, {}, '{"result":"success","userIndex":"TEST-ONLY"}')
        with mock.patch.object(self.client, "check_online", return_value=(False, self.url)), \
                mock.patch.object(cn, "http_request", side_effect=self.request), \
                mock.patch.object(cn, "eportal_rsa_encrypt", return_value="TEST-CIPHER") as encrypt:
            result = self.client.login("dummy-user", "dummy-password", "TEST-SERVICE")
        self.assertEqual(result["result"], "success")
        encrypt.assert_called_once_with(cn.reverse_str("dummy-password>SYNTHETIC"),
                                        EXP, MOD)
        posts = [item for item in self.requests if item[0] == "POST"]
        self.assertEqual(len(posts), 1)
        fields = urllib.parse.parse_qs(posts[0][4]["body"])
        self.assertEqual(urllib.parse.unquote(fields["queryString"][0]),
                         urllib.parse.urlparse(self.url).query)
        self.assertEqual(urllib.parse.unquote(fields["service"][0]), "TEST-SERVICE")
        self.assertEqual(urllib.parse.unquote(fields["password"][0]), "TEST-CIPHER")
        self.assertEqual(posts[0][4]["headers"]["Referer"], self.url)

    def test_auto_service_receives_current_query(self):
        self.routes[self.path] = (200, {}, self.html)
        self.routes["/eportal/InterFace.do?method=login"] = (200, {}, '{"result":"success"}')
        self.client.cfg["service"] = "auto"
        with mock.patch.object(self.client, "check_online", return_value=(False, self.url)), \
                mock.patch.object(cn, "http_request", side_effect=self.request), \
                mock.patch.object(cn, "eportal_rsa_encrypt", return_value="TEST-CIPHER"), \
                mock.patch.object(self.client, "page_info", return_value={
                    "service": {"TEST-SERVICE": {"serviceDefault": "true"}}}) as page_info:
            result = self.client.login("dummy-user", "dummy-password")
        page_info.assert_called_once_with(urllib.parse.urlparse(self.url).query)
        self.assertEqual(result["_service"], "TEST-SERVICE")
