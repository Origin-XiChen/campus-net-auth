using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>
/// 绿色版 → 安装版迁移：首次启动先把旧版数据导入 %APPDATA%，再清除旧目录。
/// 删除只认可信来源（自启 Run 值指向的 CampusNetAuth.exe），整目录移入回收站；
/// 回收站不可用时退化为"只删已知程序文件"，绝不递归删除未知用户文件。
/// </summary>
public static class LegacyCleanup
{
    private const string PendingKey = "legacy_cleanup_pending";

    private static readonly string[] DataFiles =
    {
        "config.json", "cred.bin", "state.json", "campusnet.log",
    };

    private static readonly string[] LegacyMarkers =
    {
        "CampusNetAuthUninstall.exe", "CampusNetAuthDaemon.vbs", "CampusNetAuthUI.vbs",
        "state.json", "config.json", "campusnet.log", "daemon.pid",
    };

    private static readonly string[] ProgramFiles =
    {
        "CampusNetAuth.exe", "CampusNetAuthUninstall.exe",
        "CampusNetAuthDaemon.vbs", "CampusNetAuthUI.vbs",
        "config.json", "cred.bin", "state.json",
        "campusnet.log", "campusnet.log.1", "daemon.pid", "autostart.log",
        "_ui_trace.log", "config.example.json", "README.md",
    };

    /// <summary>
    /// 同步准备（必须在自启自愈之前调用）：检测旧目录并导入数据。
    /// 返回待清理目录；只有导入成功才会登记 pending，供后台清理与失败重试。
    /// </summary>
    public static string? Prepare()
    {
        if (!AppInfo.IsInstalled) return null;
        try
        {
            var pending = State.Load().GetString(PendingKey);
            if (pending.Length > 0)
            {
                if (!Directory.Exists(pending) || IsUnsafeDir(pending))
                {
                    ClearPending();
                    return null;
                }
                ImportData(pending);
                return pending;
            }

            var dir = DetectFromRunKey();
            if (dir == null) return null;
            var count = ImportData(dir);
            State.Update(new JsonObject { [PendingKey] = dir });
            Log.Info("检测到旧版目录：{0}（已导入 {1} 个数据文件）", dir, count);
            return dir;
        }
        catch (Exception e)
        {
            // 导入失败时绝不删旧目录（数据还在原地）
            Log.Warn("旧版迁移准备失败（跳过清理）: {0}", e.Message);
            return null;
        }
    }

    /// <summary>后台执行：停旧守护/旧界面 → 回收站删除旧目录 → 记录状态（失败下次重试）。</summary>
    public static (bool Ok, string Detail) Cleanup(string dir)
    {
        try
        {
            if (!Directory.Exists(dir))
            {
                ClearPending();
                return (true, "旧版目录已不存在");
            }
            if (IsUnsafeDir(dir))
            {
                State.Update(new JsonObject { ["legacy_cleanup_error"] = "路径安全检查未通过" });
                return (false, "路径安全检查未通过，已跳过清理");
            }

            var stopped = StopLegacyDaemon(dir);
            CloseLegacyApps(dir);

            var (ok, detail) = DeleteDir(dir);
            if (!ok)
            {
                State.Update(new JsonObject { ["legacy_cleanup_error"] = detail });
                Log.Warn("旧版目录清理失败（下次启动重试）: {0}", detail);
                return (false, detail);
            }

            ClearPending();
            State.Update(new JsonObject
            {
                ["legacy_cleaned_at"] = Utils.Now(),
                ["legacy_cleaned_dir"] = dir,
            });

            // 停过旧守护就立刻拉起安装版守护，避免迁移期间断网
            if (stopped)
            {
                Thread.Sleep(400);
                Daemon.Start();
            }

            EventLog.Record("legacy_cleanup", "旧版目录：" + detail);
            Notify.System("校园网无感认证", "已导入旧版配置并清理旧目录（可在回收站恢复）", 6);
            Log.Info("旧版目录已清理：{0}（{1}）", dir, detail);
            return (true, detail);
        }
        catch (Exception e)
        {
            State.Update(new JsonObject { ["legacy_cleanup_error"] = e.Message });
            Log.Warn("旧版目录清理异常: {0}", e.Message);
            return (false, e.Message);
        }
    }

    private static void ClearPending()
    {
        try
        {
            if (State.Load()[PendingKey] != null)
                State.Mutate(s => s.Remove(PendingKey));
        }
        catch
        {
        }
    }

