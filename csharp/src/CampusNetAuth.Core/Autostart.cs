using Microsoft.Win32;

namespace CampusNetAuth;

/// <summary>HKCU Run 键自启（当前用户级，无需管理员）。</summary>
public static class Autostart
{
    public static string Command() => $"\"{AppInfo.ExePath}\" daemon";

    public static bool Install()
    {
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(AppInfo.RunKeyPath, writable: true);
            key.SetValue(AppInfo.TaskName, Command(), RegistryValueKind.String);
            CleanLegacyTask();
            Log.Info("开机自启已安装（注册表 Run 键）");
            Log.Info("  启动命令: {0}", Command());
            return true;
        }
        catch (Exception e)
        {
            Log.Error("安装开机自启失败: {0}", e.Message);
            return false;
        }
    }

    public static bool Uninstall()
    {
        var ok = true;
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(AppInfo.RunKeyPath, writable: true);
            key?.DeleteValue(AppInfo.TaskName, throwOnMissingValue: false);
        }
        catch (Exception e)
        {
            ok = false;
            Log.Error("移除开机自启失败: {0}", e.Message);
        }
        CleanLegacyTask();
        if (ok) Log.Info("开机自启已移除");
        return ok;
    }

    /// <summary>
    /// 卸载钩子用：只移除"指向本次安装根目录"的 Run 值。
    /// 机器上可能还有另一份绿色部署在用同一个 Run 名，不能连带删掉。
    /// </summary>
    public static bool UninstallIfOwned()
    {
        try
        {
            var reference = ReferencedPath();
            if (string.IsNullOrEmpty(reference) || !File.Exists(reference)) return false;
            var installRoot = Path.GetDirectoryName(
                Path.TrimEndingDirectorySeparator(AppInfo.BaseDir));
            if (string.IsNullOrEmpty(installRoot)) return false;
            var full = Path.GetFullPath(reference);
            var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(installRoot))
                       + Path.DirectorySeparatorChar;
            if (!full.StartsWith(root, StringComparison.OrdinalIgnoreCase)) return false;
            return Uninstall();
        }
        catch
        {
            return false;
        }
    }

    public static string? ReadValue()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(AppInfo.RunKeyPath);
            return key?.GetValue(AppInfo.TaskName) as string;
        }
        catch
        {
            return null;
        }
    }

    public static bool Status() => !string.IsNullOrEmpty(ReadValue());

    /// <summary>
    /// 自启命令自愈（与 Python 版同语义）：Run 键指向的文件已不存在，或指向
    /// 当前目录的旧格式命令（VBS/旧参数）时，改写成 "&lt;当前 exe&gt; daemon"。
    /// 指向"另一份仍然存在的安装"时不动手——测试副本不得劫持真实安装；
    /// 但安装版是唯一正规入口，迁移期旧绿色副本的 Run 值会被接管。
    /// </summary>
    public static bool Repair()
    {
        try
        {
            var cur = ReadValue();
            if (string.IsNullOrWhiteSpace(cur)) return false; // 未启用自启
            var want = Command();
            if (string.Equals(cur.Trim(), want, StringComparison.OrdinalIgnoreCase))
                return false;

            var reference = ExtractReference(cur);
            var exists = reference.Length > 0 && File.Exists(reference);
            var sameDir = reference.Length > 0 && string.Equals(
                Path.GetDirectoryName(reference), AppInfo.BaseDir,
                StringComparison.OrdinalIgnoreCase);
            if (exists && !sameDir && !AppInfo.IsInstalled) return false;

            using var key = Registry.CurrentUser.CreateSubKey(AppInfo.RunKeyPath, writable: true);
            key.SetValue(AppInfo.TaskName, want, RegistryValueKind.String);
            Log.Info("开机自启命令已自动修正: {0} -> {1}", cur.Trim(), want);
            return true;
        }
        catch (Exception e)
        {
            Log.Debug("自启命令自愈跳过: {0}", e.Message);
            return false;
        }
    }

    private static string ExtractReference(string value)
    {
        var trimmed = value.Trim();
        var m = System.Text.RegularExpressions.Regex.Match(trimmed, "\"([^\"]+)\"");
        var reference = m.Success ? m.Groups[1].Value : trimmed.Split(' ')[0];
        return reference.Trim();
    }

    /// <summary>Run 值里引用的可执行文件路径（未做存在性判断）。</summary>
    internal static string? ReferencedPath()
    {
        var value = ReadValue();
        if (string.IsNullOrWhiteSpace(value)) return null;
        var reference = ExtractReference(value);
        return reference.Length > 0 ? reference : null;
    }

    /// <summary>自启链路健康检查：Run 键指向的 exe 是否存在。</summary>
    public static (bool Ok, string Reason) Health()
    {
        var value = ReadValue();
        if (string.IsNullOrEmpty(value)) return (true, "");
        var reference = ExtractReference(value);
        if (string.IsNullOrEmpty(reference)) return (true, "");
        if (!reference.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) return (true, "");
        if (!File.Exists(reference)) return (false, "开机自启指向的程序已丢失：" + reference);
        return (true, "");
    }

    /// <summary>清理历史版本用 schtasks 创建的计划任务（尽力而为）。</summary>
    private static void CleanLegacyTask()
    {
        try
        {
            var psi = new System.Diagnostics.ProcessStartInfo("schtasks",
                $"/Delete /TN {AppInfo.TaskName} /F")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using var p = System.Diagnostics.Process.Start(psi);
            p?.WaitForExit(8000);
        }
        catch
        {
            // 历史任务清理失败不影响主流程
        }
    }
}