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

    /// <summary>自启链路健康检查：Run 键指向的 exe 是否存在。</summary>
    public static (bool Ok, string Reason) Health()
    {
        var value = ReadValue();
        if (string.IsNullOrEmpty(value)) return (true, "");
        var trimmed = value.Trim();
        var m = System.Text.RegularExpressions.Regex.Match(trimmed, "\"([^\"]+)\"");
        var reference = m.Success ? m.Groups[1].Value : trimmed.Split(' ')[0];
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