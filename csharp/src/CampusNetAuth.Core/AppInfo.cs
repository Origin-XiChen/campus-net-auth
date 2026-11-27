using System.Globalization;

namespace CampusNetAuth;

/// <summary>全局常量与路径。版本以这里为单一来源（对齐 Python 版的 APP_VERSION 约定）。</summary>
public static class AppInfo
{
    public const string AppName = "CampusNetAuth";
    // 1.0.0 起切安装版（Velopack）；预发布号用 SemVer2（-preview.N）
    public const string Version = "1.0.0-preview.1";

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

    /// <summary>是否运行在安装器（Velopack）布局里：&lt;root&gt;\current\... 旁边有 Update.exe。</summary>
    public static bool IsInstalled { get; } = DetectInstalled();

    /// <summary>
    /// 用户数据目录：开发/绿色版在 exe 同目录；安装版在 %APPDATA%\CampusNetAuth
    /// （安装目录可能只读，且升级会替换整个 current 目录）。
    /// 可用环境变量 CNA_DATA_DIR 覆盖（测试/便携场景）。
    /// </summary>
    public static string DataDir { get; } = ResolveDataDir();

    private static bool DetectInstalled()
    {
        try
        {
            var parent = Directory.GetParent(BaseDir)?.FullName;
            return parent != null && File.Exists(Path.Combine(parent, "Update.exe"));
        }
        catch
        {
            return false;
        }
    }

    private static string ResolveDataDir()
    {
        var env = Environment.GetEnvironmentVariable("CNA_DATA_DIR");
        if (!string.IsNullOrWhiteSpace(env)) return Path.GetFullPath(env);
        if (IsInstalled)
        {
            return Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                AppName);
        }
        return BaseDir;
    }

    /// <summary>安装版首次启动：把 exe 同目录的旧数据搬到 DataDir（不覆盖已有文件）。</summary>
    public static void MigrateDataIfNeeded()
    {
        if (string.Equals(DataDir, BaseDir, StringComparison.OrdinalIgnoreCase)) return;
        try
        {
            Directory.CreateDirectory(DataDir);
            foreach (var name in new[] { "config.json", "cred.bin", "state.json", "campusnet.log" })
            {
                var src = Path.Combine(BaseDir, name);
                var dst = Path.Combine(DataDir, name);
                if (!File.Exists(src) || File.Exists(dst)) continue;
                try
                {
                    File.Copy(src, dst);
                    Log.Info("已迁移数据文件到 {0}：{1}", DataDir, name);
                }
                catch (Exception e)
                {
                    Log.Warn("迁移数据文件失败（{0}）: {1}", name, e.Message);
                }
            }
        }
        catch (Exception e)
        {
            Log.Warn("准备数据目录失败: {0}", e.Message);
        }
    }

    public static string ConfigPath => Path.Combine(DataDir, "config.json");
    public static string StatePath => Path.Combine(DataDir, "state.json");
    public static string CredPath => Path.Combine(DataDir, "cred.bin");
    public static string LogPath => Path.Combine(DataDir, "campusnet.log");
    public static string DaemonPidPath => Path.Combine(DataDir, "daemon.pid");

    /// <summary>把 "0.5.4" / "1.0.0-preview.1" 归一成可比较的数字数组；无法解析返回 null。</summary>
    public static int[]? VerKey(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        var core = text.Trim();
        var cut = core.IndexOfAny(new[] { '-', '+' });
        if (cut >= 0) core = core[..cut];
        var parts = core.Split('.');
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