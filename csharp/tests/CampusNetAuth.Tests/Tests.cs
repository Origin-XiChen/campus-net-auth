namespace CampusNetAuth.Tests;

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
    }
}