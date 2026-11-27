using System.Diagnostics;
using System.Reflection;
using Microsoft.Win32;

namespace CampusNetAuth.Setup;

internal sealed class Options
{
    public string? Dir;
    public bool Silent;
    public bool NoLaunch;

    public static Options Parse(string[] args)
    {
        var o = new Options();
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i].ToLowerInvariant())
            {
                case "--silent":
                    o.Silent = true;
                    break;
                case "--no-launch":
                    o.NoLaunch = true;
                    break;
                case "--dir" when i + 1 < args.Length:
                    o.Dir = args[++i];
                    break;
            }
        }
        return o;
    }
}

internal static class Installer
{
    private const string PayloadResource = "CampusNetAuth.Setup.SetupPayload.exe";

    public static string Version
    {
        get
        {
            var info = typeof(Installer).Assembly
                .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "";
            var plus = info.IndexOf('+');
            return plus > 0 ? info[..plus] : info;
        }
    }

    public static string DefaultDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "CampusNetAuth");

    /// <summary>已安装位置（读卸载注册项）；没有安装返回 null。</summary>
    public static string? ExistingDir()
    {
        try
        {
            const string root = @"Software\Microsoft\Windows\CurrentVersion\Uninstall";
            using var all = Registry.CurrentUser.OpenSubKey(root);
            if (all == null) return null;
            foreach (var name in all.GetSubKeyNames())
            {
                using var k = all.OpenSubKey(name);
                if (k?.GetValue("DisplayName") as string != "CampusNetAuth") continue;
                if (k.GetValue("InstallLocation") is string dir && dir.Length > 0)
                    return Path.TrimEndingDirectorySeparator(dir);
                break;
            }
        }
        catch
        {
        }
        return null;
    }

    public static string? ExistingVersion()
    {
        try
        {
            const string root = @"Software\Microsoft\Windows\CurrentVersion\Uninstall";
            using var all = Registry.CurrentUser.OpenSubKey(root);
            if (all == null) return null;
            foreach (var name in all.GetSubKeyNames())
            {
                using var k = all.OpenSubKey(name);
                if (k?.GetValue("DisplayName") as string != "CampusNetAuth") continue;
                return k.GetValue("DisplayVersion") as string;
            }
        }
        catch
        {
        }
        return null;
    }

    /// <summary>校验安装目录：绝对路径、非盘根、可写。返回规范化路径或错误原因。</summary>
    public static (string? Dir, string? Error) ValidateDir(string dir)
    {
        try
        {
            var full = Path.TrimEndingDirectorySeparator(Path.GetFullPath(dir.Trim()));
            if (string.IsNullOrEmpty(full)) return (null, "安装位置不能为空");
            var root = Path.GetPathRoot(full);
            if (root != null && string.Equals(full,
                    Path.TrimEndingDirectorySeparator(root), StringComparison.OrdinalIgnoreCase))
                return (null, "不能安装到磁盘根目录");
            Directory.CreateDirectory(full);
            var probe = Path.Combine(full, "_write_probe.tmp");
            File.WriteAllText(probe, "x");
            File.Delete(probe);
            return (full, null);
        }
        catch (Exception e)
        {
            return (null, "安装位置不可写：" + e.Message);
        }
    }

    /// <summary>
    /// 解出内嵌 Setup 并静默安装到 dir；launch=true 时安装成功后启动应用。
    /// 返回 (成功, 详情)。
    /// </summary>
    public static (bool Ok, string Detail) Install(string dir, bool launch)
    {
        var (full, error) = ValidateDir(dir);
        if (full == null) return (false, error ?? "安装位置无效");

        string setup;
        try
        {
            setup = ExtractPayload();
        }
        catch (Exception e)
        {
            return (false, e.Message);
        }

        try
        {
            var psi = new ProcessStartInfo(setup)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(setup)!,
            };
            psi.ArgumentList.Add("--silent");
            psi.ArgumentList.Add("--installto");
            psi.ArgumentList.Add(full);
            using var p = Process.Start(psi);
            if (p == null) return (false, "无法启动安装器");
            if (!p.WaitForExit(600_000))
            {
                try { p.Kill(); } catch { }
                return (false, "安装超时（10 分钟）");
            }
            if (p.ExitCode != 0)
                return (false, $"安装器退出码 {p.ExitCode}（日志：%LOCALAPPDATA%\\velopack\\velopack.log）");
        }
        catch (Exception e)
        {
            return (false, "启动安装器失败：" + e.Message);
        }

        try { File.Delete(setup); } catch { }

        if (!launch) return (true, "安装完成");

        try
        {
            var exe = Path.Combine(full, "current", "CampusNetAuth.exe");
            Process.Start(new ProcessStartInfo(exe)
            {
                UseShellExecute = true,
                WorkingDirectory = Path.GetDirectoryName(exe)!,
            });
            return (true, "安装完成，已启动");
        }
        catch (Exception e)
        {
            return (true, "安装完成（启动应用失败：" + e.Message + "）");
        }
    }

    private static string ExtractPayload()
    {
        var asm = typeof(Installer).Assembly;
        using var stream = asm.GetManifestResourceStream(PayloadResource)
                           ?? throw new InvalidOperationException(
                               "安装包缺失：这是开发构建，请使用正式安装器。");
        var baseDir = Environment.GetEnvironmentVariable("CNA_SETUP_TMP");
        if (string.IsNullOrWhiteSpace(baseDir)) baseDir = Path.GetTempPath();
        var dir = Path.Combine(Path.GetFullPath(baseDir), "CampusNetAuthSetup", Version);
        Directory.CreateDirectory(dir);
        var path = Path.Combine(dir, "Setup.exe");
        using (var fs = File.Create(path))
        {
            stream.CopyTo(fs);
        }
        if (new FileInfo(path).Length < 1_000_000)
            throw new InvalidOperationException("内嵌安装包不完整，请重新下载安装器。");
        return path;
    }
}