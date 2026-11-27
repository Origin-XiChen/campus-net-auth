using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>就地升级：PE 版本静态读取（不执行候选文件）+ 换文件 + 重启守护。</summary>
public static class Upgrade
{
    public static (string? Version, string? Product)? PeVersion(string path)
    {
        if (!File.Exists(path)) return null;
        try
        {
            var size = VersionApi.GetFileVersionInfoSizeW(path, out _);
            if (size == 0) return null;
            var buffer = new byte[size];
            if (!VersionApi.GetFileVersionInfoW(path, 0, size, buffer)) return null;

            string? ver = null;
            if (VersionApi.VerQueryValueW(buffer, "\\", out var fixedPtr, out var fixedLen)
                && fixedPtr != IntPtr.Zero && fixedLen >= 52)
            {
                var ms = (uint)System.Runtime.InteropServices.Marshal.ReadInt32(fixedPtr, 8);
                var ls = (uint)System.Runtime.InteropServices.Marshal.ReadInt32(fixedPtr, 12);
                ver = $"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}";
            }

            string? product = null;
            if (VersionApi.VerQueryValueW(buffer, "\\VarFileInfo\\Translation",
                    out var transPtr, out var transLen) && transPtr != IntPtr.Zero && transLen >= 4)
            {
                var lang = (ushort)System.Runtime.InteropServices.Marshal.ReadInt16(transPtr, 0);
                var cp = (ushort)System.Runtime.InteropServices.Marshal.ReadInt16(transPtr, 2);
                var sub = $"\\StringFileInfo\\{lang:x4}{cp:x4}\\ProductName";
                if (VersionApi.VerQueryValueW(buffer, sub, out var prodPtr, out var prodLen)
                    && prodPtr != IntPtr.Zero)
                {
                    // VerQueryValue 的字符串长度单位是字符（含结尾 NUL），不要再除以 2
                    product = System.Runtime.InteropServices.Marshal
                        .PtrToStringUni(prodPtr, (int)prodLen).TrimEnd('\0').Trim();
                }
            }
            return (ver, product);
        }
        catch (Exception e)
        {
            Log.Debug("读取版本资源失败（{0}）: {1}", Path.GetFileName(path), e.Message);
            return null;
        }
    }

    public static string BackupPath =>
        AppInfo.ExePath + AppInfo.UpgradeBackupSuffix;

    /// <summary>
    /// 是否运行在安装器（Velopack）布局里：&lt;root&gt;\current\CampusNetAuth.exe，
    /// 旁边有 Update.exe。安装版由安装器/更新器管理文件，不能用裸 exe 换位升级。
    /// </summary>
    public static bool IsInstalledLayout() => AppInfo.IsInstalled;

    public static (string? Path, string? Version) FindCandidate()
    {
        var current = AppInfo.Version;
        var self = Path.GetFullPath(AppInfo.ExePath);
        string? bestPath = null;
        string? bestVer = null;
        var bestKey = Array.Empty<int>();
        try
        {
            foreach (var file in Directory.EnumerateFiles(AppInfo.BaseDir, "CampusNetAuth*.exe"))
            {
                var name = Path.GetFileName(file);
                if (!name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) continue;
                if (name.EndsWith(AppInfo.UpgradeBackupSuffix, StringComparison.OrdinalIgnoreCase))
                    continue;
                if (name.Equals("CampusNetAuthUninstall.exe", StringComparison.OrdinalIgnoreCase)
                    || name.Equals("CampusNetAuthDaemon.exe", StringComparison.OrdinalIgnoreCase))
                    continue;
                if (string.Equals(Path.GetFullPath(file), self, StringComparison.OrdinalIgnoreCase))
                    continue;
                var info = PeVersion(file);
                if (info?.Version == null) continue;
                if (info.Value.Product != null && info.Value.Product != AppInfo.AppName) continue;
                var key = AppInfo.VerKey(info.Value.Version);
                if (key == null || key.Length < 3) continue;
                var cur = AppInfo.VerKey(current)!;
                if (AppInfo.CompareVer(key, cur) <= 0) continue;
                if (AppInfo.CompareVer(key, bestKey) > 0)
                {
                    bestPath = file;
                    bestVer = info.Value.Version;
                    bestKey = key;
                }
            }
        }
        catch (Exception e)
        {
            Log.Warn("扫描升级候选失败: {0}", e.Message);
        }
        return (bestPath, bestVer);
    }

