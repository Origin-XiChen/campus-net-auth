using System.Text.Json.Nodes;
using CampusNetAuth;
using CampusNetAuth.App;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        // Velopack：处理安装/更新/卸载生命周期钩子（必须在任何 UI 之前调用）
        try { Velopack.VelopackApp.Build().Run(); } catch { }

        var command = args.Length > 0 ? args[0].ToLowerInvariant() : "";
        if (command is "" or "ui") return RunUi(args);
        return Cli.Run(args);
    }

    private static int RunUi(string[] args)
    {
        using var mutex = new Mutex(true, "CampusNetAuth.UI", out var createdNew);
        if (!createdNew)
        {
            MessageBox.Show("界面已在运行。", AppInfo.AppName);
            return 0;
        }

        var firstDeploy = !File.Exists(AppInfo.StatePath);
        if (firstDeploy)
        {
            State.Update(new JsonObject
            {
                ["first_run_at"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
            });
        }

        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        using var server = new ApiServer(firstDeploy, ForeignFiles());
        var url = server.Start();
        // 与 Python 版一致：把本次运行的本地接口地址写进 _ui_trace.log，
        // 供冒烟/排障读取（不含任何凭据）
        try
        {
            File.WriteAllText(Path.Combine(AppInfo.BaseDir, "_ui_trace.log"),
                "cmd_ui entered argv=" + string.Join(' ', args) +
                " version=" + AppInfo.Version + Environment.NewLine +
                "url=" + url + Environment.NewLine);
        }
        catch { }

        Application.Run(new MainForm(server));
        return 0;
    }

    private static List<string> ForeignFiles()
    {
        var known = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "CampusNetAuth.exe", "CampusNetAuthUninstall.exe", "config.json",
            "cred.bin", "state.json", "campusnet.log", "campusnet.log.1",
            "daemon.pid", "autostart.log", "CampusNetAuthDaemon.vbs",
            "CampusNetAuthUI.vbs", "config.example.json", "README.md",
            "_ui_trace.log",
        };
        var result = new List<string>();
        try
        {
            foreach (var file in Directory.EnumerateFiles(AppInfo.BaseDir))
            {
                var name = Path.GetFileName(file);
                if (known.Contains(name)) continue;
                if (name.EndsWith(AppInfo.UpgradeBackupSuffix, StringComparison.OrdinalIgnoreCase))
                    continue;
                if (name.StartsWith("_CampusNetAuth_update", StringComparison.OrdinalIgnoreCase))
                    continue;
                result.Add(name);
                if (result.Count >= 10) break;
            }
        }
        catch { }
        return result;
    }
}