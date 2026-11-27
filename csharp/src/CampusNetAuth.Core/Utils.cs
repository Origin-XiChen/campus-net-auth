using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace CampusNetAuth;

public static class Utils
{
    private static readonly Regex HostPattern =
        new(@"^[A-Za-z0-9.\-]{1,253}$", RegexOptions.Compiled);

    /// <summary>
    /// 解析界面里的「门户地址」：与 Python 版一致，支持 host 或 host:port。
    /// 非法输入（协议头/路径/怪字符）返回 null，调用方保持原值。
    /// </summary>
    public static (string? Host, int? Port) ParseHostPort(string? input)
    {
        if (string.IsNullOrWhiteSpace(input)) return (null, null);
        var s = input.Trim();
        if (s.Contains("://") || s.Contains('/') || s.Contains('\\')) return (null, null);
        var idx = s.IndexOf(':');
        var host = idx >= 0 ? s[..idx].Trim() : s;
        var portText = idx >= 0 ? s[(idx + 1)..].Trim() : "";
        string? okHost = HostPattern.IsMatch(host) ? host : null;
        int? port = null;
        if (portText.Length > 0 && portText.All(char.IsDigit)
            && int.TryParse(portText, out var p) && p is >= 1 and <= 65535)
            port = p;
        return (okHost, port);
    }
    public static string MaskUser(string? u)
    {
        if (string.IsNullOrEmpty(u) || u.Length <= 4) return u ?? string.Empty;
        return u[..2] + new string('*', u.Length - 4) + u[^2..];
    }

    public static string FmtUptime(double seconds)
    {
        var total = (long)Math.Max(0, seconds);
        var m = total / 60;
        var s = total % 60;
        var h = m / 60;
        m %= 60;
        return h > 0 ? $"{h}:{m:00}:{s:00}" : $"{m:00}:{s:00}";
    }

    public static string FmtCheckedAt(long ts)
    {
        try
        {
            return ts <= 0
                ? "未知"
                : DateTimeOffset.FromUnixTimeSeconds(ts).ToLocalTime()
                    .ToString("MM-dd HH:mm");
        }
        catch
        {
            return "未知";
        }
    }

    public static long Now() => DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    public static string SafeMessage(JsonObject? res, string fallback = "未知错误")
    {
        if (res != null)
        {
            var msg = res.GetString("message");
            if (!string.IsNullOrEmpty(msg)) return msg;
            var raw = res.GetString("_raw");
            if (!string.IsNullOrEmpty(raw)) return raw;
        }
        return fallback;
    }

    public static string Truncate(string s, int max) =>
        string.IsNullOrEmpty(s) ? s : (s.Length <= max ? s : s[..max]);
}