using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;
using CampusNetAuth;

namespace CampusNetAuth.App;

internal static class ConsoleHost
{
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AttachConsole(int dwProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AllocConsole();

    [DllImport("kernel32.dll")]
    private static extern uint GetConsoleOutputCP();

    private const int AttachParentProcess = -1;

    public static bool Ensure()
    {
        var attached = AttachConsole(AttachParentProcess);
        // 被 cmd/脚本以管道或文件重定向时，标准句柄本身已可用（不要再 AllocConsole，
        // 否则会把它换成新控制台的句柄，父进程就收不到输出了）
        if (!attached && !Console.IsOutputRedirected) AllocConsole();
        try
        {
            Console.OutputEncoding = Encoding.GetEncoding((int)GetConsoleOutputCP());
            Console.InputEncoding = Encoding.GetEncoding((int)GetConsoleOutputCP());
        }
        catch
        {
            // 重定向/无控制台时设置编码会失败：保持默认（无 BOM 的 UTF-8）
        }
        return true;
    }
}

internal static class Cli
{
    public static int Run(string[] args)
    {
        var command = args.Length > 0 ? args[0].ToLowerInvariant() : "";

        if (command is "--version" or "-v" or "version")
        {
            ConsoleHost.Ensure();
            Console.WriteLine($"{AppInfo.AppName} {AppInfo.Version}");
            return 0;
        }

        if (command is "" or "ui")
        {
            // 里程碑 4 才接 WebView2 窗口；此前给出明确提示
            System.Windows.Forms.MessageBox.Show(
                $"界面尚在迁移中（里程碑 4）。\n\n当前可用命令：\n" +
                $"{AppInfo.AppName}.exe --version | status | login | logout | once | " +
                "proxy | update | upgrade | log | test",
                AppInfo.AppName, System.Windows.Forms.MessageBoxButtons.OK,
                System.Windows.Forms.MessageBoxIcon.Information);
            return 0;
        }

        ConsoleHost.Ensure();
        var cfg = Config.Load();
        Log.SetLevel(cfg.GetString("log_level", "INFO"));

        switch (command)
        {
            case "setup": return CmdSetup(cfg);
            case "login": return CmdLogin(cfg, args);
            case "logout": return CmdLogout(cfg);
            case "status": return CmdStatus(cfg);
            case "once": return CmdOnce(cfg);
            case "log": return CmdLog(args);
            case "daemon":
                Log.ToConsole = true;
                return Daemon.Run(cfg);
            case "start": return Daemon.Start() ? 0 : 1;
            case "stop": return Daemon.Stop() ? 0 : 1;
            case "proxy": return CmdProxy(cfg, args);
            case "update": return CmdUpdate(args);
            case "upgrade": return CmdUpgrade(args);
            case "install": return Autostart.Install() ? 0 : 1;
            case "uninstall": return Autostart.Uninstall() ? 0 : 1;
            case "test": return CmdTest(cfg);
            default:
                Console.WriteLine("用法: CampusNetAuth.exe <命令>");
                Console.WriteLine("  setup / login / logout / status / once / daemon / start / stop");
                Console.WriteLine("  proxy [--off|--restore] / update [--force] [--open] / upgrade [--check]");
                Console.WriteLine("  install / uninstall / log [-n N] / test / --version");
                return 1;
        }
    }