    public static void CleanupBackup()
    {
        try
        {
            if (File.Exists(BackupPath))
            {
                File.Delete(BackupPath);
                Log.Info("已清理上次升级的旧版本备份：{0}", Path.GetFileName(BackupPath));
            }
        }
        catch
        {
            Log.Debug("暂无法清理 {0}（可能仍被占用）", Path.GetFileName(BackupPath));
        }
    }

    public static (bool Ok, string Message) Apply(string? newPath = null)
    {
        if (IsInstalledLayout())
        {
            return (false, "当前是安装版（由安装器管理），请通过安装器升级；" +
                           "内置更新将在后续里程碑接入 Velopack 更新器");
        }
        var target = AppInfo.ExePath;
        string? ver;
        if (string.IsNullOrEmpty(newPath))
        {
            var (candidate, candidateVer) = FindCandidate();
            if (string.IsNullOrEmpty(candidate))
                return (false, "程序目录里没有找到更高版本的程序文件");
            newPath = candidate;
            ver = candidateVer;
        }
        else
        {
            var info = PeVersion(newPath);
            if (info?.Version == null)
                return (false, "无法读取该文件的版本信息，可能不是有效的程序文件");
            if (info.Value.Product != null && info.Value.Product != AppInfo.AppName)
                return (false, $"该文件的 ProductName 是 {info.Value.Product}，不是 {AppInfo.AppName}");
            if (!AppInfo.IsNewer(info.Value.Version, AppInfo.Version))
                return (false, $"该文件版本 {info.Value.Version} 不高于当前 {AppInfo.Version}");
            ver = info.Value.Version;
        }
        if (!File.Exists(newPath))
            return (false, "升级文件不存在：" + newPath);

        var wasRunning = Daemon.IsRunning();
        var backup = BackupPath;
        Log.Info("开始就地升级：{0} → {1}（当前 {2}）", AppInfo.Version, ver, target);
        if (wasRunning && !Daemon.Stop())
            return (false, "无法停止后台守护，升级已中止（可手动停止后重试）");
        try
        {
            if (File.Exists(backup)) File.Delete(backup);
        }
        catch { }

        try
        {
            File.Move(target, backup);
            File.Move(newPath, target);
        }
        catch (Exception e)
        {
            try
            {
                if (!File.Exists(target) && File.Exists(backup))
                    File.Move(backup, target);
            }
            catch { }
            Log.Error("就地升级失败: {0}", e.Message);
            return (false, $"替换文件失败：{e.Message}（可能仍被占用）");
        }

        Log.Info("升级文件已就位，旧版本备份为 {0}", Path.GetFileName(backup));
        if (wasRunning && !Daemon.Start())
            return (true, $"程序已升级到 {ver}，但后台守护未能自动启动，请在界面里手动启动");
        EventLog.Record("upgrade", $"程序已升级 {AppInfo.Version} → {ver}");
        return (true, $"已升级到 {ver}。请关闭并重新打开管理界面，新版本才会完全生效");
    }

    private static class VersionApi
    {
        [System.Runtime.InteropServices.DllImport("version.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode,
            SetLastError = true)]
        public static extern uint GetFileVersionInfoSizeW(string lptstrFilename, out uint lpdwHandle);

        [System.Runtime.InteropServices.DllImport("version.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode,
            SetLastError = true)]
        public static extern bool GetFileVersionInfoW(string lptstrFilename, uint dwHandle,
            uint dwLen, byte[] lpData);

        [System.Runtime.InteropServices.DllImport("version.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode,
            SetLastError = true)]
        public static extern bool VerQueryValueW(byte[] pBlock, string lpSubBlock,
            out IntPtr lplpBuffer, out uint puLen);
    }
}