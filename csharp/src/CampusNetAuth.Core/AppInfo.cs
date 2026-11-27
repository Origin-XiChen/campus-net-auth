using System.Globalization;

namespace CampusNetAuth;

/// <summary>全局常量与路径。版本以这里为单一来源（对齐 Python 版的 APP_VERSION 约定）。</summary>
public static class AppInfo
{
    public const string AppName = "CampusNetAuth";
    public const string Version = "0.7.0";

    public const string GitHubRepo = "Origin-XiChen/campus-net-auth";
    public const string GitHubReleasesUrl = "https://github.com/" + GitHubRepo + "/releases";
    public const string GitHubApiLatest =
        "https://api.github.com/repos/" + GitHubRepo + "/releases/latest";

    public const int SingletonPort = 47667;
    public const string TaskName = "CampusNetAuth";
    public const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";

    public const string UpdateStateKey = "update_last";
    public const string LogoutPauseKey = "logout_pause_until";
    public const string UpgradeBackupSuffix = ".old.exe";
    public const string DownloadTmpName = "_CampusNetAuth_update.tmp";

    public static string BaseDir { get; } =
        Path.TrimEndingDirectorySeparator(Path.GetFullPath(AppContext.BaseDirectory));

    public static string ExePath { get; } =
        Environment.ProcessPath ?? Path.Combine(BaseDir, AppName + ".exe");

    public static string ConfigPath => Path.Combine(BaseDir, "config.json");
    public static string StatePath => Path.Combine(BaseDir, "state.json");
    public static string CredPath => Path.Combine(BaseDir, "cred.bin");
    public static string LogPath => Path.Combine(BaseDir, "campusnet.log");
    public static string DaemonPidPath => Path.Combine(BaseDir, "daemon.pid");

    /// <summary>把 "0.5.4" 归一成可比较的数字数组；无法解析返回 null。</summary>
    public static int[]? VerKey(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        var parts = text.Trim().Split('.');
        if (parts.Length == 0 || parts.Length > 4) return null;
        var nums = new List<int>(parts.Length);
        foreach (var p in parts)
        {
            if (!int.TryParse(p, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n))
                return null;
            nums.Add(n);
        }
        return nums.ToArray();
    }

    /// <summary>按 Python 的 tuple 语义比较（前缀更短者更小）。</summary>
    public static int CompareVer(int[] a, int[] b)
    {
        var n = Math.Min(a.Length, b.Length);
        for (var i = 0; i < n; i++)
        {
            var c = a[i].CompareTo(b[i]);
            if (c != 0) return c;
        }
        return a.Length.CompareTo(b.Length);
    }

    public static bool IsNewer(string candidate, string current)
    {
        var a = VerKey(candidate);
        var b = VerKey(current);
        return a != null && b != null && CompareVer(a, b) > 0;
    }
}