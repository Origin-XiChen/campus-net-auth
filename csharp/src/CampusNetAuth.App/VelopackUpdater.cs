using System.Diagnostics;
using System.Text.Json.Nodes;
using Velopack;
using Velopack.Sources;

namespace CampusNetAuth.App;

/// <summary>
/// 安装版（Velopack 布局）的内置更新：从 GitHub Release 读取 Velopack 更新清单。
/// 发布 Release 时必须同时上传 `releases.win.json` 与 `*.nupkg`（vpk pack 产物）。
/// </summary>
internal static class VelopackUpdater
{
    private const string StateKey = "velo_update_last";
    private const long SuccessTtl = 6 * 3600;
    private const long FailTtl = 300;
    private static int _checking;
    private static int _applying;

    private sealed class ProgressState
    {
        public int Percent;
        public long Total;
        public long Downloaded;
        public double Speed;
        public string Phase = "";
    }

    private static readonly object ProgressLock = new();
    private static readonly ProgressState Current = new();

    public static JsonObject ProgressJson()
    {
        lock (ProgressLock)
        {
            return new JsonObject
            {
                ["percent"] = Current.Percent,
                ["total"] = Current.Total,
                ["downloaded"] = Current.Downloaded,
                ["speed"] = Current.Speed,
                ["phase"] = Current.Phase,
            };
        }
    }

    private static void SetProgress(int percent, long total, long downloaded, double speed, string phase)
    {
        lock (ProgressLock)
        {
            Current.Percent = percent;
            Current.Total = total;
            Current.Downloaded = downloaded;
            Current.Speed = speed;
            Current.Phase = phase;
        }
    }

    /// <summary>升级后缓存里的 available 是按旧版本算的：按当前程序集版本重算。</summary>
    private static JsonObject Normalize(JsonObject info)
    {
        var result = (JsonObject)info.DeepClone();
        var latest = result.GetString("latest");
        var changed = !string.Equals(result.GetString("current"), AppInfo.Version,
            StringComparison.OrdinalIgnoreCase);
        result["current"] = AppInfo.Version;
        if (latest.Length == 0 && result.GetBool("ok"))
        {
            result["latest"] = AppInfo.Version;
            result["name"] = AppInfo.AppName + " " + AppInfo.Version;
        }
        if (changed)
            result["available"] = latest.Length > 0 && VersionNewer(latest, AppInfo.Version);
        return result;
    }

    private static bool VersionNewer(string latest, string current)
    {
        try
        {
            return SemanticVersion.Parse(latest).CompareTo(SemanticVersion.Parse(current)) > 0;
        }
        catch
        {
            return AppInfo.IsNewer(latest, current);
        }
    }

    private static UpdateManager Manager() =>
        new(new GithubSource($"https://github.com/{AppInfo.GitHubRepo}", null,
            // 预发布构建才收预发布 Release；正式版只看 latest
            AppInfo.Version.Contains('-')));

    public static (bool Ok, string Message, UpdateInfo? Update) Check()
    {
        if (!AppInfo.IsInstalled) return (false, "开发态不使用 Velopack 更新", null);
        try
        {
            var update = Manager().CheckForUpdatesAsync().GetAwaiter().GetResult();
            if (update == null) return (true, "已是最新版本", null);
            return (true, $"发现 v{update.TargetFullRelease.Version}", update);
        }
        catch (Exception e)
        {
            Log.Warn("Velopack 检查更新失败: {0}", e.Message);
            return (false, e.Message, null);
        }
    }

    /// <summary>
    /// 带缓存的检查结果（状态卡片与手动检查共用）。成功缓存 6 小时、失败 5 分钟。
    /// 返回字段与绿色版 UpdateCheck 对齐，UI 无需区分两种模式。
    /// </summary>
    public static JsonObject CheckInfo(bool force=false)
    {
        var cached = LoadCached();
        if (!force && cached != null && Fresh(cached)) return Normalize(cached);

        var (ok, message, update) = Check();
        var latest = update?.TargetFullRelease.Version.ToString()
                     ?? (ok ? AppInfo.Version : "");
        var info = new JsonObject
        {
            ["ok"] = ok,
            ["current"] = AppInfo.Version,
            ["latest"] = latest,
            ["available"] = update != null,
            ["url"] = AppInfo.GitHubReleasesUrl,
            ["name"] = latest.Length > 0 ? AppInfo.AppName + " " + latest : "",
            ["published_at"] = "",
            ["checked_at"] = Utils.Now(),
            ["error"] = ok ? "" : message,
            ["asset_url"] = "",
            ["asset_size"] = update?.TargetFullRelease.Size ?? 0,
            ["asset_sha256"] = "",
        };
        SaveCached(info);
        return Normalize(info);
    }

