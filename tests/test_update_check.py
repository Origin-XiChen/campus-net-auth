# -*- coding: utf-8 -*-
"""GitHub Release 更新检查（只检查，不下载、不替换文件）。"""

import json
import time
import unittest
from unittest import mock

import campusnet as cn
from tests.test_core import _StateDirMixin


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _payload(tag="v0.6.1", name="0.6.1", url=None):
    return {
        "tag_name": tag,
        "name": name,
        "published_at": "2026-10-24T08:00:00Z",
        "html_url": url if url is not None else (
            "https://github.com/%s/releases/tag/%s" % (cn.GITHUB_REPO, tag)),
    }


class TestReleaseCheck(_StateDirMixin):
    def setUp(self):
        _StateDirMixin.setUp(self)   # 检查结果会落盘，必须隔离 state.json
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def tearDown(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None
        _StateDirMixin.tearDown(self)

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
        info, _ = self._check(_payload("v9.9.9", name="9.9.9"))
        self.assertTrue(info["ok"])
        self.assertEqual(info["latest"], "9.9.9")
        self.assertTrue(info["available"])
        self.assertEqual(info["name"], "9.9.9")
        self.assertIn("/releases/tag/v9.9.9", info["url"])
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


class TestOpenReleasePage(_StateDirMixin):
    def setUp(self):
        _StateDirMixin.setUp(self)   # get_update_info 会回退读 state.json
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def tearDown(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None
        _StateDirMixin.tearDown(self)

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


class TestUpdatePersistence(_StateDirMixin):
    """检查结果落盘：CLI 新进程（status / upgrade --check）也能读到结论。"""

    def setUp(self):
        _StateDirMixin.setUp(self)
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def tearDown(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None
        _StateDirMixin.tearDown(self)

    def _check(self, payload=None, error=None):
        patcher = mock.patch.object(cn.urllib.request, "urlopen")
        m = patcher.start()
        self.addCleanup(patcher.stop)
        if error is not None:
            m.side_effect = error
        else:
            m.return_value = _Resp(payload)
        return cn.check_latest_release(force=True)

    @staticmethod
    def _restart():
        """模拟新进程：内存缓存清空，只剩 state.json。"""
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def test_success_survives_restart(self):
        info = self._check(_payload("v0.9.0"))
        self._restart()
        again = cn.get_update_info()
        self.assertTrue(again["ok"])
        self.assertEqual(again["latest"], "0.9.0")
        self.assertEqual(again["checked_at"], info["checked_at"])
        self.assertTrue(again["available"])

    def test_failure_survives_restart(self):
        self._check(error=OSError("offline"))
        self._restart()
        again = cn.get_update_info()
        self.assertFalse(again["ok"])
        self.assertIn("OSError", again["error"])
        self.assertTrue(again["checked_at"])

    def test_available_recomputed_against_running_version(self):
        """升级后旧记录不能继续喊"有新版本"：读取时按当前版本重算。"""
        cn.update_state({cn.UPDATE_STATE_KEY: {
            "ok": True, "latest": cn.APP_VERSION, "available": True,
            "url": cn.GITHUB_RELEASES_URL, "name": "", "published_at": "",
            "checked_at": int(time.time()), "error": ""}})
        self._restart()
        info = cn.get_update_info()
        self.assertTrue(info["ok"])
        self.assertFalse(info["available"])
        self.assertEqual(info["current"], cn.APP_VERSION)

    def test_concurrent_check_reuses_current_result(self):
        """自动重查与手动点击撞车时，不重复发网络请求。"""
        self._check(_payload("v0.9.0"))
        with mock.patch.object(cn.urllib.request, "urlopen") as m:
            self.assertTrue(cn._UPDATE_FETCH_LOCK.acquire(blocking=False))
            try:
                info = cn.check_latest_release(force=True)
            finally:
                cn._UPDATE_FETCH_LOCK.release()
        self.assertEqual(info["latest"], "0.9.0")
        m.assert_not_called()


class TestUpdateCheckDue(_StateDirMixin):
    """自动补查的判定：成功 6 小时、失败 5 分钟。"""

    NOW = 100000

    def setUp(self):
        _StateDirMixin.setUp(self)
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def tearDown(self):
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None
        _StateDirMixin.tearDown(self)

    def _record(self, ok, age):
        cn.update_state({cn.UPDATE_STATE_KEY: {
            "ok": ok, "latest": "0.9.0", "available": True,
            "url": cn.GITHUB_RELEASES_URL, "name": "", "published_at": "",
            "checked_at": int(self.NOW - age),
            "error": "" if ok else "OSError: offline"}})
        cn._UPDATE_CACHE["t"] = 0.0
        cn._UPDATE_CACHE["data"] = None

    def test_never_checked_is_due(self):
        self.assertTrue(cn.update_check_due(now=self.NOW)[0])

    def test_fresh_success_not_due(self):
        self._record(True, 60)
        self.assertFalse(cn.update_check_due(now=self.NOW)[0])

    def test_success_past_ttl_is_due(self):
        self._record(True, cn.UPDATE_CHECK_TTL + 1)
        self.assertTrue(cn.update_check_due(now=self.NOW)[0])

    def test_recent_failure_not_due(self):
        self._record(False, 60)
        self.assertFalse(cn.update_check_due(now=self.NOW)[0])

    def test_failure_past_retry_gap_is_due(self):
        self._record(False, cn.UPDATE_CHECK_FAIL_TTL + 1)
        self.assertTrue(cn.update_check_due(now=self.NOW)[0])


if __name__ == "__main__":
    unittest.main()
