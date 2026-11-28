namespace CampusNetAuth.Tests;

using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

public static class RsaGoldenTests
{
    private const string Exp = "10001";
    private const string Mod =
        "9c2899b8ceddf9beafad2db8e431884a79fd9b9c881e459c0e1963984779d66" +
        "12222cee814593cc458845bbba42b2d3474c10b9d31ed84f256c6e3a1c795e68" +
        "e18585b84650076f122e763289a4bcb0de08762c3ceb591ec44d764a69817318" +
        "fbce09d6ecb0364111f6f38e90dc44ca89745395a17483a778f1cc8dc990d87c3";

    private static readonly (string Name, string Plain, string Expected)[] Vectors =
    {
        ("basic", "Test@123456>AABBCCDDEE01",
            "0757c69ef2257a6633d8fdf1fdd087a245914d6092d5e35aa70ceebfffa2edb7" +
            "03567d61b5b27006a63589e2ffd0e18cf4c90a09206f4e8a79caa2c15f967dc0" +
            "b89bde178513f9bb4ecdeab59147a223d6d332642b24eaa333b4600cc2379156" +
            "7357e23fb9dfa76eac89af49bb82e27df57313626222ef8694ac0f25a2d573e1"),
        ("empty_mac", "abc123>",
            "6b7e5d4294d176ce5bc23f35e18ec00f9bb1f9215b6094088e4d671aa51ae143" +
            "30e71284bdb2d5626eaab702d21daf2dc73ea3e33b512717d7acf70b1a721878" +
            "25d822311677c654d845b2a00e5ca137a8de0d4484d2639ad0d4c6f2799fb7a5" +
            "c32dc0a092be1c88a40b83491862e52a5bca711c34f596adf15b6bfc8d2564d4"),
        ("single_char", "a",
            "2955aea7a5fb7ab2952139fedda5863de4b0e1f2c3b4c6d26567c40d1d169264" +
            "f44bdf649d9a2cd44cf3a01ee57ca44118ed5057a456549ce9bc9136924977f4" +
            "da3b736df9cfc621bc6c22a709d6d5de0fa7055ddeb615a92f4c114662d87616" +
            "1dff6a28397dd14adf3051852c62c31e7f38053450da8961fea1d99c699dafa8"),
        ("numeric_user", "1234567890>AABBCCDDEE01",
            "147afeea65e02ad15621738cc7f3dd3312964b7e3a8a9eb51a8a8cd8b9e5d5e6" +
            "6ddbd76255a25b27e13397da039ebf160ca21cbd3f3801160d6442a20059761b" +
            "8494e1fb5082ea9fb1104d9de9a043e359372ea1fd933027aeb1feb72e2d9fba" +
            "ce876a0e660a7e38d3b8859f150936708bfeb6b690b94ddf20e76c3c2ddad890"),
        ("symbols", "P@ssw0rd!#$%^&*()>00-11-22-33-44-55",
            "98539f063fff57d44a9cb00525df4ddd42b64dc6e7b4c3eccebad70506ba55d914" +
            "c27be9c3b82b099af856965775965273dd0ee19a6e4a445365e4587739e9e00115" +
            "dd4e1c50295ae96b4d8758871501e08105a7f87229bb01432db0a1957df780540" +
            "98d045a08c214c57ab0a22b3803337223ae0a312598c83b6fae78ca8838"),
        ("one_full_block_no_pad", new string('x', 126),
            "4c253531a57c8b910edf89625d27b806386b42747c77002fbee56a0ba86f7ab69b" +
            "4a9530689c1676b1505b99d31018b9c310261e0e6fe5773b362c444eeb3097a0d4" +
            "5a24cbba77a0e68ce750a31ed0c7209f4ce23332478f1c5989b8d1850df59b666c" +
            "3eb16aeb4e8fbaa80d6d091d6265fd4d521fbc9ea46ff4fbf25f0a512c"),
        ("cross_block", new string('y', 127),
            "6d9b351bbe1f0d9e02eb8478b2338f88b615cd34b27afbc3c4b40bc75f8c2b2e19" +
            "ee15def3686baf36e8af44cd1128fed87c098c52d9a4bc35a8cf2493202e537a03" +
            "8d6644987df86402ba30b956526b7d8cd4962384c4d508c0f1ea0dacdc4009fabe" +
            "4a63576b4c4bfa829883072be99ac07d3d72d4ebb29491bfde402710de " +
            "17fa478ad8f0ca48d433a38e43798071e5c101b57f3e7a5fc4f80cfaad56489741" +
            "ecdea38f3764bcb7c8b813158fa795736639f550289f03b8d136005e7ef0739181" +
            "91c7f5d9fd110cdd61b0b9b6ae4b2735d08dec3d6e01eef3254d2ff135bfe2c250" +
            "0734331f1252b885a76940dfd5c0bebfbbf69faeb1f3868f811d0ed725"),
    };

