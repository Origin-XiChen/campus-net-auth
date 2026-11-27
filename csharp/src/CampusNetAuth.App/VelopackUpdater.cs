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
        if (!force && cached != null && Fresh(cached)) return (JsonObject)cached.DeepClone();

        var (ok, message, update) = Check();
        var latest = update?.TargetFullRelease.Version.ToString() ?? "";
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
            ["asset_size"] = 0,
            ["asset_sha256"] = "",
        };
        SaveCached(info);
        return (JsonObject)info.DeepClone();
    }

    /// <summary>状态轮询用：返回缓存；缓存过期时在后台补查一次，绝不阻塞接口。</summary>
    public static JsonObject GetInfoOrStartCheck()
    {
        var cached = LoadCached();
        if (cached != null && Fresh(cached)) return (JsonObject)cached.DeepClone();
        if (Interlocked.CompareExchange(ref _checking, 1, 0) == 0)
        {
            Task.Run(() =>
            {
                try { CheckInfo(force: true); }
                finally { Interlocked.Exchange(ref _checking, 0); }
            });
        }
        return cached != null
            ? (JsonObject)cached.DeepClone()
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
        try
        {
            var mgr = Manager();
            var update = mgr.CheckForUpdatesAsync().GetAwaiter().GetResult();
            if (update == null) return "已是最新版本";
            mgr.DownloadUpdates(update);
            Log.Info("更新包已下载，准备重启应用：v{0}", update.TargetFullRelease.Version);
            mgr.ApplyUpdatesAndRestart(update.TargetFullRelease);
            return "更新已下载，正在重启应用";
        }
        catch (Exception e)
        {
            Log.Error("Velopack 更新失败: {0}", e.Message);
            return "更新失败：" + e.Message;
        }
    }

    public static string? LatestVersion()
    {
        var (ok, _, update) = Check();
        return ok && update != null ? update.TargetFullRelease.Version.ToString() : null;
    }
}