    private static string? DetectFromRunKey()
    {
        var reference = Autostart.ReferencedPath();
        if (string.IsNullOrEmpty(reference)) return null;
        string exe;
        try
        {
            exe = Path.GetFullPath(reference);
        }
        catch
        {
            return null;
        }
        if (!File.Exists(exe)) return null;
        if (!string.Equals(Path.GetFileName(exe), AppInfo.AppName + ".exe",
                StringComparison.OrdinalIgnoreCase))
            return null;

        var dir = Path.GetDirectoryName(exe);
        if (string.IsNullOrEmpty(dir) || IsUnsafeDir(dir)) return null;

        var info = Upgrade.PeVersion(exe);
        if (info?.Version == null) return null;
        if (!string.IsNullOrEmpty(info.Value.Product) && info.Value.Product != AppInfo.AppName)
            return null;
        if (!AppInfo.IsNewer(AppInfo.Version, info.Value.Version)) return null;
        if (!HasLegacyMarkers(dir)) return null;
        return dir;
    }

    private static bool HasLegacyMarkers(string dir) =>
        LegacyMarkers.Any(m => File.Exists(Path.Combine(dir, m)));

    /// <summary>复制旧数据（不覆盖已有文件）；任一复制失败都抛出，调用方据此放弃清理。</summary>
    private static int ImportData(string dir)
    {
        Directory.CreateDirectory(AppInfo.DataDir);
        var count = 0;
        foreach (var name in DataFiles)
        {
            var src = Path.Combine(dir, name);
            if (!File.Exists(src)) continue;
            var dst = Path.Combine(AppInfo.DataDir, name);
            if (File.Exists(dst)) continue;
            File.Copy(src, dst);
            count++;
        }
        return count;
    }

    /// <summary>VER 回执里的守护路径在旧目录内才停（避免误停其它安装）。</summary>
    private static bool StopLegacyDaemon(string dir)
    {
        var info = Daemon.Info();
        if (!info.Ok || string.IsNullOrEmpty(info.ExePath) || !IsUnder(info.ExePath, dir))
            return false;

        Log.Info("停止旧版守护 v{0}：{1}", info.Version, info.ExePath);
        Daemon.Stop();
        if (Daemon.IsRunning())
        {
            KillProcessesUnder(dir);
            var deadline = DateTime.UtcNow.AddSeconds(4);
            while (DateTime.UtcNow < deadline && Daemon.IsRunning()) Thread.Sleep(150);
        }
        return true;
    }

    /// <summary>旧界面/残留进程：先礼貌关窗，3 秒不退再按"exe 在旧目录内"精确终止。</summary>
    private static void CloseLegacyApps(string dir)
    {
        foreach (var name in new[] { AppInfo.AppName, "CampusNetAuthUninstall" })
        {
            foreach (var p in Process.GetProcessesByName(name))
            {
                try
                {
                    if (p.Id == Environment.ProcessId) continue;
                    var path = TryGetPath(p);
                    if (path == null || !IsUnder(path, dir)) continue;
                    if (p.MainWindowHandle == IntPtr.Zero)
                    {
                        p.Kill();
                        p.WaitForExit(2000);
                        continue;
                    }
                    p.CloseMainWindow();
                    if (!p.WaitForExit(3000))
                    {
                        p.Kill();
                        p.WaitForExit(2000);
                    }
                }
                catch
                {
                    // 进程已退出/无权限：继续处理其它进程
                }
                finally
                {
                    p.Dispose();
                }
            }
        }
    }

    private static void KillProcessesUnder(string dir)
    {
        foreach (var p in Process.GetProcessesByName(AppInfo.AppName))
        {
            try
            {
                if (p.Id == Environment.ProcessId) continue;
                var path = TryGetPath(p);
                if (path == null || !IsUnder(path, dir)) continue;
                p.Kill();
                p.WaitForExit(2000);
            }
            catch
            {
            }
            finally
            {
                p.Dispose();
            }
        }
    }

    private static (bool Ok, string Detail) DeleteDir(string dir)
    {
        for (var attempt = 0; attempt < 3; attempt++)
        {
            if (!Directory.Exists(dir)) return (true, "已移入回收站");
            if (TryRecycle(dir)) return (true, "已移入回收站");
            Thread.Sleep(600);
        }
        return DeleteKnownFiles(dir);
    }

