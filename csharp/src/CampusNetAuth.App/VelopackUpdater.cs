using Velopack;
using Velopack.Sources;

namespace CampusNetAuth.App;

/// <summary>
/// 安装版（Velopack 布局）的内置更新：从 GitHub Release 读取 Velopack 更新清单。
/// 发布 Release 时必须同时上传 `releases.win.json` 与 `*.nupkg`（vpk pack 产物）。
/// </summary>
internal static class VelopackUpdater
{
    private static UpdateManager Manager() =>
        new(new GithubSource($"https://github.com/{AppInfo.GitHubRepo}", null, false));

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