    public static void GoldenVectors()
    {
        foreach (var (name, plain, expected) in Vectors)
        {
            var got = Crypto.RsaEncrypt(Crypto.ReverseUnits(plain), Exp, Mod);
            Assert.Equal(expected, got, $"向量 {name}");
        }
    }

    public static void BlockStructure()
    {
        var one = Crypto.RsaEncrypt(new string('x', 126), Exp, Mod);
        Assert.DoesNotContain(" ", one, "单块密文");
        Assert.Equal(256, one.Length, "单块长度");

        var parts = Crypto.RsaEncrypt(new string('y', 127), Exp, Mod).Split(' ');
        Assert.Equal(2, parts.Length, "双块数量");
        foreach (var p in parts) Assert.Equal(256, p.Length, "每块长度");
    }

    public static void ReverseUnitsMatchesJs()
    {
        Assert.Equal("cba", Crypto.ReverseUnits("abc"));
        var reversed = Crypto.ReverseUnits("a\U0001F600"); // a + 😀（代理对）
        Assert.Equal(3, reversed.Length, "code unit 数");
        Assert.Equal('\uDE00', reversed[0], "低代理在前");
        Assert.Equal('\uD83D', reversed[1], "高代理在后");
        Assert.Equal('a', reversed[2], "ASCII 在最后");
    }

    public static void DoubleUrlEncode()
    {
        Assert.Equal("a%2520b", Crypto.Dq("a b"));
        Assert.Equal("P%2540ss", Crypto.Dq("P@ss"));
        Assert.Equal("a%252Bb", Crypto.Dq("a+b"));
    }
}

public static class VersionTests
{
    public static void ComparesNumerically()
    {
        Assert.True(AppInfo.IsNewer("0.10.0", "0.9.0"), "0.10 > 0.9");
        Assert.True(AppInfo.IsNewer("0.6.4", "0.6.3"), "0.6.4 > 0.6.3");
        Assert.False(AppInfo.IsNewer("0.6.3", "0.6.3"), "同版本");
        Assert.False(AppInfo.IsNewer("0.6", "0.6.0"), "前缀更短更小");
        Assert.True(AppInfo.IsNewer("0.6.0", "0.6"), "0.6.0 > 0.6");
        Assert.False(AppInfo.IsNewer("bad", "0.6.1"), "非法版本");
    }

    public static void PrereleaseSuffix()
    {
        Assert.True(AppInfo.IsNewer("1.0.0-preview.1", "0.7.0"), "预发布高于旧版");
        Assert.True(AppInfo.IsNewer("1.0.0-preview.2", "0.6.4"), "预发布高于旧版（另一组）");
        // 只按数字段比较：1.0.0 与 1.0.0-preview.1 视为同段相等（预发布先后交给 Velopack）
        Assert.False(AppInfo.IsNewer("1.0.0", "1.0.0-preview.1"), "同数字段不判新");
        Assert.Equal(3, AppInfo.VerKey("1.0.0-preview.1")!.Length, "后缀被剥离");
        Assert.Equal(3, AppInfo.VerKey("1.0.0+build.5")!.Length, "构建元数据被剥离");
    }

    /// <summary>回归：VerQueryValue 的长度是字符数，ProductName 不能被截断。</summary>
    public static void PeProductNameNotTruncated()
    {
        var dll = typeof(Upgrade).Assembly.Location;
        var info = Upgrade.PeVersion(dll);
        Assert.True(info != null, "读取程序集版本资源");
        Assert.Equal("CampusNetAuth.Core", info!.Value.Product, "ProductName 完整读取");
        // PE 的 FileVersion 只能数字段（1.0.0.0），与 SemVer 预发布号按数字段对齐
        Assert.Equal(AppInfo.VerKey(AppInfo.Version)![0], AppInfo.VerKey(info.Value.Version)![0], "主版本");
        Assert.Equal(
            string.Join(".", AppInfo.VerKey(AppInfo.Version)!.Take(3)),
            info.Value.Version, "版本主三段");
    }
}

