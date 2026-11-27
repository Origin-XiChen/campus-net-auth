using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>
/// 界面启动时确保后台守护在跑（关闭界面不会停守护）。
/// 只接管"同一份程序"的旧守护；端口被另一份部署占用时不动手，避免测试副本
/// 或并行安装互相劫持。本次开机内用户手动停止过守护则尊重该选择。
/// </summary>
public static class DaemonGuard
{
    private const string ManualStopKey = "daemon_manual_stop_tick";

    public static void MarkManualStop()
    {
        try
        {
            State.Update(new JsonObject { [ManualStopKey] = Environment.TickCount64 });
        }
        catch
        {
        }
    }

    /// <summary>标记按"开机时长"判断归属：重启后 TickCount64 变小，自动失效。</summary>
    public static bool ManuallyStoppedThisBoot()
    {
        try
        {
            var st = State.Load();
            var tick = st.GetInt64(ManualStopKey, -1);
            if (tick < 0) return false;
            if (Environment.TickCount64 < tick)
            {
                // 上次标记来自上一次开机：清掉
                ClearManualStop();
                return false;
            }
            return true;
        }
        catch
        {
            return false;
        }
    }

    public static void ClearManualStop()
    {
        try
        {
            if (State.Load()[ManualStopKey] != null)
                State.Mutate(s => s.Remove(ManualStopKey));
        }
        catch
        {
        }
    }

    /// <summary>
    /// 界面启动后调用（后台线程）：
    /// * 守护在跑且是同一 exe 的旧版本 → 重启使新版本生效；
    /// * 守护未运行且不是"本次开机手动停止" → 自启开启或已配置账号时自动拉起；
    /// * 端口被别的部署占用 → 不劫持。
    /// </summary>
    public static (bool Ok, string Detail) EnsureAfterUiStart()
    {
        try
        {
            var info = Daemon.Info();
            if (info.Ok)
            {
                var sameExe = !string.IsNullOrEmpty(info.ExePath)
                    && info.ExePath.Equals(AppInfo.ExePath, StringComparison.OrdinalIgnoreCase);
                if (sameExe && info.Version != AppInfo.Version)
                {
                    Log.Info("守护版本 {0} ≠ {1}，自动重启使新版本生效", info.Version, AppInfo.Version);
                    Daemon.Stop();
                    var restarted = Daemon.Start();
                    if (restarted)
                    {
                        EventLog.Record("daemon_refresh",
                            $"后台守护已自动重启（{info.Version ?? "旧版"} → {AppInfo.Version}）");
                    }
                    return (restarted, restarted ? "已自动重启旧守护" : "重启守护失败");
                }
                return (true, "守护已在运行");
            }
            if (Daemon.IsRunning()) return (true, "守护已在运行");

            if (ManuallyStoppedThisBoot())
                return (true, "本次开机内已手动停止守护，跳过自动启动");

            var cfg = Config.Load();
            var configured = CredentialStore.Exists && cfg.GetString("username").Length > 0;
            if (!Autostart.Status() && !configured)
                return (true, "未启用自启且未配置账号，跳过自动启动");

            var started = Daemon.Start();
            if (started)
            {
                EventLog.Record("daemon_autostart", "界面启动时自动拉起守护");
                Log.Info("界面启动：守护未运行，已自动拉起");
            }
            return (started, started ? "已自动启动守护" : "守护启动失败");
        }
        catch (Exception e)
        {
            Log.Warn("界面启动守护自检失败: {0}", e.Message);
            return (false, e.Message);
        }
    }
}