    /// <summary>状态轮询用：返回缓存；缓存过期时在后台补查一次，绝不阻塞接口。</summary>
    public static JsonObject GetInfoOrStartCheck()
    {
        var cached = LoadCached();
        if (cached != null && Fresh(cached)) return Normalize(cached);
        if (Interlocked.CompareExchange(ref _checking, 1, 0) == 0)
        {
            Task.Run(() =>
            {
                try { CheckInfo(force: true); }
                finally { Interlocked.Exchange(ref _checking, 0); }
            });
        }
        return cached != null
            ? Normalize(cached)
            : new JsonObject
            {
                ["ok"] = false,
                ["current"] = AppInfo.Version,
                ["latest"] = "",
                ["available"] = false,
                ["url"] = AppInfo.GitHubReleasesUrl,
                ["name"] = "",
                ["published_at"] = "",
                ["checked_at"] = 0,
                ["error"] = "",
                ["asset_url"] = "",
                ["asset_size"] = 0,
                ["asset_sha256"] = "",
            };
    }

    private static JsonObject? LoadCached()
    {
        try
        {
            return State.Load()[StateKey]?.DeepClone() as JsonObject;
        }
        catch
        {
            return null;
        }
    }

    private static bool Fresh(JsonObject info)
    {
        var checkedAt = info.GetInt64("checked_at");
        if (checkedAt <= 0) return false;
        var ttl = info.GetBool("ok") ? SuccessTtl : FailTtl;
        return Utils.Now() - checkedAt < ttl;
    }

    private static void SaveCached(JsonObject info)
    {
        try
        {
            State.Update(new JsonObject { [StateKey] = info.DeepClone() });
        }
        catch (Exception e)
        {
            Log.Debug("保存更新检查结果失败: {0}", e.Message);
        }
    }

    /// <summary>下载并应用更新（会重启应用；调用方应在后台任务里跑）。</summary>
    public static string DownloadAndApply()
    {
        if (!AppInfo.IsInstalled) return "开发态不支持内置更新";
        // UI 弹窗被误关后可能重复点「下载」，同一进程只允许一个 Velopack 操作
        if (Interlocked.CompareExchange(ref _applying, 1, 0) != 0)
            return "更新已在进行中，请稍候…";
        try
        {
            try
            {
                var mgr = Manager();
                var update = mgr.CheckForUpdatesAsync().GetAwaiter().GetResult();
                if (update == null) return "已是最新版本";
                var total = update.TargetFullRelease.Size;
                var clock = Stopwatch.StartNew();
                long lastBytes = 0;
                var lastMs = 0.0;
                var speed = 0.0;
                SetProgress(0, total, 0, 0, "下载中");
                mgr.DownloadUpdates(update, pct =>
                {
                    var ms = clock.Elapsed.TotalMilliseconds;
                    var bytes = total > 0 ? total * Math.Clamp(pct, 0, 100) / 100 : 0;
                    if (ms - lastMs >= 250)
                    {
                        var inst = (bytes - lastBytes) * 1000.0 / Math.Max(1, ms - lastMs);
                        speed = speed <= 0 ? inst : speed * 0.7 + inst * 0.3;
                        lastBytes = bytes;
                        lastMs = ms;
                        SetProgress(pct, total, bytes, speed, "下载中");
                    }
                });
                SetProgress(100, total, total, speed, "校验安装");
                Log.Info("更新包已下载，准备重启应用：v{0}", update.TargetFullRelease.Version);
                SetProgress(100, total, total, speed, "正在重启");
                mgr.ApplyUpdatesAndRestart(update.TargetFullRelease);
                return "更新已下载，正在重启应用";
            }
            catch (Exception e)
            {
                Log.Error("Velopack 更新失败: {0}", e.Message);
                SetProgress(0, 0, 0, 0, "失败");
                return "更新失败：" + e.Message;
            }
        }
        finally
        {
            Interlocked.Exchange(ref _applying, 0);
        }
    }

    public static string? LatestVersion()
    {
        var (ok, _, update) = Check();
        return ok && update != null ? update.TargetFullRelease.Version.ToString() : null;
    }
}