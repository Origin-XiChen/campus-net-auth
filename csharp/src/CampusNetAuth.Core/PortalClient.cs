using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace CampusNetAuth;

/// <summary>锐捷 ePortal / SAM+ 门户客户端（协议级复刻 Python 版）。</summary>
public sealed class PortalClient
{
    public JsonObject Cfg { get; }
    public string Host { get; }
    public int Port { get; }
    public string BasePath { get; }
    public double Timeout { get; }
    public CookieJar Cookies { get; } = new();

    private string? _userIndex;

    private static readonly (string Code, string Hint)[] ErrorHints =
    {
        ("ERR_USER_FIRSTLOGIN_NEED_CHANGE_PASSWORD", "首次登录必须修改密码，请先在网页端改密"),
        ("ERR_AUTH_USER_OR_PASS_ERROR", "账号或密码错误"),
        ("ERR_USER_ARREARAGE", "账号欠费"),
        ("ERR_USER_BIND_ERROR", "账号已被绑定其它设备/IP"),
        ("ERR_USER_ONLINE_LIMIT", "在线设备数超过限制"),
        ("ERR_USER_FORBIDDEN", "账号被禁用"),
        ("ERR_USER_EXPIRED", "账号已过期"),
        ("ERR_RADIUS_REJECT", "认证服务器拒绝（RADIUS reject）"),
        ("ERR_USER_NOT_EXIST", "账号不存在"),
        ("ERR_PASSWORD_ERROR", "密码错误"),
        ("wait", "网关正在处理，稍后重试即可"),
    };

    public PortalClient(JsonObject cfg)
    {
        Cfg = cfg;
        Host = cfg.GetString("portal_host", "172.16.54.18");
        Port = cfg.GetInt("portal_port", 80);
        BasePath = cfg.GetString("portal_base", "/eportal");
        Timeout = cfg.GetInt("timeout", 8);
    }

    public string Url(string path) => $"http://{Host}:{Port}{path}";

    // ---------- 基础接口 ----------
    private static string FormEscape(string s) =>
        Uri.EscapeDataString(s).Replace("%20", "+");

    public static string FormEncode(IEnumerable<KeyValuePair<string, string>> pairs) =>
        string.Join("&", pairs.Select(kv => FormEscape(kv.Key) + "=" + FormEscape(kv.Value)));

    public JsonObject? Interface(string method, JsonObject? data = null)
    {
        var path = $"{BasePath}/InterFace.do?method={method}";
        var body = new List<KeyValuePair<string, string>>();
        if (data != null)
        {
            foreach (var kv in data)
            {
                var value = kv.Value == null ? "" : kv.Value.ToJsonString().Trim('"');
                body.Add(new KeyValuePair<string, string>(kv.Key, value));
            }
        }
        var headers = new Dictionary<string, string>
        {
            ["Referer"] = Url(BasePath + "/index.jsp"),
            ["X-Requested-With"] = "XMLHttpRequest",
        };
        var r = Http.Request("POST", Host, Port, path, headers,
            Encoding.UTF8.GetBytes(FormEncode(body)), Timeout, Cookies);
        if (r.Status != 200) return null;
        var txt = r.Text().Trim();
        if (txt.Length == 0) return null;
        try
        {
            var node = JsonNode.Parse(txt);
            if (node is JsonObject obj) return obj;
            return new JsonObject { ["_raw"] = txt[..Math.Min(400, txt.Length)] };
        }
        catch
        {
            return new JsonObject { ["_raw"] = txt[..Math.Min(400, txt.Length)] };
        }
    }

    public JsonObject? PageInfo(string queryString = "") =>
        Interface("pageInfo", new JsonObject { ["queryString"] = queryString });

    public JsonObject? OnlineInfo(string? userIndex = null)
    {
        var ui = userIndex ?? _userIndex;
        return string.IsNullOrEmpty(ui)
            ? null
            : Interface("getOnlineUserInfo", new JsonObject { ["userIndex"] = ui });
    }