    private static int CmdSetup(JsonObject cfg)
    {
        Console.Write($"门户地址 [{cfg.GetString("portal_host")}]: ");
        var host = Console.ReadLine()?.Trim() ?? "";
        if (host.Length > 0)
        {
            if (host.Contains("://")) host = new Uri(host).Host;
            var parts = host.Split(':');
            cfg["portal_host"] = parts[0];
            if (parts.Length > 1 && int.TryParse(parts[1], out var p)) cfg["portal_port"] = p;
        }

        Console.Write($"账号 [{Utils.MaskUser(cfg.GetString("username"))}]: ");
        var user = Console.ReadLine()?.Trim() ?? "";
        if (user.Length > 0) cfg["username"] = user;

        Console.Write($"服务 default/DX/LT/YD/auto [{cfg.GetString("service")}]: ");
        var svc = Console.ReadLine()?.Trim() ?? "";
        if (svc.Length > 0) cfg["service"] = svc;

        Console.Write("密码（留空=不修改）: ");
        var password = ReadPassword();
        Console.WriteLine();

        Config.Save(Config.Sanitize(cfg));
        if (password.Length > 0 && !CredentialStore.Save(password))
        {
            Console.WriteLine("! 密码保存失败（DPAPI），请重试");
            return 1;
        }
        Console.WriteLine("配置已保存。" + (password.Length > 0 ? "凭据已加密保存。" : ""));
        return 0;
    }

    private static string ReadPassword()
    {
        var sb = new StringBuilder();
        while (true)
        {
            var key = Console.ReadKey(intercept: true);
            if (key.Key == ConsoleKey.Enter) break;
            if (key.Key == ConsoleKey.Backspace)
            {
                if (sb.Length > 0) sb.Length--;
                continue;
            }
            if (!char.IsControl(key.KeyChar)) sb.Append(key.KeyChar);
        }
        return sb.ToString();
    }

