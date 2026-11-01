#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统代理守卫的离线单元测试。

守护在开机后、认证前会先检查 Windows 系统代理（WinINET）：关机前开着"梯子"
的用户，开机后系统代理仍是开的，而代理客户端在未认证的校园网里连不上节点，
于是所有 HTTP 都交给一个死代理——浏览器打不开网页，程序也探测不到网络。

这组用例全部**不碰真实注册表**：把 ``_inet_read`` / ``_inet_write`` /
``_inet_notify_changed`` 换成内存实现，因此在任何机器上结果都一致（测试结果
绝不能依赖"跑测试这台机器当前有没有开代理"）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import campusnet as cn  # noqa: E402
from tests.test_core import _StateDirMixin  # noqa: E402


class _FakeInetSettings:
    """模拟 HKCU\\...\\Internet Settings 键：内存字典 + 记录写入。

    只实现本项目用到的那点 API（QueryValueEx / SetValueEx / DeleteValue）。

    ⚠️ 刻意模拟真实 winreg 的**类型校验**：ProxyEnable 必须是 REG_DWORD，
    按字符串写要报错。这是自测时抓到的真实 bug——假对象若对类型来者不拒，
    这个 bug 在单测里就永远看不见。
    """

    DWORD_NAMES = {"ProxyEnable", "AutoDetect"}

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []      # [(op, name, value)]
        self.notified = 0

    # ---- 供 campusnet 的三个私有函数替换 ----
    def read(self, name):
        return self.values.get(name)

    def write(self, name, value, kind=None):
        if value is None:
            self.values.pop(name, None)
            self.writes.append(("del", name, None))
            return True
        # 复刻真实行为：DWORD 名字必须按 REG_DWORD(4) 写，否则失败
        if name in self.DWORD_NAMES and kind != 4:
            raise TypeError(
                "模拟 winreg: %s 需要 REG_DWORD(4)，收到 %r" % (name, kind))
        self.values[name] = value
        self.writes.append(("set", name, value))
        return True

    def notify(self):
        self.notified += 1


class TestProxyState(_StateDirMixin):
    """只读判定：什么算"代理开着"。"""

    def setUp(self):
        super().setUp()
        self.fake = _FakeInetSettings()
        self._patched = {
            "_inet_read": self.fake.read,
            "_inet_write": self.fake.write,
            "_inet_notify_changed": self.fake.notify,
        }
        self._saved = {k: getattr(cn, k) for k in self._patched}
        for k, v in self._patched.items():
            setattr(cn, k, v)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(cn, k, v)
        super().tearDown()

    def test_state_detects_enabled_proxy(self):
        self.fake.values = {"ProxyEnable": 1,
                            "ProxyServer": "127.0.0.1:15732",
                            "ProxyOverride": "<local>"}
        st = cn.proxy_state()
        self.assertTrue(st["active"])
        self.assertTrue(st["enabled"])
        self.assertEqual(st["server"], "127.0.0.1:15732")
        self.assertIn("127.0.0.1:15732", st["detail"])

    def test_state_detects_pac_even_without_proxy_enable(self):
        self.fake.values = {"ProxyEnable": 0,
                            "AutoConfigURL": "http://x/p.pac"}
        st = cn.proxy_state()
        self.assertFalse(st["enabled"])
        self.assertTrue(st["active"])          # 只有 PAC 也算开启
        self.assertIn("PAC", st["detail"])

    def test_state_clean_when_nothing_configured(self):
        st = cn.proxy_state()
        self.assertFalse(st["active"])
        self.assertIn("未启用系统代理", st["detail"])