    public JsonObject? Keepalive(string? userIndex = null)
    {
        var ui = userIndex ?? _userIndex;
        return string.IsNullOrEmpty(ui)
            ? null
            : Interface("keepalive", new JsonObject { ["userIndex"] = ui });
    }

    public JsonObject? GetServices() => Interface("getServices");

    public JsonObject Logout(string? userIndex = null, bool forceByCred = false,
        string? username = null, string? password = null)
    {
        var ui = userIndex ?? _userIndex;
        if (!string.IsNullOrEmpty(ui))
            return Interface("logout", new JsonObject { ["userIndex"] = ui })
                   ?? Fail("门户未响应下线请求");
        if (forceByCred && !string.IsNullOrEmpty(username) && !string.IsNullOrEmpty(password))
            return Interface("logoutByUserIdAndPass",
                       new JsonObject { ["userId"] = username, ["pass"] = password })
                   ?? Fail("门户未响应下线请求");
        return Fail("没有可用的在线会话 userIndex");
    }

    // ---------- 连通性 ----------
    /// <summary>返回 (online, location)：true=已在线；false=被门户劫持；null=探测无结论。</summary>
    public (bool? Online, string? Location) CheckOnline()
    {
        var targets = Cfg["detect_targets"] as JsonArray ?? new JsonArray();
        var list = targets.OfType<JsonObject>().Where(t => !string.IsNullOrEmpty(t.GetString("host"))).ToList();
        var ptime = Math.Min(Timeout, Cfg.GetInt("probe_timeout", 3));
        var results = new List<HttpResponse>(list.Count);

        if (list.Count <= 1)
        {
            foreach (var t in list)
            {
                results.Add(Http.Request("GET", t.GetString("host"), 80,
                    t.GetString("path", "/"), timeout: ptime));
            }
        }
        else
        {
            var tasks = list.Select(t => Task.Run(() =>
                Http.Request("GET", t.GetString("host"), 80,
                    t.GetString("path", "/"), timeout: ptime))).ToArray();
            results.AddRange(Task.WhenAll(tasks).GetAwaiter().GetResult());
        }

        var strict = list.Any(t => !string.IsNullOrEmpty(t.GetString("expect")));
        var anyConnected = false;
        for (var i = 0; i < list.Count; i++)
        {
            var t = list[i];
            var r = results[i];
            if (r.Status < 0) continue;
            anyConnected = true;
            if (r.Status is 301 or 302 or 303 or 307 or 308)
            {
                var loc = r.Get("Location") ?? "";
                if (loc.Contains(Host) || loc.Contains("eportal", StringComparison.OrdinalIgnoreCase))
                    return (false, loc);
                continue;
            }
            if (r.Status == 200)
            {
                var body = r.Text();
                if (body.Contains(Host) || body.Contains("eportal", StringComparison.OrdinalIgnoreCase))
                {
                    var m = Regex.Match(body,
                        "(?:location|href|URL)\\s*=\\s*[\"']([^\"']+)[\"']",
                        RegexOptions.IgnoreCase);
                    if (m.Success && m.Groups[1].Value.Contains(Host))
                        return (false, m.Groups[1].Value);
                    continue;
                }
                var expect = t.GetString("expect");
                if (!string.IsNullOrEmpty(expect) && body.Contains(expect))
                    return (true, null);
                if (string.IsNullOrEmpty(expect) && !strict)
                    return (true, null);
            }
        }
        return anyConnected ? (false, (string?)null) : (null, (string?)null);
    }

    public bool PortalReachable()
    {
        var r = Http.Request("GET", Host, Port, "/",
            timeout: Math.Min(Timeout, Cfg.GetInt("probe_timeout", 3)), cookieJar: Cookies);
        return r.Status > 0;
    }

    // ---------- 登录页发现 ----------
    public static bool LooksLikeEportalLogin(string? html)
    {
        if (string.IsNullOrEmpty(html)) return false;
        if (Regex.IsMatch(html, "action\\s*=\\s*[\"'][^\"']*admin\\.do",
                RegexOptions.IgnoreCase))
            return false;
        if (html.Contains("publickeyexponent", StringComparison.OrdinalIgnoreCase))
            return true;
        return Regex.IsMatch(html, "[\\s\"']id\\s*=\\s*[\"']pwd[\"']",
            RegexOptions.IgnoreCase);
    }

