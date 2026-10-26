# -*- coding: utf-8 -*-
"""GitHub Release 更新检查（只检查，不下载、不替换文件）。"""

import json
import unittest
from unittest import mock

import campusnet as cn


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _payload(tag="v0.6.2", name="0.6.2", url=None):
    return {
        "tag_name": tag,
        "name": name,
        "published_at": "2026-10-24T08:00:00Z",
        "html_url": url if url is not None else (
            "https://github.com/%s/releases/tag/%s" % (cn.GITHUB_REPO, tag)),
    }


class TestReleaseCheck(unittest.TestCase):
    def setUp(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def tearDown(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def _check(self, payload=None, error=None):
        patcher = mock.patch.object(cn.urllib.request, "urlopen")
        m = patcher.start()
        self.addCleanup(patcher.stop)
        if error is not None:
            m.side_effect = error
        else:
            m.return_value = _Resp(payload)
        return cn.check_latest_release(), m

    def test_newer_release_available(self):
        info, _ = self._check(_payload("v0.6.2"))
        self.assertTrue(info["ok"])
        self.assertEqual(info["latest"], "0.6.2")
        self.assertTrue(info["available"])
        self.assertEqual(info["name"], "0.6.2")
        self.assertIn("/releases/tag/v0.6.2", info["url"])
        self.assertTrue(info["checked_at"])

    def test_same_version_not_available(self):
        info, _ = self._check(_payload("v" + cn.APP_VERSION))
        self.assertTrue(info["ok"])
        self.assertFalse(info["available"])

    def test_older_version_not_available(self):
        info, _ = self._check(_payload("v0.0.1"))
        self.assertTrue(info["ok"])
        self.assertFalse(info["available"])

    def test_version_tag_without_v(self):
        info, _ = self._check(_payload("0.7.0"))
        self.assertEqual(info["latest"], "0.7.0")
        self.assertTrue(info["available"])

    def test_network_error_is_reported_not_raised(self):
        info, _ = self._check(error=OSError("offline"))
        self.assertFalse(info["ok"])
        self.assertIn("OSError", info["error"])
        self.assertFalse(info["available"])
        self.assertEqual(info["url"], cn.GITHUB_RELEASES_URL)
        self.assertTrue(info["checked_at"])

    def test_bad_payload_is_reported(self):
        info, _ = self._check({"tag_name": "not-a-version"})
        self.assertFalse(info["ok"])
        self.assertIn("无法解析", info["error"])

    def test_untrusted_html_url_falls_back_to_repo(self):
        info, _ = self._check(_payload(url="https://evil.example/x"))
        self.assertTrue(info["ok"])
        self.assertEqual(info["url"], cn.GITHUB_RELEASES_URL)

    def test_cache_reused_until_forced(self):
        info, m = self._check(_payload("v0.6.1"))
        self.assertTrue(info["ok"])
        again = cn.check_latest_release()
        self.assertEqual(again["latest"], "0.6.1")
        self.assertEqual(m.call_count, 1)
        cn.check_latest_release(force=True)
        self.assertEqual(m.call_count, 2)

    def test_failure_also_cached(self):
        _, m = self._check(error=OSError("offline"))
        cn.check_latest_release()
        self.assertEqual(m.call_count, 1)

    def test_get_update_info_never_uses_network(self):
        with mock.patch.object(cn.urllib.request, "urlopen") as m:
            info = cn.get_update_info()
        self.assertFalse(info["ok"])
        self.assertEqual(info["current"], cn.APP_VERSION)
        m.assert_not_called()


class TestOpenReleasePage(unittest.TestCase):
    def setUp(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def tearDown(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def test_opens_default_repo_page(self):
        with mock.patch("webbrowser.open", return_value=True) as m:
            self.assertTrue(cn.open_release_page())
        m.assert_called_once_with(cn.GITHUB_RELEASES_URL)

    def test_opens_cached_release_url(self):
        url = "https://github.com/%s/releases/tag/v0.6.1" % cn.GITHUB_REPO
        cn._UPDATE_CACHE["data"] = {
            "ok": True, "latest": "0.6.1", "available": True, "url": url,
        }
        with mock.patch("webbrowser.open", return_value=True) as m:
            self.assertTrue(cn.open_release_page())
        m.assert_called_once_with(url)

    def test_rejects_untrusted_url(self):
        cn._UPDATE_CACHE["data"] = {
            "ok": True, "latest": "0.6.1", "available": True,
            "url": "https://evil.example/release",
        }
        with mock.patch("webbrowser.open", return_value=True) as m:
            self.assertFalse(cn.open_release_page())
        m.assert_not_called()

    def test_returns_false_when_browser_fails(self):
        with mock.patch("webbrowser.open", side_effect=OSError("no browser")):
            self.assertFalse(cn.open_release_page())


if __name__ == "__main__":
    unittest.main()