    private static int CmdLogin(JsonObject cfg, string[] args)
    {
        LogoutPause.Clear();
        var cred = CredentialStore.Load();
        if (string.IsNullOrEmpty(cfg.GetString("username")) || string.IsNullOrEmpty(cred))
        {
            Console.WriteLine("尚未配置账号密码，请先运行: CampusNetAuth.exe setup");
            return 1;
        }
        var client = new PortalClient(cfg);
        ProxyGuard.MaybeClear(cfg, "立即登录前清理系统代理", client);
        var force = args.Contains("--force", StringComparer.OrdinalIgnoreCase);
        if (!force)
        {
            var (online, _) = client.CheckOnline();
            if (online == true)
            {
                Console.WriteLine("当前已在线，无需登录（加 --force 可强制重登）");
                return 0;
            }
        }
        var res = client.Login(cfg.GetString("username"), cred);
        if (res.GetString("result") == "success")
        {
            Console.WriteLine("登录成功");
            if (res.GetString("userIndex").Length > 0)
            {
                State.Update(new JsonObject
                {
                    ["userIndex"] = res.GetString("userIndex"),
                    ["loginTime"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                }, "last_error");
            }
            return 0;
        }
        var msg = res.GetString("message");
        var hint = res.GetString("_hint");
        Console.WriteLine("登录失败：" + (msg.Length > 0 ? msg : hint));
        return 2;
    }

    private static int CmdLogout(JsonObject cfg)
    {
        var cred = CredentialStore.Load();
        var client = new PortalClient(cfg);
        var ui = State.Load().GetString("userIndex");
        var ok = false;
        if (ui.Length > 0)
        {
            var r = client.Logout(ui);
            ok = r.GetString("result").ToLowerInvariant() == "success";
        }
        if (!ok)
        {
            var info = client.Interface("getOnlineUserInfo");
            var sessionUi = info?.GetString("userIndex");
            if (!string.IsNullOrEmpty(sessionUi))
            {
                var r = client.Logout(sessionUi);
                ok = r.GetString("result").ToLowerInvariant() == "success";
            }
        }
        if (!ok && !string.IsNullOrEmpty(cred))
        {
            var r = client.Logout(null, forceByCred: true,
                username: cfg.GetString("username"), password: cred);
            ok = r.GetString("result").ToLowerInvariant() == "success";
        }
        if (!ok)
        {
            Console.WriteLine("下线失败：门户未确认（可能已离线），详见日志");
            return 2;
        }
        State.Mutate(s =>
        {
            s.Remove("userIndex");
            s.Remove("loginTime");
            s.Remove("last_error");
        });
        var pause = cfg.GetInt("logout_pause", 600);
        if (pause > 0)
        {
            LogoutPause.Set(pause);
            Console.WriteLine($"已下线，{Math.Max(1, pause / 60)} 分钟内不会自动重连（login 可提前恢复）");
        }
        else
        {
            Console.WriteLine("已下线（logout_pause=0，守护可能很快自动重连）");
        }
        return 0;
    }

    private static string NetworkStateText(bool? online, bool? portal)
    {
        if (online == true) return "已在线";
        if (online == false) return "未认证（被门户拦截）";
        if (portal == true) return "公网探测无结论（门户可达）";
        return "不在校园网（门户不可达）";
    }

    private static int CmdStatus(JsonObject cfg)
    {
        var client = new PortalClient(cfg);
        var (online, _) = client.CheckOnline();
        bool? portal = online == null ? client.PortalReachable() : null;

        Console.WriteLine("=".PadRight(58, '='));
        Console.WriteLine(" 校园网无感认证 —— 状态");
        Console.WriteLine("=".PadRight(58, '='));
        Console.WriteLine("版本     : {0}", AppInfo.Version);
        Console.WriteLine("门户地址 : {0}", client.Url(""));
        Console.WriteLine("账号     : {0}", cfg.GetString("username").Length > 0
            ? Utils.MaskUser(cfg.GetString("username")) : "(未配置)");
        Console.WriteLine("服务     : {0}", cfg.GetString("service"));
        Console.WriteLine("网络状态 : {0}", NetworkStateText(online, portal));
        Console.WriteLine("开机自启 : {0}", Autostart.Status() ? "已启用" : "未启用");
        if (Daemon.IsRunning())
        {
            var (ok, ver, _, detail) = Daemon.Info();
            Console.WriteLine("守护进程 : 运行中");
            Console.WriteLine("守护版本 : {0}", ok ? "v" + ver : "未知（" + detail + "）");
        }
        else
        {
            Console.WriteLine("守护进程 : 已停止");
        }
        Console.WriteLine("凭据文件 : {0}", CredentialStore.Exists ? "已存在" : "缺失");
        var info = UpdateCheck.Get();
        var when = info.GetInt64("checked_at") > 0
            ? "，检查于 " + Utils.FmtCheckedAt(info.GetInt64("checked_at")) : "";
        if (info.GetInt64("checked_at") <= 0)
            Console.WriteLine("远程更新 : 未检查（执行 update 检查）");
        else if (!info.GetBool("ok"))
            Console.WriteLine("远程更新 : 检查失败（{0}{1}）", info.GetString("error"), when);
        else if (info.GetBool("available"))
            Console.WriteLine("远程更新 : 发现 v{0}（{1}{2}）", info.GetString("latest"), info.GetString("url"), when);
        else
            Console.WriteLine("远程更新 : 已是最新（v{0}{1}）", info.GetString("latest"), when);

        var hints = cfg.GetString("login_page_hints") is { } ? cfg["login_page_hints"] as JsonObject : null;
        if (hints != null && hints.GetString("nasip").Length > 0 && hints.GetString("wlanacname").Length > 0)
            Console.WriteLine("登录页兜底: 已配置 (wlanacname={0} nasip={1})",
                hints.GetString("wlanacname"), hints.GetString("nasip"));
        else
            Console.WriteLine("登录页兜底: 未配置");

        var left = LogoutPause.Left();
        if (left > 0)
            Console.WriteLine("下线静默 : 剩余 {0}s（守护不会自动登回；login 可恢复）", left);
        return 0;
    }

    private static int CmdOnce(JsonObject cfg)
    {
        if (LogoutPause.Left() > 0)
        {
            Console.WriteLine("处于「手动下线」静默期，本次跳过；执行 login 可提前恢复");
            return 0;
        }
        var cred = CredentialStore.Load();
        if (string.IsNullOrEmpty(cfg.GetString("username")) || string.IsNullOrEmpty(cred))
        {
            Console.WriteLine("尚未配置账号密码");
            return 1;
        }
        var client = new PortalClient(cfg);
        ProxyGuard.MaybeClear(cfg, "单次检测前清理系统代理", client);
        var (online, loginPage) = client.CheckAuthentication();
        if (online == true)
        {
            Console.WriteLine("已在线，无需操作");
            return 0;
        }
        if (online == null)
        {
            Console.WriteLine("公网探测无结论，门户页面未满足认证条件，本次跳过");
            return 0;
        }
        var res = client.Login(cfg.GetString("username"), cred, loginPage: loginPage);
        if (res.GetString("result") == "success")
        {
            Console.WriteLine("登录成功");
            return 0;
        }
        Console.WriteLine("登录失败: " + Utils.SafeMessage(res));
        return 2;
    }

    private static int CmdLog(string[] args)
    {
        var n = 15;
        var idx = Array.FindIndex(args, a => a is "-n" or "--lines");
        if (idx >= 0 && idx + 1 < args.Length) int.TryParse(args[idx + 1], out n);
        try
        {
            var lines = File.ReadAllLines(AppInfo.LogPath, Encoding.UTF8);
            foreach (var line in lines.TakeLast(Math.Max(1, n))) Console.WriteLine(line);
            return 0;
        }
        catch (Exception e)
        {
            Console.WriteLine("（暂无日志）" + e.Message);
            return 0;
        }
    }

    private static int CmdProxy(JsonObject cfg, string[] args)
    {
        if (args.Contains("--restore", StringComparer.OrdinalIgnoreCase))
        {
            var (ok, msg) = ProxyGuard.Restore();
            Console.WriteLine((ok ? "恢复成功：" : "恢复失败：") + msg);
            return ok ? 0 : 1;
        }
        if (args.Contains("--off", StringComparer.OrdinalIgnoreCase))
        {
            var (ok, changed, before) = ProxyGuard.Disable("命令行指定");
            if (!before.Active)
            {
                Console.WriteLine("当前未启用系统代理，无需处理");
                return 0;
            }
            Console.WriteLine("关闭前：" + before.Detail);
            Console.WriteLine(ok ? "已关闭系统代理" : "关闭系统代理失败，详见日志");
            return ok ? 0 : 1;
        }

        var st = ProxyGuard.GetState();
        Console.WriteLine("=".PadRight(58, '='));
        Console.WriteLine(" 系统代理状态");
        Console.WriteLine("=".PadRight(58, '='));
        Console.WriteLine("当前状态  : {0}", st.Active ? "已开启" : "未开启");
        Console.WriteLine("详细      : {0}", st.Detail);
        Console.WriteLine("代理服务器: {0}", st.Server.Length > 0 ? st.Server : "（未设置）");
        Console.WriteLine("PAC 地址  : {0}", st.Pac.Length > 0 ? st.Pac : "（未设置）");
        Console.WriteLine("自动发现  : {0}", st.PacWpad ? "已启用（WPAD）" : "未启用");
        Console.WriteLine("守卫开关  : {0}（config.json 的 proxy_guard）", cfg.GetString("proxy_guard", "auto"));
        var bak = State.Load()["proxy_backup"] as JsonObject;
        Console.WriteLine(bak != null
            ? $"关闭前备份: {bak.GetString("t", "?")}（原因：{bak.GetString("reason", "?")}）"
            : "关闭前备份: 无（说明从未由本程序关闭过代理）");
        return 0;
    }

    private static int CmdUpdate(string[] args)
    {
        var force = args.Contains("--force", StringComparer.OrdinalIgnoreCase);
        var info = UpdateCheck.CheckLatest(force);
        Console.WriteLine("当前版本 : {0}", AppInfo.Version);
        if (!info.GetBool("ok"))
        {
            Console.WriteLine("检查失败 : {0}", info.GetString("error", "未知错误"));
            return 1;
        }
        Console.WriteLine("最新版本 : {0}", info.GetString("latest"));
        if (info.GetBool("available"))
        {
            Console.WriteLine("可用更新 : 是" +
                              (info.GetString("name").Length > 0 ? $"（{info.GetString("name")}）" : ""));
            Console.WriteLine("下载页面 : {0}", info.GetString("url"));
            if (args.Contains("--open", StringComparer.OrdinalIgnoreCase))
                UpdateCheck.OpenReleasePage();
        }
        else
        {
            Console.WriteLine("可用更新 : 否（已是最新）");
        }
        return 0;
    }

    private static int CmdUpgrade(string[] args)
    {
        if (args.Contains("--check", StringComparer.OrdinalIgnoreCase))
        {
            var (stale, ver, reason) = Daemon.Stale();
            Console.WriteLine("当前版本 : {0}", AppInfo.Version);
            if (Daemon.IsRunning())
            {
                var (ok, dv, dp, dd) = Daemon.Info();
                Console.WriteLine("守护进程 : 运行中(PID={0})", SafeDaemonPid());
                Console.WriteLine("守护版本 : {0}", ok ? "v" + dv : "未知（" + dd + "）");
                Console.WriteLine("版本一致 : {0}", !stale ? "是" : "否 —— " + reason);
            }
            else
            {
                Console.WriteLine("守护进程 : 已停止");
            }
            var (candidate, candidateVer) = Upgrade.FindCandidate();
            Console.WriteLine("升级候选 : {0}", candidate != null
                ? $"{Path.GetFileName(candidate)}（v{candidateVer}）"
                : "未发现（把新 exe 放进程序目录即可）");
            var upd = UpdateCheck.Get();
            if (upd.GetBool("ok") && upd.GetInt64("checked_at") > 0)
            {
                Console.WriteLine("远端最新 : v{0}（{1}；检查于 {2}）", upd.GetString("latest"),
                    upd.GetBool("available") ? "有更新，执行 update 查看下载页" : "已是最新",
                    Utils.FmtCheckedAt(upd.GetInt64("checked_at")));
            }
            else
            {
                Console.WriteLine("远端最新 : 未检查（执行 update 检查 GitHub Release）");
            }
            return 0;
        }
        var (ok2, msg) = Upgrade.Apply();
        Console.WriteLine((ok2 ? "升级成功：" : "升级失败：") + msg);
        return ok2 ? 0 : 1;
    }

    private static int SafeDaemonPid()
    {
        try
        {
            var text = File.ReadAllText(AppInfo.DaemonPidPath).Trim();
            return int.TryParse(text.Split('\n')[0].Trim(), out var pid) ? pid : 0;
        }
        catch
        {
            return 0;
        }
    }

    private static int CmdTest(JsonObject cfg)
    {
        Console.WriteLine("当前版本 : {0}", AppInfo.Version);
        Console.WriteLine("门户地址 : {0}:{1}{2}", cfg.GetString("portal_host"),
            cfg.GetInt("portal_port", 80), cfg.GetString("portal_base"));
        Console.WriteLine("凭据     : {0}", CredentialStore.Exists ? "已加密保存" : "缺失");
        var client = new PortalClient(cfg);
        var reachable = client.PortalReachable();
        Console.WriteLine("门户可达 : {0}", reachable ? "是" : "否（不在校园网？）");
        if (reachable)
        {
            var (online, location) = client.CheckOnline();
            Console.WriteLine("在线状态 : {0}",
                online == true ? "已在线" : online == false ? "未认证" : "探测无结论");
            if (!string.IsNullOrEmpty(location)) Console.WriteLine("门户重定向: {0}", location);
        }
        return 0;
    }
}