    /// <summary>整目录送回收站（可恢复）；失败返回 false，由调用方退化到白名单删除。</summary>
    private static bool TryRecycle(string dir)
    {
        try
        {
            var op = new ShFileOp
            {
                wFunc = FoDelete,
                // 双 NUL 结尾：这里补一个，封送器再加一个
                pFrom = Path.TrimEndingDirectorySeparator(dir) + "\0",
                fFlags = FofSilent | FofNoConfirmation | FofNoConfirmMkDir |
                         FofAllowUndo | FofNoErrorUi,
            };
            var rc = SHFileOperationW(ref op);
            return rc == 0 && op.fAnyOperationsAborted == 0 && !Directory.Exists(dir);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>回收站不可用时的兜底：只删旧版自己的已知文件/目录，未知文件保留。</summary>
    private static (bool Ok, string Detail) DeleteKnownFiles(string dir)
    {
        var locked = new List<string>();
        foreach (var name in ProgramFiles)
        {
            if (!TryDelete(Path.Combine(dir, name))) locked.Add(name);
        }
        try
        {
            foreach (var f in Directory.EnumerateFiles(dir, "CampusNetAuth*.old.exe"))
            {
                if (!TryDelete(f)) locked.Add(Path.GetFileName(f));
            }
        }
        catch
        {
        }
        TryDeleteTree(Path.Combine(dir, "webview2"));
        TryDeleteTree(Path.Combine(dir, "probe"));

        if (locked.Count > 0)
            return (false, "部分文件被占用，稍后重试：" + locked[0]);

        try
        {
            if (!Directory.Exists(dir)) return (true, "已删除旧版程序文件");
            if (!Directory.EnumerateFileSystemEntries(dir).Any())
            {
                Directory.Delete(dir);
                return (true, "已删除旧版程序文件");
            }
            return (true, "已删除旧版程序文件，目录保留（含其它文件）");
        }
        catch (Exception e)
        {
            return (false, e.Message);
        }
    }

    private static bool TryDelete(string path)
    {
        try
        {
            if (!File.Exists(path)) return true;
            File.Delete(path);
            return !File.Exists(path);
        }
        catch
        {
            return false;
        }
    }

    private static bool TryDeleteTree(string path)
    {
        try
        {
            if (Directory.Exists(path)) Directory.Delete(path, true);
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static string? TryGetPath(Process p)
    {
        try { return p.MainModule?.FileName; }
        catch { return null; }
    }

    /// <summary>path 是否位于 dir 之下（按路径边界判断，避免 CampusNetAuth2 误判）。</summary>
    internal static bool IsUnder(string path, string dir)
    {
        try
        {
            var full = Path.GetFullPath(path);
            var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(dir))
                       + Path.DirectorySeparatorChar;
            return full.StartsWith(root, StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return false;
        }
    }

    /// <summary>删除安全阀：根目录/网络盘/当前安装或数据目录（及其祖先/子目录）一律不删。</summary>
    internal static bool IsUnsafeDir(string dir)
    {
        if (string.IsNullOrWhiteSpace(dir)) return true;
        string full;
        try
        {
            full = Path.TrimEndingDirectorySeparator(Path.GetFullPath(dir));
        }
        catch
        {
            return true;
        }
        if (full.StartsWith(@"\\", StringComparison.Ordinal)) return true;
        var root = Path.GetPathRoot(full);
        if (root != null && string.Equals(full,
                Path.TrimEndingDirectorySeparator(root), StringComparison.OrdinalIgnoreCase))
            return true;

        var installRoot = Path.GetDirectoryName(
            Path.TrimEndingDirectorySeparator(AppInfo.BaseDir));
        foreach (var protect in new[] { AppInfo.BaseDir, AppInfo.DataDir, installRoot })
        {
            if (string.IsNullOrEmpty(protect)) continue;
            if (IsSameOrAncestor(full, protect) || IsSameOrAncestor(protect, full))
                return true;
        }
        return false;
    }

    private static bool IsSameOrAncestor(string dir, string path)
    {
        try
        {
            var a = Path.TrimEndingDirectorySeparator(Path.GetFullPath(dir));
            var b = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
            return string.Equals(a, b, StringComparison.OrdinalIgnoreCase)
                   || b.StartsWith(a + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            return true;
        }
    }

    private const uint FoDelete = 0x0003;
    private const ushort FofSilent = 0x0004;
    private const ushort FofNoConfirmation = 0x0010;
    private const ushort FofAllowUndo = 0x0040;
    private const ushort FofNoConfirmMkDir = 0x0200;
    private const ushort FofNoErrorUi = 0x0400;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ShFileOp
    {
        public IntPtr hwnd;
        public uint wFunc;
        [MarshalAs(UnmanagedType.LPWStr)] public string pFrom;
        [MarshalAs(UnmanagedType.LPWStr)] public string? pTo;
        public ushort fFlags;
        public int fAnyOperationsAborted;
        public IntPtr hNameMappings;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpszProgressTitle;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SHFileOperationW(ref ShFileOp op);
}