    public static Dictionary<string, string> ParseInputs(string html)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (Match tag in Regex.Matches(html, "<input\\b[^>]*>", RegexOptions.IgnoreCase))
        {
            var attrs = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (Match a in Regex.Matches(tag.Value,
                         "([\\w:-]+)\\s*=\\s*[\"']([^\"']*)[\"']"))
            {
                attrs[a.Groups[1].Value] = a.Groups[2].Value;
            }
            if (attrs.TryGetValue("id", out var id))
                result[id] = attrs.GetValueOrDefault("value", "");
            if (attrs.TryGetValue("name", out var name) && !result.ContainsKey(name))
                result[name] = attrs.GetValueOrDefault("value", "");
        }
        return result;
    }

    public (string? Url, string? Html) DiscoverLoginPage((bool? Online, string? Location)? detection = null)
    {
        var (online, location) = detection ?? CheckOnline();
        if (online == true) return (null, null);

        var seen = new HashSet<string>(StringComparer.Ordinal);

        (string? Url, string? Html) ReadPage(string candidate, HttpResponse? response = null, string? baseUrl = null)
        {
            try
            {
                var absolute = new Uri(new Uri(baseUrl ?? Url("/")), candidate);
                if (absolute.Scheme != "http"
                    || !absolute.Host.Equals(Host, StringComparison.OrdinalIgnoreCase)
                    || absolute.Port != Port
                    || !string.IsNullOrEmpty(absolute.UserInfo))
                    return (null, null);
                var path = absolute.PathAndQuery;
                var url = Url(path);
                if (!seen.Add(url)) return (null, null);
                response ??= Http.Request("GET", Host, Port, path,
                    timeout: Timeout, cookieJar: Cookies);
                if (response.Status == 200)
                {
                    var html = response.Text();
                    if (LooksLikeEportalLogin(html)) return (url, html);
                }
                return (null, null);
            }
            catch
            {
                return (null, null);
            }
        }

        if (!string.IsNullOrEmpty(location))
        {
            var page = ReadPage(location);
            if (page.Html != null) return page;
        }

        var portalAnswered = false;
        foreach (var p in new[] { "/", BasePath + "/index.jsp", BasePath + "/" })
        {
            if (seen.Contains(Url(p))) continue;
            var r = Http.Request("GET", Host, Port, p, timeout: Timeout, cookieJar: Cookies);
            portalAnswered |= r.Status > 0;
            var page = ReadPage(Url(p), r);
            if (page.Html != null) return page;

            string? loc = null;
            if (r.Status is 301 or 302 or 303 or 307 or 308)
            {
                loc = r.Get("Location");
            }
            else if (r.Status == 200)
            {
                var m = Regex.Match(r.Text(),
                    "(?:location|href)\\s*=\\s*[\"']([^\"']+)[\"']",
                    RegexOptions.IgnoreCase);
                if (m.Success) loc = m.Groups[1].Value;
            }
            if (!string.IsNullOrEmpty(loc))
            {
                page = ReadPage(loc, baseUrl: Url(p));
                if (page.Html != null) return page;
            }
        }

        // 兜底：网关不做 302 劫持时，按门户设备参数自己拼登录页 URL
        var query = LoginPageQuery();
        var hints = Cfg["login_page_hints"] as JsonObject;
        if (query.Length > 0)
        {
            var path = $"{BasePath}/index.jsp?{query}";
            var resp = Http.Request("GET", Host, Port, path,
                timeout: Timeout, cookieJar: Cookies);
            var page = ReadPage(Url(path), resp);
            if (page.Html != null) return page;
            if (resp.Status > 0)
            {
                Log.Info("登录页兜底无效：按 login_page_hints 拼出的地址没返回登录页" +
                         "（核对 wlanacname / nasip 是否与 SAM+ 登记的 portal 设备一致）");
            }
        }
        else if (portalAnswered)
        {
            if (hints != null && hints.Count > 0)
            {
                Log.Info("登录页兜底未生效：login_page_hints 缺 nasip / wlanacname（或本机取不到 IPv4）");
            }
            else
            {
                Log.Info("未找到可用登录页：网关没给出重定向，门户根路径也不给登录页。" +
                         "可把浏览器登录时地址栏 index.jsp? 后面的参数填进 config.json 的 login_page_hints");
            }
        }
        return (null, null);
    }

    public (bool? Online, (string Url, string Html)? LoginPage) CheckAuthentication()
    {
        var (online, location) = CheckOnline();
        if (online != null) return (online, null);

        var info = Interface("getOnlineUserInfo");
        if (info != null && !string.IsNullOrEmpty(info.GetString("userIndex"))
            && info.GetString("result", "success").ToLowerInvariant() == "success")
            return (true, null);

        try
        {
            var (url, html) = DiscoverLoginPage((online, location));
            if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(html))
                return (null, null);
            var parsed = new Uri(url);
            if (parsed.Scheme != "http"
                || !parsed.Host.Equals(Host, StringComparison.OrdinalIgnoreCase)
                || parsed.Port != Port
                || !string.IsNullOrEmpty(parsed.UserInfo)
                || string.IsNullOrEmpty(parsed.Query))
                return (null, null);
            var fields = ParseInputs(html);
            var enc = fields.GetValueOrDefault("passwordEncrypt", "").Trim().ToLowerInvariant();
            if (enc == "true")
            {
                if (!(fields.ContainsKey("publicKeyExponent") && fields.ContainsKey("publicKeyModulus")))
                    return (null, null);
            }
            else if (enc != "false" || !fields.ContainsKey("pwd"))
            {
                return (null, null);
            }
            return (false, (url, html));
        }
        catch
        {
            return (null, null);
        }
    }

    // ---------- 登录 ----------
    private static JsonObject Fail(string message, string? hint = null)
    {
        var res = new JsonObject { ["result"] = "fail", ["message"] = message };
        if (!string.IsNullOrEmpty(hint)) res["_hint"] = hint;
        return res;
    }

    private static string Sha256Hex(string text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.ASCII.GetBytes(text))).ToLowerInvariant();

    private static Dictionary<string, List<string>> ParseQuery(string query)
    {
        var result = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        foreach (var part in query.Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var idx = part.IndexOf('=');
            var k = idx >= 0 ? part[..idx] : part;
            var v = idx >= 0 ? part[(idx + 1)..] : "";
            k = Uri.UnescapeDataString(k.Replace("+", " "));
            v = Uri.UnescapeDataString(v.Replace("+", " "));
            if (!result.TryGetValue(k, out var list))
            {
                list = new List<string>();
                result[k] = list;
            }
            list.Add(v);
        }
        return result;
    }

    public JsonObject Login(string username, string password,
        string? serviceHint = null, (string Url, string Html)? loginPage = null)
    {
        string url;
        string html;
        if (loginPage != null)
        {
            (url, html) = loginPage.Value;
        }
        else
        {
            var found = DiscoverLoginPage();
            if (string.IsNullOrEmpty(found.Url) || string.IsNullOrEmpty(found.Html))
                return Fail("未能获取登录页（已在线？或不在校园网环境）");
            url = found.Url!;
            html = found.Html!;
        }

        var queryString = new Uri(url).Query.TrimStart('?');
        var qs = ParseQuery(queryString);
        var mac = qs.TryGetValue("mac", out var macs) && macs.Count > 0 && !string.IsNullOrEmpty(macs[0])
            ? macs[0]
            : "111111111";

        var fields = ParseInputs(html);
        var exp = fields.GetValueOrDefault("publicKeyExponent", "");
        var mod = fields.GetValueOrDefault("publicKeyModulus", "");
        var encryptFlag = fields.GetValueOrDefault("passwordEncrypt", "false");
        var useEncrypt = encryptFlag.ToLowerInvariant() == "true";

        // 新版页面把加密参数留空：按页面 JS 同样的方式向 pageInfo 补齐
        JsonObject? pageDetail = null;
        if (!useEncrypt || string.IsNullOrEmpty(exp) || string.IsNullOrEmpty(mod))
        {
            pageDetail = PageInfo(queryString);
            if (pageDetail != null)
            {
                var pExp = pageDetail.GetString("publicKeyExponent").Trim();
                var pMod = pageDetail.GetString("publicKeyModulus").Trim();
                if (pageDetail.GetString("passwordEncrypt").ToLowerInvariant() == "true"
                    && pExp.Length > 0 && pMod.Length > 0)
                {
                    useEncrypt = true;
                    exp = pExp;
                    mod = pMod;
                }
            }
        }

        // 门户公钥 pinning（TOFU）
        var pinOn = Cfg.GetBool("pin_pubkey", true);
        string? fp = null;
        if (pinOn)
        {
            var pinned = State.Load().GetString("portal_pubkey_fp");
            if (!useEncrypt)
            {
                if (!string.IsNullOrEmpty(pinned))
                {
                    return Fail(
                        "门户登录页未启用加密，与首次记录不符（疑似伪装门户）。如确认学校变更，" +
                        "请删除 state.json 的 portal_pubkey_fp 字段后重试，或将 pin_pubkey 设为 false",
                        "门户公钥校验失败");
                }
            }
            else if (!string.IsNullOrEmpty(exp) && !string.IsNullOrEmpty(mod))
            {
                fp = Sha256Hex(exp.Trim().ToLowerInvariant() + ":" + mod.Trim().ToLowerInvariant());
                if (!string.IsNullOrEmpty(pinned) && pinned != fp)
                {
                    return Fail(
                        "门户 RSA 公钥指纹与首次记录不一致（疑似伪装门户/中间人）。如确认学校更换密钥，" +
                        "删除 state.json 的 portal_pubkey_fp 字段后重试，或将 pin_pubkey 设为 false",
                        "门户公钥指纹变化");
                }
            }
        }

        var service = serviceHint ?? Cfg.GetString("service", "default");
        if (service == "auto") service = AutoService(queryString, pageDetail);

        string pwdSend;
        if (useEncrypt)
        {
            if (string.IsNullOrEmpty(exp) || string.IsNullOrEmpty(mod))
                return Fail("登录页要求加密但未找到 RSA 公钥");
            try
            {
                pwdSend = Crypto.RsaEncrypt(Crypto.LoginPlain(password, mac), exp, mod);
            }
            catch (Exception e)
            {
                return Fail("RSA 加密失败: " + e.Message);
            }
        }
        else
        {
            pwdSend = password;
        }

        var payload = new List<KeyValuePair<string, string>>
        {
            new("userId", Crypto.Dq(username)),
            new("password", Crypto.Dq(pwdSend)),
            new("service", Crypto.Dq(service)),
            new("queryString", Crypto.Dq(queryString)),
            new("operatorPwd", ""),
            new("operatorUserId", ""),
            new("validcode", ""),
            new("passwordEncrypt", Crypto.Dq(useEncrypt ? "true" : "false")),
        };
        var body = string.Join("&", payload.Select(kv => kv.Key + "=" + kv.Value));
        var path = $"{BasePath}/InterFace.do?method=login";
        var headers = new Dictionary<string, string> { ["Referer"] = url };
        var r = Http.Request("POST", Host, Port, path, headers,
            Encoding.UTF8.GetBytes(body), Timeout, Cookies);
        if (r.Status != 200) return Fail($"HTTP {r.Status}");

        var txt = r.Text().Trim();
        JsonObject res;
        try
        {
            res = JsonNode.Parse(txt) as JsonObject
                  ?? throw new FormatException("not an object");
        }
        catch
        {
            return Fail("响应解析失败: " + txt[..Math.Min(200, txt.Length)]);
        }

        if (res.GetString("result") == "success")
        {
            _userIndex = res.GetString("userIndex");
            res["_service"] = service;
            res["_encrypted"] = useEncrypt;
            if (pinOn && !string.IsNullOrEmpty(fp)
                && string.IsNullOrEmpty(State.Load().GetString("portal_pubkey_fp")))
            {
                try { State.Update(new JsonObject { ["portal_pubkey_fp"] = fp }); }
                catch { }
            }
        }
        else
        {
            var msg = res.GetString("message");
            foreach (var (code, hint) in ErrorHints)
            {
                if (msg.Contains(code, StringComparison.Ordinal))
                {
                    res["_hint"] = hint;
                    break;
                }
            }
            var blocked = AuthBlockedReason(queryString);
            if (blocked.Length > 0)
            {
                res["_blocked"] = blocked;
                if (res["_hint"] == null) res["_hint"] = blocked;
            }
        }
        return res;
    }

    public string AuthBlockedReason(string queryString = "")
    {
        var info = PageInfo(queryString);
        if (info == null) return "";
        if (info.GetString("isToCasPage").ToLowerInvariant() == "true")
            return "门户已改为 CAS/统一身份认证登录，无法用账号密码自动认证";
        if (info.GetString("isCheckSmsAuth").ToLowerInvariant() == "true")
            return "门户要求短信二次验证，无法自动认证";
        if (!string.IsNullOrWhiteSpace(info.GetString("validCodeUrl")))
            return "门户登录页已启用验证码，无法自动认证";
        return "";
    }

    private string AutoService(string queryString = "", JsonObject? info = null)
    {
        info ??= PageInfo(queryString);
        if (info?["service"] is JsonObject services)
        {
            foreach (var kv in services)
            {
                if (kv.Value is JsonObject v && v.GetString("serviceDefault") == "true")
                    return kv.Key;
            }
        }
        return "default";
    }

    // ---------- 本机地址 ----------
    public static string LocalLanIpv4(string host)
    {
        if (string.IsNullOrEmpty(host)) return "";
        try
        {
            using var udp = new UdpClient();
            udp.Client.ReceiveTimeout = 500;
            udp.Connect(host, 80);
            if (udp.Client.LocalEndPoint is IPEndPoint ep)
            {
                var ip = ep.Address.ToString();
                return ip.StartsWith("0.") ? "" : ip;
            }
        }
        catch { }
        return "";
    }

    private static string MacOf(NetworkInterface ni)
    {
        try
        {
            var bytes = ni.GetPhysicalAddress().GetAddressBytes();
            if (bytes.Length < 6) return "";
            var mac = string.Join("-", bytes.Take(6).Select(b => b.ToString("x2")));
            return mac == "00-00-00-00-00-00" ? "" : mac;
        }
        catch
        {
            return "";
        }
    }

    public static string LocalMacForIp(string ip)
    {
        if (string.IsNullOrEmpty(ip)) return "";
        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                var mac = MacOf(ni);
                if (mac.Length == 0) continue;
                foreach (var ua in ni.GetIPProperties().UnicastAddresses)
                {
                    if (ua.Address.AddressFamily == AddressFamily.InterNetwork
                        && ua.Address.ToString() == ip)
                        return mac;
                }
            }
        }
        catch { }
        return "";
    }

    public static string LocalMacText()
    {
        try
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                var mac = MacOf(ni);
                if (mac.Length > 0) return mac;
            }
        }
        catch { }
        return "";
    }

    public string LoginPageQuery()
    {
        if (Cfg["login_page_hints"] is not JsonObject hints || hints.Count == 0) return "";
        var nasip = hints.GetString("nasip").Trim();
        var acname = hints.GetString("wlanacname").Trim();
        if (nasip.Length == 0 || acname.Length == 0) return "";

        var ip = hints.GetString("wlanuserip").Trim();
        if (ip.Length == 0) ip = LocalLanIpv4(Host);
        if (ip.Length == 0) return "";

        var mac = hints.GetString("wlanparameter").Trim();
        if (mac.Length == 0) mac = LocalMacForIp(ip);
        if (mac.Length == 0) mac = LocalMacText();

        var fields = new List<KeyValuePair<string, string>>
        {
            new("wlanuserip", ip),
            new("wlanacname", acname),
            new("nasip", nasip),
        };
        if (mac.Length > 0) fields.Add(new("wlanparameter", mac));
        fields.Add(new("url", hints.GetString("url")));
        return FormEncode(fields);
    }
}