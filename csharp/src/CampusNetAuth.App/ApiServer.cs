using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using CampusNetAuth;

namespace CampusNetAuth.App;

/// <summary>本地管理 API（自建 TCP HTTP 服务）：token + Host 校验，端点与 Python 版对齐。</summary>
public sealed class ApiServer : IDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly Dictionary<string, TaskEntry> _tasks = new();
    private readonly object _lock = new();
    private (long Time, bool? Online, JsonObject Info, bool? Portal)? _probe;
    private CancellationTokenSource? _cts;
    private Task? _loop;
    private readonly bool _firstDeploy;
    private readonly List<string> _foreignFiles;

    public string Token { get; } = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
    public int Port { get; private set; }

    public event Action<string>? WindowAction;

    private sealed class TaskEntry
    {
        public string Status = "running";
        public string Output = "";
    }

    public ApiServer(bool firstDeploy, List<string> foreignFiles)
    {
        _firstDeploy = firstDeploy;
        _foreignFiles = foreignFiles;
    }

    public string Start()
    {
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        _cts = new CancellationTokenSource();
        var token = _cts.Token;
        _loop = Task.Run(() => MiniHttp.ServeAsync(_listener, Handle, token));
        Log.Info("本地管理接口已启动：{0}", Url);
        return Url;
    }

    public string Url => $"http://127.0.0.1:{Port}/?token={Token}";

    private MiniHttpResponse Handle(MiniHttpRequest request)
    {
        var host = request.Headers.GetValueOrDefault("Host", "");
        if (!host.Equals($"127.0.0.1:{Port}", StringComparison.OrdinalIgnoreCase)
            && !host.Equals($"localhost:{Port}", StringComparison.OrdinalIgnoreCase))
        {
            return MiniHttp.Text(403, "text/plain", "forbidden host");
        }

        if (request.Path == "/")
        {
            if (request.QueryValue("token") != Token)
                return MiniHttp.Text(403, "text/plain", "forbidden");
            return MiniHttp.Text(200, "text/html; charset=utf-8", UiResource.ReadIndexHtml());
        }

        if (!request.Path.StartsWith("/api/", StringComparison.Ordinal))
            return MiniHttp.Text(404, "text/plain", "not found");

        if (request.Headers.GetValueOrDefault("X-CNA-Token", "") != Token)
            return JsonResponse(new JsonObject { ["ok"] = false, ["error"] = "forbidden" }, 403);

        var data = ParseBody(request.Body);
        return Dispatch(request, data);
    }

    private static JsonObject ParseBody(byte[] body)
    {
        if (body.Length == 0) return new JsonObject();
        try
        {
            return JsonNode.Parse(Encoding.UTF8.GetString(body)) as JsonObject ?? new JsonObject();
        }
        catch
        {
            return new JsonObject();
        }
    }

    private static int _statusHits;

    private MiniHttpResponse Dispatch(MiniHttpRequest request, JsonObject data)
    {
        var path = request.Path;
        if (path == "/api/status" && Interlocked.Increment(ref _statusHits) == 1)
        {
            // 无截屏验证用：WebView2 页面加载后会立刻轮询一次状态
            Log.Info("界面已开始轮询状态（WebView2 页面已加载）");
        }
        switch (path)
        {
            case "/api/status":
                return JsonResponse(BuildStatus());
            case "/api/config":
                if (request.Method == "GET")
                    return JsonResponse(new JsonObject { ["ok"] = true, ["cfg"] = Config.Load() });
                var cfg = Config.Load();
                foreach (var kv in data)
                {
                    if (kv.Key is "username" or "service" or "proxy_guard"
                        or "interval" or "offline_interval" or "retry_interval"
                        or "max_interval" or "timeout")
                        cfg[kv.Key] = kv.Value?.DeepClone();
                }
                Config.Save(Config.Sanitize(cfg));
                return JsonResponse(new JsonObject { ["ok"] = true });
            case "/api/connect":
                return JsonResponse(Connect(data));
            case "/api/logout":
                return JsonResponse(Logout());
            case "/api/log":
            {
                var n = 200;
                if (int.TryParse(request.QueryValue("n"), out var parsed)) n = parsed;
                return JsonResponse(new JsonObject { ["text"] = TailLog(n) });
            }
            case "/api/task":
                return JsonResponse(TaskStatus(request.QueryValue("id") ?? ""));
            case "/api/daemon/start":
                return JsonResponse(new JsonObject { ["ok"] = Daemon.Start() });
            case "/api/daemon/stop":
                return JsonResponse(new JsonObject { ["ok"] = Daemon.Stop() });
            case "/api/daemon/restart":
                Daemon.Stop();
                Thread.Sleep(800);
                return JsonResponse(new JsonObject { ["ok"] = Daemon.Start() });
            case "/api/daemon/refresh":
                return JsonResponse(new JsonObject { ["id"] = StartTask(DaemonRefreshTask, "daemon_refresh") });
            case "/api/autostart/install":
                return JsonResponse(new JsonObject { ["ok"] = Autostart.Install(), ["message"] = "开机自启已启用" });
            case "/api/autostart/uninstall":
                return JsonResponse(new JsonObject { ["ok"] = Autostart.Uninstall(), ["message"] = "开机自启已移除" });
            case "/api/proxy/off":
            {
                var (ok, changed, before) = ProxyGuard.Disable("界面手动关闭");
                return JsonResponse(new JsonObject
                {
                    ["ok"] = ok,
                    ["changed"] = changed,
                    ["before"] = ProxyJson(before),
                    ["proxy"] = ProxyJson(ProxyGuard.GetState()),
                });
            }
            case "/api/proxy/restore":
            {
                var (ok, msg) = ProxyGuard.Restore();
                return JsonResponse(new JsonObject
                {
                    ["ok"] = ok,
                    ["message"] = msg,
                    ["proxy"] = ProxyJson(ProxyGuard.GetState()),
                });
            }
            case "/api/update/check":
                return JsonResponse(new JsonObject
                {
                    ["ok"] = true,
                    ["update"] = AppInfo.IsInstalled
                        ? VelopackUpdateInfo()
                        : UpdateCheck.CheckLatest(force: true),
                });
            case "/api/update/open":
                return JsonResponse(new JsonObject { ["ok"] = UpdateCheck.OpenReleasePage() });
            case "/api/update/install":
                return JsonResponse(new JsonObject
                {
                    ["id"] = AppInfo.IsInstalled
                        ? StartTask(VelopackUpdater.DownloadAndApply, "update_install")
                        : StartTask(UpdateInstallTask, "update_install"),
                });
            case "/api/upgrade/apply":
                return JsonResponse(new JsonObject { ["id"] = StartTask(UpgradeTask, "upgrade") });
            case "/api/self-test":
                return JsonResponse(new JsonObject { ["id"] = StartTask(SelfTestTask, "self_test") });
            case "/api/health":
                return JsonResponse(new JsonObject { ["id"] = StartTask(HealthTask, "health") });
            case "/api/notify-test":
            {
                var uiOk = Notify.System("校园网无感认证", "界面进程通知测试", 3, dedup: false);
                var daemonOk = false;
                var daemonMsg = "守护未运行";
                if (Daemon.IsRunning())
                {
                    try
                    {
                        using var c = new TcpClient();
                        c.Connect("127.0.0.1", AppInfo.SingletonPort);
                        var s = c.GetStream();
                        s.ReadTimeout = 2000;
                        s.Write(Encoding.UTF8.GetBytes("NOTIFY"));
                        var buf = new byte[32];
                        var n = s.Read(buf, 0, buf.Length);
                        daemonOk = n > 0 && Encoding.UTF8.GetString(buf, 0, n).StartsWith("OK");
                        daemonMsg = daemonOk ? "" : "守护无回执";
                    }
                    catch (Exception e)
                    {
                        daemonMsg = e.Message;
                    }
                }
                return JsonResponse(new JsonObject
                {
                    ["ui_ok"] = uiOk,
                    ["daemon_ok"] = daemonOk,
                    ["daemon_msg"] = daemonMsg,
                });
            }
            case "/api/open-dir":
                try
                {
                    Process.Start(new ProcessStartInfo(AppInfo.BaseDir) { UseShellExecute = true });
                    return JsonResponse(new JsonObject { ["ok"] = true });
                }
                catch (Exception e)
                {
                    return JsonResponse(new JsonObject { ["ok"] = false, ["error"] = e.Message });
                }
            case "/api/diagnose":
                return JsonResponse(Diagnose());
            case "/api/uninstall":
                return JsonResponse(new JsonObject
                {
                    ["ok"] = false,
                    ["message"] = "卸载器将在安装器里程碑提供（当前请直接删除程序目录）",
                });
            case "/api/window":
                WindowAction?.Invoke(data.GetString("action"));
                return JsonResponse(new JsonObject { ["ok"] = true });
            default:
                return JsonResponse(new JsonObject { ["ok"] = false, ["error"] = "unknown endpoint" });
        }
    }

    // ---------- 状态 ----------
    private void Probe(JsonObject cfg, PortalClient client,
        out bool? online, out JsonObject info, out bool? portal)
    {
        lock (_lock)
        {
            if (_probe is { } cached && Utils.Now() - cached.Time < 5)
            {
                online = cached.Online;
                info = (JsonObject)cached.Info.DeepClone();
                portal = cached.Portal;
                return;
            }
        }

        var (o, _) = client.CheckOnline();
        bool? p = null;
        JsonObject i = new();
        if (o == null)
        {
            p = client.PortalReachable();
            if (p == true) i = client.OnlineInfo() ?? new JsonObject();
        }
        else if (o == true)
        {
            i = client.OnlineInfo() ?? new JsonObject();
        }
        lock (_lock) _probe = (Utils.Now(), o, i, p);
        online = o;
        info = (JsonObject)i.DeepClone();
        portal = p;
    }

    private JsonObject BuildStatus()
    {
        var cfg = Config.Load();
        var client = new PortalClient(cfg);
        Probe(cfg, client, out var online, out var info, out var portal);

        var st = State.Load();
        var (stale, daemonVer, _) = Daemon.Stale();
        var running = Daemon.IsRunning();
        var (candidate, candidateVer) = Upgrade.FindCandidate();

        var netText = online switch
        {
            true => "已连接",
            false => "未认证（被门户拦截）",
            null => portal == true ? "门户可达 · 探测无结论" : "不在校园网（门户不可达）",
        };

        var proxy = ProxyGuard.GetState();
        var foreign = new JsonArray();
        foreach (var f in _foreignFiles) foreign.Add(f);

        return new JsonObject
        {
            ["online"] = online,
            ["info"] = info,
            ["portal"] = portal,
            ["net_text"] = netText,
            ["host"] = $"{client.Host}:{client.Port}",
            ["interval"] = cfg.GetInt("interval", 30),
            ["cfg"] = cfg,
            ["service"] = cfg.GetString("service"),
            ["username"] = cfg.GetString("username"),
            ["app_version"] = AppInfo.Version,
            ["autostart"] = Autostart.Status(),
            ["autostart_health"] = new JsonArray(Autostart.Health().Ok, Autostart.Health().Reason),
            ["daemon"] = running,
            ["daemon_pid"] = ReadPid(),
            ["daemon_version"] = daemonVer,
            ["daemon_stale"] = stale,
            ["upgrade"] = candidate != null
                ? new JsonObject
                {
                    ["version"] = candidateVer,
                    ["name"] = Path.GetFileName(candidate),
                }
                : null,
            ["update"] = UpdateCheck.Get(),
            ["uninstaller"] = File.Exists(Path.Combine(AppInfo.BaseDir, "CampusNetAuthUninstall.exe")),
            ["first_deploy"] = _firstDeploy,
            ["foreign_files"] = foreign,
            ["events"] = st["events"]?.DeepClone() ?? new JsonArray(),
            ["last_error"] = st["last_error"]?.DeepClone(),
            ["proxy"] = ProxyJson(proxy),
            ["proxy_guard"] = cfg.GetString("proxy_guard", "auto"),
            ["proxy_backup"] = st["proxy_backup"]?.DeepClone(),
        };
    }

    private static JsonObject ProxyJson(ProxyGuard.ProxyState s) => new()
    {
        ["enabled"] = s.Enabled,
        ["server"] = s.Server,
        ["pac"] = s.Pac,
        ["pac_wpad"] = s.PacWpad,
        ["active"] = s.Active,
        ["detail"] = s.Detail,
    };

    private static int ReadPid()
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

    // ---------- 动作 ----------
    private JsonObject Connect(JsonObject data)
    {
        var cfg = Config.Load();
        if (data["host"] != null) cfg["portal_host"] = data["host"]!.DeepClone();
        if (data["username"] != null) cfg["username"] = data["username"]!.DeepClone();
        if (data["service"] != null) cfg["service"] = data["service"]!.DeepClone();
        var password = data.GetString("password");
        Config.Save(Config.Sanitize(cfg));
        if (password.Length > 0 && !CredentialStore.Save(password))
            return new JsonObject { ["ok"] = false, ["error"] = "密码保存失败（DPAPI）" };

        cfg = Config.Load();
        var cred = CredentialStore.Load();
        if (string.IsNullOrEmpty(cfg.GetString("username")) || string.IsNullOrEmpty(cred))
            return new JsonObject { ["ok"] = false, ["error"] = "尚未配置账号密码" };

        LogoutPause.Clear();
        var client = new PortalClient(cfg);
        ProxyGuard.MaybeClear(cfg, "界面立即连接前清理系统代理", client);

        var (online, _) = client.CheckOnline();
        if (online == true)
        {
            InvalidateProbe();
            return new JsonObject { ["ok"] = true, ["result"] = "already_online" };
        }
        var res = client.Login(cfg.GetString("username"), cred);
        InvalidateProbe();
        if (res.GetString("result") == "success")
        {
            if (res.GetString("userIndex").Length > 0)
            {
                State.Update(new JsonObject
                {
                    ["userIndex"] = res.GetString("userIndex"),
                    ["loginTime"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                }, "last_error");
            }
            return new JsonObject { ["ok"] = true, ["result"] = "success" };
        }
        return new JsonObject
        {
            ["ok"] = false,
            ["error"] = res.GetString("message", "登录失败"),
            ["hint"] = res.GetString("_hint"),
        };
    }

    private JsonObject Logout()
    {
        var cfg = Config.Load();
        var cred = CredentialStore.Load();
        var client = new PortalClient(cfg);
        var res = client.Logout(State.Load().GetString("userIndex"));
        var ok = res.GetString("result").ToLowerInvariant() == "success";
        if (!ok && !string.IsNullOrEmpty(cred))
        {
            var r = client.Logout(null, forceByCred: true,
                username: cfg.GetString("username"), password: cred);
            ok = r.GetString("result").ToLowerInvariant() == "success";
        }
        if (!ok) return new JsonObject { ["ok"] = false, ["error"] = "门户未确认下线" };
        State.Mutate(s =>
        {
            s.Remove("userIndex");
            s.Remove("loginTime");
            s.Remove("last_error");
        });
        var pause = cfg.GetInt("logout_pause", 600);
        if (pause > 0) LogoutPause.Set(pause);
        InvalidateProbe();
        return new JsonObject { ["ok"] = true, ["rc"] = 0, ["pause"] = LogoutPause.Left() };
    }

    private void InvalidateProbe()
    {
        lock (_lock) _probe = null;
    }

    // ---------- 任务 ----------
    private string StartTask(Func<string> work, string name)
    {
        var id = Convert.ToHexString(RandomNumberGenerator.GetBytes(6)).ToLowerInvariant();
        var entry = new TaskEntry();
        lock (_lock) _tasks[id] = entry;
        Task.Run(() =>
        {
            try
            {
                var output = work();
                lock (_lock)
                {
                    entry.Status = "done";
                    entry.Output = output;
                }
                Log.Info("后台任务完成（{0}），输出:\n{1}", name, Utils.Truncate(output, 4000));
            }
            catch (Exception e)
            {
                lock (_lock)
                {
                    entry.Status = "error";
                    entry.Output = e.Message;
                }
                Log.Error("后台任务失败（{0}）: {1}", name, e.Message);
            }
        });
        return id;
    }

    private JsonObject TaskStatus(string id)
    {
        lock (_lock)
        {
            if (!_tasks.TryGetValue(id, out var entry))
                return new JsonObject { ["status"] = "error", ["output"] = "任务不存在" };
            if (entry.Status == "running")
                return new JsonObject { ["status"] = "running", ["output"] = "" };
            _tasks.Remove(id);
            return new JsonObject { ["status"] = entry.Status, ["output"] = entry.Output };
        }
    }

    private string DaemonRefreshTask()
    {
        var (stale, ver, reason) = Daemon.Stale();
        if (!stale) return $"后台守护已是当前版本 v{AppInfo.Version}，无需处理。";
        var before = ver;
        Daemon.Stop();
        if (!Daemon.Start())
            return $"守护仍是旧版本（{reason}），且无法自动重启：请先停止守护后重新启动程序。";
        return $"后台守护已重启为 v{AppInfo.Version}（原 v{before ?? "未知"}），新版本功能已生效。";
    }

    /// <summary>安装版：把 Velopack 检查结果映射成与 GitHub 检查一致的字段。</summary>
    private static JsonObject VelopackUpdateInfo()
    {
        var (ok, message, update) = VelopackUpdater.Check();
        var latest = update?.TargetFullRelease.Version.ToString() ?? "";
        return new JsonObject
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
    }

    private string UpdateInstallTask()
    {
        var info = UpdateCheck.Get();
        if (!(info.GetBool("ok") && info.GetBool("available")))
            return "当前没有可用的新版本（请先点「检查更新」）";
        var (path, err) = UpdateCheck.DownloadAsset(info);
        if (path == null) return "下载失败：" + err;
        var (ok, msg) = Upgrade.Apply(path);
        if (!ok)
        {
            try { File.Delete(path); } catch { }
            return "升级失败：" + msg;
        }
        Notify.System("校园网无感认证", msg);
        return msg;
    }

    private string UpgradeTask()
    {
        var (ok, msg) = Upgrade.Apply();
        if (ok) Notify.System("校园网无感认证", msg);
        return msg;
    }

    private string SelfTestTask()
    {
        var cfg = Config.Load();
        var sb = new StringBuilder();
        sb.AppendLine("CampusNetAuth 自检");
        sb.AppendLine($"版本: {AppInfo.Version}");
        sb.AppendLine($"门户: {cfg.GetString("portal_host")}:{cfg.GetInt("portal_port", 80)}{cfg.GetString("portal_base")}");
        sb.AppendLine($"账号: {Utils.MaskUser(cfg.GetString("username"))}");
        sb.AppendLine($"凭据: {(CredentialStore.Exists ? "已加密保存" : "缺失")}");
        sb.AppendLine($"开机自启: {(Autostart.Status() ? "已启用" : "未启用")}");
        sb.AppendLine($"守护: {(Daemon.IsRunning() ? "运行中 v" + (Daemon.Info().Version ?? "?") : "已停止")}");

        var client = new PortalClient(cfg);
        var reachable = client.PortalReachable();
        sb.AppendLine($"门户可达: {(reachable ? "是" : "否")}");
        if (reachable)
        {
            var (online, _) = client.CheckOnline();
            sb.AppendLine($"在线状态: {(online == true ? "已在线" : online == false ? "未认证" : "探测无结论")}");
            var (url, html) = client.DiscoverLoginPage();
            sb.AppendLine(html != null ? $"登录页: {url}" : "登录页: 未发现（网关不劫持且 hints 未命中）");
        }
        return sb.ToString();
    }

    private string HealthTask()
    {
        if (!Daemon.IsRunning()) return "体检需要后台守护配合，请先启动守护。";
        var cfg = Config.Load();
        var client = new PortalClient(cfg);
        var ui = State.Load().GetString("userIndex");
        var (before, _) = client.CheckOnline();
        var sb = new StringBuilder();
        sb.AppendLine($"体检开始：在线={before}");
        var res = client.Logout(ui);
        sb.AppendLine("已请求下线：" + (res.GetString("result") == "success" ? "门户确认" : Utils.SafeMessage(res)));
        var started = Utils.Now();
        for (var i = 0; i < 15; i++)
        {
            Thread.Sleep(5000);
            var (online, _) = client.CheckOnline();
            if (online == true)
            {
                sb.AppendLine($"断线后 {Utils.Now() - started}s 恢复在线。");
                return sb.ToString();
            }
        }
        sb.AppendLine("75 秒内未恢复在线：请检查守护日志与门户提示。");
        return sb.ToString();
    }

    private JsonObject Diagnose()
    {
        try
        {
            var cfg = Config.Load();
            var client = new PortalClient(cfg);
            var reachable = client.PortalReachable();
            var (online, location) = reachable ? client.CheckOnline() : (null, null);
            var (url, html) = reachable ? client.DiscoverLoginPage() : (null, null);
            var dir = Path.Combine(AppInfo.BaseDir, "probe");
            Directory.CreateDirectory(dir);
            var result = new JsonObject
            {
                ["time"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                ["portal"] = client.Url(""),
                ["reachable"] = reachable,
                ["online"] = online?.ToString() ?? "null",
                ["redirect"] = location ?? "",
                ["login_page"] = url ?? "",
                ["login_page_ok"] = html != null,
            };
            File.WriteAllText(Path.Combine(dir, "capture_result.json"),
                result.ToJsonString(Json.Options), Encoding.UTF8);
            return new JsonObject { ["ok"] = true };
        }
        catch (Exception e)
        {
            return new JsonObject { ["ok"] = false, ["error"] = e.Message };
        }
    }

    private static string TailLog(int n)
    {
        try
        {
            if (!File.Exists(AppInfo.LogPath)) return "（暂无日志）";
            using var fs = new FileStream(AppInfo.LogPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var length = fs.Length;
            var start = Math.Max(0, length - 256 * 1024);
            fs.Seek(start, SeekOrigin.Begin);
            using var reader = new StreamReader(fs, Encoding.UTF8);
            var lines = reader.ReadToEnd().Split('\n');
            return string.Join("\n", lines.TakeLast(Math.Max(1, n))).TrimEnd();
        }
        catch (Exception e)
        {
            return "（暂无日志）" + e.Message;
        }
    }

    private static MiniHttpResponse JsonResponse(JsonObject obj, int status = 200) =>
        MiniHttp.Text(status, "application/json; charset=utf-8", obj.ToJsonString(Json.Options));

    public void Dispose()
    {
        try { _cts?.Cancel(); } catch { }
        try { _listener.Stop(); } catch { }
    }
}

/// <summary>读取嵌入的 index.html（构建时通过 EmbeddedResource 打入）。</summary>
internal static class UiResource
{
    public static string ReadIndexHtml()
    {
        var asm = typeof(UiResource).Assembly;
        using var stream = asm.GetManifestResourceStream("CampusNetAuth.ui.index.html")
                           ?? throw new InvalidOperationException("内嵌 index.html 缺失");
        using var reader = new StreamReader(stream, Encoding.UTF8);
        return reader.ReadToEnd();
    }
}