public static class LegacyCleanupTests
{
    public static void SafetyValve()
    {
        Assert.True(LegacyCleanup.IsUnsafeDir(AppInfo.BaseDir), "当前程序目录");
        Assert.True(LegacyCleanup.IsUnsafeDir(
            Path.GetDirectoryName(Path.TrimEndingDirectorySeparator(AppInfo.BaseDir))!), "安装根目录");
        Assert.True(LegacyCleanup.IsUnsafeDir(Path.GetPathRoot(AppInfo.BaseDir)!), "盘根");
        Assert.True(LegacyCleanup.IsUnsafeDir(@"\\server\share\CampusNetAuth"), "网络路径");
        Assert.True(LegacyCleanup.IsUnsafeDir(""), "空路径");
        Assert.False(LegacyCleanup.IsUnsafeDir(@"C:\OldApps\CampusNetAuth"), "普通旧目录");
    }

    public static void PathUnder()
    {
        Assert.True(LegacyCleanup.IsUnder(@"C:\Apps\Old\CampusNetAuth.exe", @"C:\Apps\Old"), "直接子文件");
        Assert.True(LegacyCleanup.IsUnder(@"C:\Apps\Old\sub\x.exe", @"C:\Apps\Old\"), "子目录");
        Assert.False(LegacyCleanup.IsUnder(@"C:\Apps\Older\CampusNetAuth.exe", @"C:\Apps\Old"), "同前缀不同目录");
        Assert.False(LegacyCleanup.IsUnder(@"C:\Apps\Old", @"C:\Apps\Old"), "目录本身不算在内");
    }
}

public static class ConfigTests
{
    public static void SanitizeFallsBack()
    {
        var cfg = Config.Defaults();
        cfg["service"] = "移动";
        cfg["interval"] = "abc";
        cfg["logout_pause"] = 10_000_000;
        cfg["portal_base"] = "eportal/";
        cfg["username"] = "  u  ";
        var s = Config.Sanitize(cfg);
        Assert.Equal("default", s.GetString("service"));
        Assert.Equal(30, s.GetInt("interval"));
        Assert.Equal(86400, s.GetInt("logout_pause"));
        Assert.Equal("/eportal", s.GetString("portal_base"));
        Assert.Equal("u", s.GetString("username"));

        cfg = Config.Defaults();
        cfg["service"] = "yd";
        Assert.Equal("YD", Config.Sanitize(cfg).GetString("service"));
        cfg["service"] = " Auto ";
        Assert.Equal("auto", Config.Sanitize(cfg).GetString("service"));
    }

    public static void SanitizeHints()
    {
        var cfg = Config.Defaults();
        cfg["login_page_hints"] = System.Text.Json.Nodes.JsonValue.Create(false);
        Assert.Equal(0, (Config.Sanitize(cfg)["login_page_hints"] as System.Text.Json.Nodes.JsonObject)!.Count);

        cfg = Config.Defaults();
        cfg["login_page_hints"] = new System.Text.Json.Nodes.JsonObject { ["nasip"] = "10.1.2.3" };
        var hints = (System.Text.Json.Nodes.JsonObject)Config.Sanitize(cfg)["login_page_hints"]!;
        Assert.Equal("10.1.2.3", hints.GetString("nasip"));
        Assert.Equal("logic", hints.GetString("wlanacname"));
    }

    public static void SanitizeClamps()
    {
        var cfg = Config.Defaults();
        cfg["interval"] = 1;
        cfg["timeout"] = 999;
        cfg["probe_timeout"] = 0;
        cfg["logout_pause"] = -5;
        cfg["max_interval"] = 10;
        cfg["keepalive_interval"] = 1;
        var s = Config.Sanitize(cfg);
        Assert.Equal(5, s.GetInt("interval"), "interval 下限");
        Assert.Equal(60, s.GetInt("timeout"), "timeout 上限");
        Assert.Equal(1, s.GetInt("probe_timeout"), "probe_timeout 下限");
        Assert.Equal(0, s.GetInt("logout_pause"), "logout_pause 下限");
        Assert.Equal(60, s.GetInt("max_interval"), "max_interval 下限");
        Assert.Equal(30, s.GetInt("keepalive_interval"), "keepalive 下限");

        cfg = Config.Defaults();
        cfg["timeout"] = "abc";
        Assert.Equal(8, Config.Sanitize(cfg).GetInt("timeout"), "坏值回落默认");
    }
}

public static class CryptoExtraTests
{
    public static void LoginPlainDefaultMac()
    {
        Assert.Equal("111111111>cba", Crypto.LoginPlain("abc", ""));
        Assert.Equal("bb-aa>cba", Crypto.LoginPlain("abc", "aa-bb"));
    }
}

public static class HtmlTests
{
    private const string StudentPage =
        "<html><body><input id=\"username\" name=\"username\" type=\"text\">" +
        "<input id=\"pwd\" name=\"pwd\" type=\"password\">" +
        "<input type=\"hidden\" id=\"publicKeyExponent\" value=\"10001\">" +
        "</body></html>";

    private const string AdminPage =
        "<html><body><form action=\"./admin.do?method=login\">" +
        "<input id=\"pwd\" name=\"pwd\" type=\"password\"></form></body></html>";

    public static void LooksLikeLoginPage()
    {
        Assert.True(PortalClient.LooksLikeEportalLogin(StudentPage), "学生登录页");
        Assert.False(PortalClient.LooksLikeEportalLogin(AdminPage), "管理台页面");
        Assert.False(PortalClient.LooksLikeEportalLogin(""), "空页面");
        Assert.False(PortalClient.LooksLikeEportalLogin(
            "<script>alert('WEB认证设备未注册')</script>"), "错误页");
        Assert.True(PortalClient.LooksLikeEportalLogin(
            "<input id='pwd' name='pwd' type='password'>"), "单引号 pwd");
        Assert.False(PortalClient.LooksLikeEportalLogin(
            "<form action='./ADMIN.do?method=login'><input id='pwd'></form>"),
            "管理台 action 大小写不敏感");
        Assert.False(PortalClient.LooksLikeEportalLogin(
            "<input id='password' name='password'>"), "非 pwd 字段");
    }

    public static void ParseInputs()
    {
        var fields = PortalClient.ParseInputs(
            "<input id=\"a\" name=\"x\" value=\"1\">" +
            "<input name=\"y\" value=\"2\">" +
            "<input name=\"z\" value=\"3\" id=\"z2\">");
        Assert.Equal("1", fields["a"]);
        Assert.Equal("1", fields["x"]);
        Assert.Equal("2", fields["y"]);
        Assert.Equal("3", fields["z2"]);
        Assert.Equal("3", fields["z"]);

        var more = PortalClient.ParseInputs(
            "<input id='p' name='q' value=''>" +
            "<input type=\"hidden\" id=\"publicKeyExponent\" value=\"10001\">");
        Assert.Equal("", more["p"]);
        Assert.Equal("10001", more["publicKeyExponent"]);
    }

    public static void FormEncoding()
    {
        var encoded = PortalClient.FormEncode(new[]
        {
            new KeyValuePair<string, string>("a b", "c d"),
            new KeyValuePair<string, string>("k", "x@y+z%"),
            new KeyValuePair<string, string>("cn", "学"),
        });
        Assert.Equal("a+b=c+d&k=x%40y%2Bz%25&cn=%E5%AD%A6", encoded);
    }
}

public static class DaemonGuardTests
{
    public static void ManualStopMarker()
    {
        State.Save(new JsonObject());
        Assert.False(DaemonGuard.ManuallyStoppedThisBoot(), "初始无标记");
        DaemonGuard.MarkManualStop();
        Assert.True(DaemonGuard.ManuallyStoppedThisBoot(), "标记生效");
        DaemonGuard.ClearManualStop();
        Assert.False(DaemonGuard.ManuallyStoppedThisBoot(), "清除后失效");

        // 模拟重启：存量 tick 大于本次开机时长 → 标记应失效并清掉
        State.Update(new JsonObject
        {
            ["daemon_manual_stop_tick"] = Environment.TickCount64 + 60_000_000,
        });
        Assert.False(DaemonGuard.ManuallyStoppedThisBoot(), "重启后失效");
        Assert.IsNull(State.Load()["daemon_manual_stop_tick"], "失效后清键");
    }
}

public static class CredentialStoreTests
{
    public static void PythonHeaderAndLegacyRaw()
    {
        var path = AppInfo.CredPath;
        try
        {
            if (File.Exists(path)) File.Delete(path);
            Assert.True(CredentialStore.Save("Test@123456"), "保存");
            var bytes = File.ReadAllBytes(path);
            Assert.True(bytes.Length > 6
                        && bytes[0] == (byte)'D' && bytes[1] == (byte)'P'
                        && bytes[2] == (byte)'A' && bytes[3] == (byte)'P'
                        && bytes[4] == (byte)'I' && bytes[5] == 1,
                "DPAPI\\x01 头（与 Python 版一致）");
            Assert.Equal("Test@123456", CredentialStore.Load(), "Python 头格式回读");

            // 0.7.0 早期 C# 写入的裸 DPAPI blob 仍要能读
            var legacy = ProtectedData.Protect(
                Encoding.UTF8.GetBytes("Legacy@1"), null, DataProtectionScope.CurrentUser);
            File.WriteAllBytes(path, legacy);
            Assert.Equal("Legacy@1", CredentialStore.Load(), "裸格式兼容");
        }
        finally
        {
            try { if (File.Exists(path)) File.Delete(path); } catch { }
        }
    }
}

public static class HostPortTests
{
    public static void ParsesHostPort()
    {
        var (h, p) = Utils.ParseHostPort("172.16.54.18:80");
        Assert.Equal("172.16.54.18", h, "host");
        Assert.Equal(80, p, "port");
        (h, p) = Utils.ParseHostPort("172.16.54.18");
        Assert.Equal("172.16.54.18", h, "无端口 host");
        Assert.IsNull(p, "无端口");
        (h, p) = Utils.ParseHostPort("portal.example.edu:8443");
        Assert.Equal("portal.example.edu", h, "主机名");
        Assert.Equal(8443, p, "端口");
        (h, p) = Utils.ParseHostPort("http://172.16.54.18:80/eportal");
        Assert.IsNull(h, "拒绝协议头/路径");
        Assert.IsNull(p, "拒绝协议头/路径端口");
        (h, p) = Utils.ParseHostPort("bad host:80");
        Assert.IsNull(h, "拒绝怪字符");
        Assert.Equal(80, p, "端口仍可解析");
    }

    public static void SanitizeRepairsHostPort()
    {
        var cfg = Config.Defaults();
        cfg["portal_host"] = "172.16.54.18:80";
        cfg["portal_port"] = 80;
        var s = Config.Sanitize(cfg);
        Assert.Equal("172.16.54.18", s.GetString("portal_host"), "host 去端口");
        Assert.Equal(80, s.GetInt("portal_port"), "端口保持");

        cfg = Config.Defaults();
        cfg["portal_host"] = "172.16.54.18:8080";
        cfg["portal_port"] = 80;
        s = Config.Sanitize(cfg);
        Assert.Equal("172.16.54.18", s.GetString("portal_host"), "host 去端口");
        Assert.Equal(8080, s.GetInt("portal_port"), "端口被地址覆盖");
    }
}

public static class UtilsTests
{
    public static void MaskAndUptime()
    {
        Assert.Equal("12******90", Utils.MaskUser("1234567890"));
        Assert.Equal("abcd", Utils.MaskUser("abcd"));
        Assert.Equal("ab*de", Utils.MaskUser("abcde"));
        Assert.Equal("", Utils.MaskUser(""));

        Assert.Equal("00:00", Utils.FmtUptime(0));
        Assert.Equal("00:59", Utils.FmtUptime(59));
        Assert.Equal("01:00", Utils.FmtUptime(60));
        Assert.Equal("1:00:00", Utils.FmtUptime(3600));
        Assert.Equal("1:01:01", Utils.FmtUptime(3661));
        Assert.Equal("00:00", Utils.FmtUptime(-5), "负数按 0");

        Assert.Equal("abc", Utils.Truncate("abc", 3));
        Assert.Equal("ab", Utils.Truncate("abc", 2));
        Assert.Equal("移动", Utils.ServiceLabel("YD"));
        Assert.Equal("校园网", Utils.ServiceLabel("default"));
        Assert.Equal("自动", Utils.ServiceLabel("auto"));
    }
}

public static class StateTests
{
    public static void LogoutPauseRoundTrip()
    {
        State.Save(new JsonObject());
        Assert.Equal(0L, LogoutPause.Left(), "初始静默期");

        var until = LogoutPause.Set(3600);
        Assert.True(until > Utils.Now(), "静默期在未来");
        var left = LogoutPause.Left();
        Assert.True(left > 3500 && left <= 3600, $"剩余秒数异常：{left}");

        Assert.True(LogoutPause.Clear(), "清除静默期");
        Assert.Equal(0L, LogoutPause.Left(), "清除后归零");
        Assert.IsNull(State.Load()[AppInfo.LogoutPauseKey], "状态键已移除");
    }

    public static void EventLogMergeAndTrim()
    {
        State.Save(new JsonObject());
        EventLog.Record("blocked", "第一次");
        EventLog.Record("blocked", "第二次");
        var events = State.Load()["events"] as JsonArray;
        Assert.Equal(1, events!.Count, "同事件 60s 内合并");
        Assert.Equal("第一次", ((JsonObject)events[0]!).GetString("msg"), "合并保留原文案");

        for (var i = 0; i < 10; i++) EventLog.Record("k" + i, "m" + i);
        events = State.Load()["events"] as JsonArray;
        Assert.Equal(8, events!.Count, "最多保留 8 条");
        Assert.Equal("k9", ((JsonObject)events[^1]!).GetString("kind"), "最新事件在末尾");
    }
}