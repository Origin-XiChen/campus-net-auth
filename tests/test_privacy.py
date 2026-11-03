# -*- coding: utf-8 -*-
"""隐私守卫：确保仓库里被 git 跟踪的文本不含真实身份/设备信息。

只扫描 tracked 文件；config.json / cred.bin / state.json / 日志等本地运行时文件
由 .gitignore 拦住，不进入这个测试。失败信息只报文件、行号和类别，不回显原值，
避免 CI 日志二次泄露。
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 只允许这些合成值；新增合法占位值时要同步这里
MAC_ALLOW = {
    "aabbccddee01",
    "aabbccddee02",
    "aabbccddeeff",
    "001122334455",
    "000000000000",
}
EMAIL_ALLOW_EXACT = {"git@github.com"}
EMAIL_ALLOW_DOMAINS = {
    "test",
    "example.com",
    "example.org",
    "example.net",
    "users.noreply.github.com",
}
TEN_DIGIT_ALLOW = {"1234567890", "1577836800"}  # 示例账号 / 代码里的固定时间戳

FORBIDDEN_NAMES = {
    "config.json",
    "cred.bin",
    "state.json",
    "campusnet.log",
    "daemon.pid",
    "HANDOVER.md",
    "_build_local.bat",
    "_smoke_result.txt",
    "_audit_report.txt",
    "_ui_trace.log",
    "_final_check.py",
    "_restore_deploy.py",
}
FORBIDDEN_PREFIXES = (
    "probe/capture_",
    ".workbuddy/",
    "dist/",
    "build/",
    "build_assets/",
    ".webview2-cache/",
)
FORBIDDEN_SUFFIXES = (".exe", ".bin", ".log", ".pid", ".pyc", ".spec")

RE_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[-:]){5}[0-9A-Fa-f]{2}\b")
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_WIN_USER = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'<>]+", re.I)
RE_UNIX_HOME = re.compile(r"/home/[A-Za-z0-9._-]*[A-Za-z0-9][A-Za-z0-9._-]*")
RE_PRIVATE_KEY = re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")
RE_TOKEN = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
RE_TEN_DIGITS = re.compile(r"\b\d{10}\b")
RE_PHONE = re.compile(r"\b1[3-9]\d{9}\b")


def _tracked_files():
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise unittest.SkipTest("git ls-files 不可用，跳过隐私守卫")
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _read_text(path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace")


def _line(text, pos):
    return text.count("\n", 0, pos) + 1


def scan_text(text):
    """返回 [(类别, 行号)]；值本身一律不返回，防止二次泄露。"""
    found = []

    for m in RE_MAC.finditer(text):
        value = re.sub(r"[^0-9A-Fa-f]", "", m.group(0)).lower()
        if value not in MAC_ALLOW:
            found.append(("MAC", _line(text, m.start())))

    for m in RE_EMAIL.finditer(text):
        value = m.group(0).lower()
        domain = value.rsplit("@", 1)[1]
        allowed = value in EMAIL_ALLOW_EXACT or any(
            domain == d or domain.endswith("." + d) for d in EMAIL_ALLOW_DOMAINS
        )
        if not allowed:
            found.append(("email", _line(text, m.start())))

    for category, regex in (
        ("windows-user-path", RE_WIN_USER),
        ("unix-home-path", RE_UNIX_HOME),
        ("private-key", RE_PRIVATE_KEY),
        ("token", RE_TOKEN),
    ):
        for m in regex.finditer(text):
            found.append((category, _line(text, m.start())))

    for m in RE_TEN_DIGITS.finditer(text):
        if m.group(0) not in TEN_DIGIT_ALLOW:
            found.append(("10-digit", _line(text, m.start())))

    for m in RE_PHONE.finditer(text):
        found.append(("phone", _line(text, m.start())))

    return found


class TestTrackedFiles(unittest.TestCase):
    def test_no_forbidden_paths(self):
        bad = []
        for name in _tracked_files():
            base = name.rsplit("/", 1)[-1]
            if (base in FORBIDDEN_NAMES
                    or name.startswith(FORBIDDEN_PREFIXES)
                    or name.endswith(FORBIDDEN_SUFFIXES)):
                bad.append(name)
        self.assertEqual(bad, [], "禁止提交本地敏感/构建文件: " + ", ".join(bad))

    def test_no_personal_identifiers(self):
        violations = []
        for name in _tracked_files():
            text = _read_text(ROOT / name)
            if text is None:
                continue
            for category, line in scan_text(text):
                violations.append("%s:%d [%s]" % (name, line, category))
        if violations:
            shown = violations[:20]
            extra = "" if len(violations) <= 20 else "\n... 另有 %d 处" % (len(violations) - 20)
            self.fail("发现疑似个人信息（值已隐藏，请本地复核）:\n" + "\n".join(shown) + extra)


class TestPrivacyDetector(unittest.TestCase):
    """守卫本身也要有测试，避免正则失效后静默放行。"""

    def test_flags_unlisted_mac(self):
        fake = "-".join(["11", "22", "33", "44", "55", "66"])
        self.assertIn("MAC", [c for c, _ in scan_text(fake)])

    def test_allows_documented_mac(self):
        self.assertEqual(scan_text("aa-bb-cc-dd-ee-01"), [])

    def test_flags_windows_home(self):
        fake = "C:" + "\\" + "Users" + "\\" + "alice"
        self.assertIn("windows-user-path", [c for c, _ in scan_text(fake)])

    def test_flags_unix_home(self):
        fake = "/home/" + "alice"
        self.assertIn("unix-home-path", [c for c, _ in scan_text(fake)])

    def test_ignores_regex_documentation(self):
        # AGENTS.md 里的本机路径检测命令不能被自己误伤
        self.assertEqual(scan_text("/home/[^/ ]+"), [])
        self.assertEqual(scan_text("/home/..."), [])

    def test_flags_token(self):
        fake = "gh" + "p_" + "a" * 36
        self.assertIn("token", [c for c, _ in scan_text(fake)])

    def test_flags_unlisted_account(self):
        fake = str(1234567890 + 1)
        self.assertIn("10-digit", [c for c, _ in scan_text(fake)])

    def test_flags_email(self):
        fake = "alice@" + "corp" + "." + "com"
        self.assertIn("email", [c for c, _ in scan_text(fake)])

    def test_flags_phone(self):
        fake = "1" + "3" + "9" + "00000000"
        self.assertIn("phone", [c for c, _ in scan_text(fake)])


class TestIgnoreRules(unittest.TestCase):
    def test_sensitive_runtime_paths_ignored(self):
        content = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in ("cred.bin", "config.json", "state.json", "*.log", "*.tmp",
                      "*.lock", "dist/", "HANDOVER.md", "_build_local.bat",
                      "probe/capture_*"):
            with self.subTest(entry=entry):
                self.assertIn(entry, content)

    def test_config_example_uses_synthetic_account(self):
        data = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(data.get("username"), "1234567890")


if __name__ == "__main__":
    unittest.main()