class TestDisableProxy(_StateDirMixin):
    """关闭 / 恢复 / 幂等 / 失败上报。"""

    ON = {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:15732",
          "ProxyOverride": "<local>"}

    def setUp(self):
        super().setUp()
        self.fake = _FakeInetSettings()
        self._patched = {
            "_inet_read": self.fake.read,
            "_inet_write": self.fake.write,
            "_inet_notify_changed": self.fake.notify,
        }
        self._saved = {k: getattr(cn, k) for k in self._patched}
        for k, v in self._patched.items():
            setattr(cn, k, v)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(cn, k, v)
        super().tearDown()

    def test_disable_is_noop_without_writes_when_already_clean(self):
        r = cn.disable_system_proxy(record=False)
        self.assertFalse(r["changed"])
        self.assertEqual(self.fake.writes, [])   # 一个字节都不写
        self.assertEqual(self.fake.notified, 0)  # 也不去通知系统

    def test_disable_turns_off_proxy_and_backs_up(self):
        self.fake.values = dict(self.ON)
        self.fake.values["AutoConfigURL"] = "http://x/p.pac"
        r = cn.disable_system_proxy(reason="TEST")
        self.assertTrue(r["changed"])
        self.assertTrue(r["ok"])
        self.assertEqual(self.fake.values["ProxyEnable"], 0)
        self.assertNotIn("AutoConfigURL", self.fake.values)
        # ProxyServer 原样保留：关掉开关即可，不必抹掉服务器地址
        self.assertEqual(self.fake.values["ProxyServer"], "127.0.0.1:15732")
        self.assertEqual(self.fake.notified, 1)  # 必须通知系统重读
        bak = cn.load_state().get("proxy_backup")
        self.assertEqual(bak["ProxyEnable"], 1)
        self.assertEqual(bak["ProxyServer"], "127.0.0.1:15732")
        self.assertEqual(bak["AutoConfigURL"], "http://x/p.pac")

    def test_disable_writes_proxy_enable_as_dword(self):
        """ProxyEnable 必须按 REG_DWORD 写：按字符串写会在真实注册表上静默失败。"""
        self.fake.values = dict(self.ON)
        r = cn.disable_system_proxy(record=False)
        self.assertTrue(r["ok"], "写入应为成功（DWORD 类型正确）")
        self.assertIn(("set", "ProxyEnable", 0), self.fake.writes)

    def test_disable_reports_failure_when_type_is_wrong(self):
        """反向保护：写入失败时必须把 ok 报成 False，而不是假装成功。

        生产代码里的 _inet_write 契约是"永不抛异常、失败返回 False"，
        所以替身也必须返回 False（而不是抛 TypeError）。
        """
        self.fake.values = dict(self.ON)   # 先造出"代理确实开着"的前提

        def bad_write(name, value, kind=None):
            return False                   # 模拟类型不匹配导致的写入失败
        with mock.patch.object(cn, "_inet_write", bad_write):
            r = cn.disable_system_proxy(record=False)
        self.assertTrue(r["changed"])
        self.assertFalse(r["ok"])          # 不能静默成功

    def test_disable_is_idempotent_and_keeps_first_backup(self):
        self.fake.values = dict(self.ON)
        cn.disable_system_proxy(record=True)
        first = cn.load_state()["proxy_backup"]
        r2 = cn.disable_system_proxy(record=True)   # 第二次：已经关了
        self.assertFalse(r2["changed"])
        self.assertEqual(cn.load_state()["proxy_backup"], first)

    def test_restore_writes_back_exact_original_values(self):
        self.fake.values = dict(self.ON)
        cn.disable_system_proxy(record=True)
        ok, _ = cn.restore_system_proxy()
        self.assertTrue(ok)
        self.assertEqual(self.fake.values["ProxyEnable"], 1)
        self.assertEqual(self.fake.values["ProxyServer"], "127.0.0.1:15732")
        self.assertNotIn("proxy_backup", cn.load_state())   # 恢复后清备份

    def test_restore_without_backup_reports_cleanly(self):
        ok, msg = cn.restore_system_proxy()
        self.assertFalse(ok)
        self.assertIn("没有可恢复", msg)

    def test_maybe_clear_proxy_respects_off_switch(self):
        self.fake.values = dict(self.ON)
        r = cn.maybe_clear_proxy({"proxy_guard": "off"})
        self.assertTrue(r.get("skipped"))
        self.assertEqual(self.fake.writes, [])
        self.assertEqual(self.fake.values["ProxyEnable"], 1)  # 原样不动

    def test_maybe_clear_proxy_defaults_to_auto(self):
        self.fake.values = dict(self.ON)
        r = cn.maybe_clear_proxy({})
        self.assertTrue(r["changed"])
        self.assertEqual(self.fake.values["ProxyEnable"], 0)

    def test_maybe_clear_proxy_never_raises(self):
        """代理清理是辅助步骤：内部炸了也不能把认证流程带崩。"""
        def boom(*a, **k):
            raise RuntimeError("registry exploded")
        with mock.patch.object(cn, "disable_system_proxy", side_effect=boom):
            r = cn.maybe_clear_proxy({"proxy_guard": "auto"})
        self.assertFalse(r["changed"])
        self.assertIn("error", r)


class TestProxyGuardConfig(_StateDirMixin):
    """配置项 proxy_guard 的取值归一（手写 JSON 写错也不该把守护带崩）。"""

    def test_only_accepts_auto_or_off(self):
        """proxy_guard 只认 auto/off；手写错的（如 "true"、"OFF "）一律归一。"""
        import json
        for raw, want in (("off", "off"), ("OFF", "off"), (" off ", "off"),
                          ("auto", "auto"), ("AUTO", "auto"),
                          ("yes", "auto"), ("", "auto"), (None, "auto")):
            with self.subTest(raw=raw):
                with open(cn.CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump({"proxy_guard": raw}, f)
                self.assertEqual(cn.load_config()["proxy_guard"], want)


if __name__ == "__main__":
    unittest.main()
