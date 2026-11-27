using System.Text.Json.Nodes;

namespace CampusNetAuth;

public static class Utils
{
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