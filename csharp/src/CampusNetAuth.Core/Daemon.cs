using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>守护进程：单例端口、控制通道（STOP/CHECK/VER/NOTIFY）、认证循环。</summary>
public static class Daemon
{
    public static bool IsRunning()
    {
        try
        {
            using var client = new TcpClient();
            return client.ConnectAsync("127.0.0.1", AppInfo.SingletonPort)
                .Wait(TimeSpan.FromMilliseconds(500)) && client.Connected;
        }
        catch
        {
            return false;
        }
    }

    public static (bool Ok, string? Version, string? ExePath, string Detail) Info(double timeout = 1.5)
    {
        try
        {
            using var client = new TcpClient();
            if (!client.ConnectAsync("127.0.0.1", AppInfo.SingletonPort)
                    .Wait(TimeSpan.FromSeconds(timeout)))
                return (false, null, null, "守护未运行（Timeout）");
            var stream = client.GetStream();
            stream.ReadTimeout = (int)(timeout * 1000);
            stream.Write(Encoding.UTF8.GetBytes("VER"));
            var buffer = new byte[1024];
            var n = stream.Read(buffer, 0, buffer.Length);
            if (n <= 0) return (false, null, null, "无回执（旧版守护，不认识 VER 指令）");
            var raw = Encoding.UTF8.GetString(buffer, 0, n).Trim();
            if (raw.Length == 0) return (false, null, null, "无回执（旧版守护，不认识 VER 指令）");
            if (!raw.StartsWith("OK", StringComparison.OrdinalIgnoreCase))
                return (false, null, null, "回执无法解析：" + raw[..Math.Min(64, raw.Length)]);
            var body = raw[2..].Trim();
            var tab = body.IndexOf('\t');
            var ver = tab >= 0 ? body[..tab].Trim() : body.Trim();
            var path = tab >= 0 ? body[(tab + 1)..].Trim() : null;
            if (ver.Length == 0) return (false, null, null, "回执无法解析：" + raw[..Math.Min(64, raw.Length)]);
            return (true, ver, string.IsNullOrEmpty(path) ? null : path, "");
        }
        catch (Exception e)
        {
            return (false, null, null, "指令发送失败：" + e.GetType().Name);
        }
    }

    public static (bool Stale, string? Version, string Reason) Stale()
    {
        if (!IsRunning()) return (false, null, "守护未运行");
        var (ok, ver, path, detail) = Info();
        if (ok)
        {
            if (ver != AppInfo.Version)
                return (true, ver, $"守护版本 {ver} ≠ 当前 {AppInfo.Version}");
            if (!string.IsNullOrEmpty(path)
                && !path.Equals(AppInfo.ExePath, StringComparison.OrdinalIgnoreCase))
                return (true, ver, "守护运行的是另一份程序：" + path);
            return (false, ver, "");
        }
        var recorded = State.Load().GetString("daemon_version");
        if (recorded.Length > 0 && recorded == AppInfo.Version)
            return (false, recorded, "");
        return (true, recorded.Length > 0 ? recorded : null,
            detail.Length > 0 ? detail : "守护未报告版本");
    }

    public static bool Start()
    {
        try
        {
            var psi = new ProcessStartInfo(AppInfo.ExePath, "daemon")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = AppInfo.BaseDir,
            };
            Process.Start(psi);
            return true;
        }
        catch (Exception e)
        {
            Log.Error("守护启动失败: {0}", e.Message);
            return false;
        }
    }

    public static bool Stop(double waitTimeout = 8)
    {
        if (!IsRunning())
        {
            RemovePid();
            return true;
        }

        try
        {
            using var client = new TcpClient();
            if (client.ConnectAsync("127.0.0.1", AppInfo.SingletonPort)
                    .Wait(TimeSpan.FromSeconds(1)))
            {
                var s = client.GetStream();
                s.ReadTimeout = 1000;
                s.Write(Encoding.UTF8.GetBytes("STOP"));
                try { s.Read(new byte[16], 0, 16); } catch { }
            }
        }
        catch { }

        var deadline = DateTime.UtcNow.AddSeconds(6);
        while (DateTime.UtcNow < deadline)
        {
            if (!IsRunning()) break;
            Thread.Sleep(200);
        }

        if (IsRunning())
        {
            // STOP 不生效：按 pid 文件强杀（校验进程归属）
            try
            {
                var pid = ReadPid();
                if (pid > 0)
                {
                    var p = Process.GetProcessById(pid);
                    var path = TryGetPath(p);
                    if (path == null || path.Equals(AppInfo.ExePath, StringComparison.OrdinalIgnoreCase))
                        p.Kill(entireProcessTree: true);
                }
            }
            catch (Exception e)
            {
                Log.Debug("强制停止守护失败: {0}", e.Message);
            }
        }

        deadline = DateTime.UtcNow.AddSeconds(Math.Max(0, waitTimeout));
        while (DateTime.UtcNow < deadline && IsRunning()) Thread.Sleep(200);
        RemovePid();
        return !IsRunning();
    }

    private static string? TryGetPath(Process p)
    {
        try { return p.MainModule?.FileName; }
        catch { return null; }
    }

    private static int ReadPid()
    {
        try
        {
            var text = File.ReadAllText(AppInfo.DaemonPidPath).Trim();
            var first = text.Split('\n')[0].Trim();
            return int.TryParse(first, out var pid) ? pid : 0;
        }
        catch
        {
            return 0;
        }
    }

    private static void WritePid()
    {
        try { File.WriteAllText(AppInfo.DaemonPidPath, Environment.ProcessId.ToString()); }
        catch { }
    }

    private static void RemovePid()
    {
        try { File.Delete(AppInfo.DaemonPidPath); } catch { }
    }

    // ============================ 守护主循环 ============================

    /// <summary>错误提示附带的现场信息：门户 / 学号 / 运营商，方便判断配置是否选错。</summary>
    private static string Diagnostic(JsonObject cfg) =>
        $"门户 {cfg.GetString("portal_host")}:{cfg.GetInt("portal_port", 80)} · " +
        $"学号 {Utils.MaskUser(cfg.GetString("username"))} · " +
        $"运营商 {Utils.ServiceLabel(cfg.GetString("service"))}";

    /// <summary>
    /// 后台进程的简单版本检测：主界面没开时，发现新版本就在右下角提示 20s。
    /// 主界面在跑时交给界面的更新弹窗，不重复打扰；同一版本 12h 内只提示一次。
    /// </summary>
    private static void CheckUpdateNoticeAsync()
    {
        if (UiRunning()) return;
        Task.Run(() =>
        {
            try
            {
                if (UiRunning()) return;
                var info = UpdateCheck.CheckLatest(force: false);
                if (!info.GetBool("ok") || !info.GetBool("available")) return;
                var latest = info.GetString("latest");
                if (latest.Length == 0) return;
                var st = State.Load();
                if (st.GetString("update_notice_version") == latest &&
                    Utils.Now() - st.GetInt64("update_notice_at") < 12 * 3600)
                    return;
                State.Update(new JsonObject
                {
                    ["update_notice_version"] = latest,
                    ["update_notice_at"] = Utils.Now(),
                });
                Notify.Custom(new NotifyRequest
                {
                    Kind = NotifyKind.Success,
                    Position = NotifyPosition.BottomRight,
                    Title = "发现新版本 v" + latest,
                    Message = "打开主界面查看更新公告",
                    AutoCloseSeconds = 20,
                });
            }
            catch (Exception e)
            {
                Log.Debug("后台版本检查失败（忽略）: {0}", e.Message);
            }
        });
    }

    /// <summary>主界面是否在运行（UI 进程持有同名 Mutex）。</summary>
    private static bool UiRunning()
    {
        try
        {
            using var mutex = Mutex.OpenExisting("CampusNetAuth.UI");
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>确认在线/登录成功后清掉历史失败标记与失败事件，避免界面一直挂着旧警告。</summary>
    public static void ClearAuthIssues()
    {
        try
        {
            var st = State.Load();
            var hasError = st["last_error"] != null;
            var events = st["events"] as JsonArray;
            var hasBad = events != null && events.Any(e => e is JsonObject o &&
                (o.GetString("kind") == "login_fail" || o.GetString("kind") == "blocked"));
            if (!hasError && !hasBad) return;
            State.Mutate(s =>
            {
                s.Remove("last_error");
                if (s["events"] is JsonArray arr)
                {
                    for (var i = arr.Count - 1; i >= 0; i--)
                    {
                        if (arr[i] is JsonObject e &&
                            e.GetString("kind") is "login_fail" or "blocked")
                            arr.RemoveAt(i);
                    }
                }
            });
            Log.Info("认证已恢复：已清除历史登录错误与失败事件");
        }
        catch (Exception e)
        {
            Log.Debug("清理历史登录错误失败: {0}", e.Message);
        }
    }

    public static int Run(JsonObject cfg)
    {
        var listener = new TcpListener(IPAddress.Loopback, AppInfo.SingletonPort);
        try
        {
            listener.Start();
        }
        catch (SocketException)
        {
            Log.Warn("已有守护实例在运行（端口 {0} 被占用），本次退出", AppInfo.SingletonPort);
            return 1;
        }

        WritePid();
        DaemonGuard.ClearManualStop();
        try
        {
            var cred = CredentialStore.Load();
            if (string.IsNullOrEmpty(cfg.GetString("username")) || string.IsNullOrEmpty(cred))
            {
                Log.Error("尚未配置账号密码，请先运行 setup");
                return 1;
            }


            try { State.Update(new JsonObject { ["daemon_version"] = AppInfo.Version }); } catch { }
            var client = new PortalClient(cfg);

            // 开机先过代理守卫（校内一律关；校外只关死代理）
            try
            {
                var (_, changed) = ProxyGuard.MaybeClear(cfg, "守护启动：认证前清理系统代理", client);
                if (changed)
                    EventLog.Record("proxy", "检测到系统代理已开启，已自动关闭以保证网页可用");
            }
            catch (Exception e)
            {
                Log.Debug("启动时清理代理失败（已忽略）: {0}", e.Message);
            }

            var username = cfg.GetString("username");
            var interval = cfg.GetInt("interval", 30);
            var retry = cfg.GetInt("retry_interval", 30);
            var maxInterval = cfg.GetInt("max_interval", 600);
            var offlineInterval = cfg.GetInt("offline_interval", 60);
            var keepaliveIv = cfg.GetInt("keepalive_interval", 300);
            var verifyIv = cfg.GetInt("verify_interval", 5);
            var recheckIv = cfg.GetInt("recheck_interval", 30);
            var pauseIv = cfg.GetInt("logout_pause", 600);

            Log.Info("守护启动 | 账号={0} | 服务={1} | 门户={2}:{3} | 检测周期={4}s | 离线退避={5}s | " +
                     "失败重试={6}s | 最长退避={7}s | 请求超时={8}s | 保活={9}s | 恢复快探={10}s",
                Utils.MaskUser(username), cfg.GetString("service"), client.Host, client.Port,
                interval, offlineInterval, retry, maxInterval, cfg.GetInt("timeout", 8),
                keepaliveIv, recheckIv);
            Log.Info("守护规则：仅当校园网(iHBUT)门户可达且未认证时才登录，校外网络静默等待；" +
                     "在线时每 {0}s 主动续约会话", keepaliveIv);

            var totals = StatsTotals.Load();
            var stats = new Dictionary<string, int>
            {
                ["loops"] = 0, ["login_ok"] = 0, ["login_fail"] = 0,
                ["reconnects"] = 0, ["state_flips"] = 0, ["keepalive_ok"] = 0,
                ["stale_recover"] = 0,
            };

            void Bump(string key)
            {
                stats[key] = stats.GetValueOrDefault(key) + 1;
                totals[key] = totals.GetInt(key) + 1;
                try { StatsTotals.Save(totals); } catch { }
            }

            var persistedIndex = State.Load().GetString("userIndex");
            var startedAt = Environment.TickCount64 / 1000.0;
            var lastHeartbeat = 0.0;
            var lastKeepalive = 0.0;
            var failStreak = 0;
            var staleStreak = 0;
            var staleNotified = false;
            var portalOk = false;
            var prevOnline = (bool?)null;
            var pauseLogged = 0L;
            var blockedReason = "";
            var authClean = false;
            double? lastUpdateCheck = null;

            while (true)
            {
                try
                {
                    stats["loops"]++;

                    if (!portalOk)
                    {
                        if (!client.PortalReachable())
                        {
                            var wait = Math.Min(offlineInterval * Math.Min(failStreak + 1, 10), maxInterval);
                            if (failStreak == 0)
                                Log.Info("未检测到校园网门户（不在 iHBUT 网络），守护静默等待，不会尝试认证");
                            else if (failStreak % 10 == 1)
                                Log.Info("仍不在校园网：已连续 {0} 次探测不到门户，退避等待 {1}s", failStreak, wait);
                            failStreak++;
                            var slept = 0.0;
                            while (slept < wait)
                            {
                                var seg = Math.Min(recheckIv, wait - slept);
                                var signal = Wait(listener, seg);
                                if (signal == "STOP") return 0;
                                if (signal == "CHECK") break;
                                slept += seg;
                                if (client.PortalReachable())
                                {
                                    Log.Info("检测到校园网门户，提前结束退避，立即重连");
                                    break;
                                }
                            }
                            continue;
                        }
                        portalOk = true;
                        if (failStreak > 0)
                        {
                            stats["state_flips"]++;
                            failStreak = 0;
                            Log.Info("校园网门户已恢复，重置检测退避，立即重试");
                        }
                    }

                    var (online, loginPage) = client.CheckAuthentication();

                    if (online != prevOnline)
                    {
                        if (online == true && prevOnline == false)
                            EventLog.Record("restored", "网络已恢复在线");
                        else if (online == false && prevOnline == true)
                        {
                            if (pauseIv > 0 && LogoutPause.Left() > 0)
                                EventLog.Record("offline", "手动下线中，静默期内不会自动重连");
                            else
                                EventLog.Record("lost", "检测到未认证，正在自动重连…");
                        }
                        else if (online == null && prevOnline != null)
                            EventLog.Record("offline", "公网探测无结论，等待复核门户状态…");
                        prevOnline = online;
                    }

                    if (online == true)
                    {
                        if (!authClean)
                        {
                            ClearAuthIssues();
                            authClean = true;
                        }

                        // 联网/认证成功后再做版本检测（没有网络不可能查到更新）
                        var updateNow = Environment.TickCount64 / 1000.0;
                        if (lastUpdateCheck == null || updateNow - lastUpdateCheck >= 6 * 3600)
                        {
                            lastUpdateCheck = updateNow;
                            CheckUpdateNoticeAsync();
                        }

                        if (failStreak > 0) Log.Info("网络已恢复");
                        failStreak = 0;
                        staleStreak = 0;
                        staleNotified = false;
                        blockedReason = "";

                        var now = Environment.TickCount64 / 1000.0;
                        if (pauseIv > 0 && LogoutPause.Left() > 0)
                        {
                            // 在线时不打断用户操作，仅保持节奏
                        }

                        if (!string.IsNullOrEmpty(persistedIndex)
                            && now - lastKeepalive >= keepaliveIv)
                        {
                            lastKeepalive = now;
                            try
                            {
                                var kr = client.Keepalive(persistedIndex);
                                if (kr != null && kr.GetString("result").ToLowerInvariant() == "success")
                                {
                                    stats["keepalive_ok"]++;
                                }
                                else
                                {
                                    Log.Info("keepalive 续约未成功（{0}），立即复核在线状态…",
                                        kr == null ? "无响应" : Utils.SafeMessage(kr));
                                    var (online2, _) = client.CheckOnline();
                                    if (online2 == false) continue;
                                }
                            }
                            catch
                            {
                                lastKeepalive = now;
                            }
                        }

                        if (now - lastHeartbeat >= 300)
                        {
                            lastHeartbeat = now;
                            Log.Info("存活心跳 | 已运行 {0} | 在线 | 累计登录成功={1} 失败={2}（本次运行 +{3}/+{4}）",
                                Utils.FmtUptime(now - startedAt),
                                totals.GetInt("login_ok"), totals.GetInt("login_fail"),
                                stats["login_ok"], stats["login_fail"]);
                        }

                        if (Wait(listener, interval) == "STOP") return 0;
                        continue;
                    }

                    if (online == null)
                    {
                        portalOk = client.PortalReachable();
                        var wait = Math.Min(offlineInterval * Math.Min(failStreak + 1, 10), maxInterval);
                        if (failStreak == 0)
                            Log.Info("公网探测无结论，{0}，等待重试…",
                                portalOk ? "门户可达但页面不满足认证条件" : "校园网门户不可达");
                        else if (failStreak % 10 == 1)
                            Log.Info("公网探测仍无结论：已连续 {0} 次，退避等待 {1}s", failStreak, wait);
                        failStreak++;

                        var slept = 0.0;
                        while (slept < wait)
                        {
                            var seg = Math.Min(recheckIv, wait - slept);
                            var signal = Wait(listener, seg);
                            if (signal == "STOP") return 0;
                            if (signal == "CHECK") break;
                            slept += seg;
                            if (client.CheckOnline().Online != null)
                            {
                                Log.Info("公网探测已有结果，提前结束复核，立即复核");
                                break;
                            }
                        }
                        continue;
                    }

                    // ---- 手动下线静默期：不自动登回 ----
                    var left = pauseIv > 0 ? LogoutPause.Left() : 0;
                    if (left > 0)
                    {
                        var deadline = Utils.Now() + left;
                        if (pauseLogged != deadline)
                        {
                            pauseLogged = deadline;
                            Log.Info("处于「手动下线」静默期（剩 {0}s），不自动登录；" +
                                     "点界面「立即连接」或执行 login 可提前恢复", left);
                        }
                        if (Wait(listener, Math.Min(left, interval)) == "STOP") return 0;
                        continue;
                    }
                    pauseLogged = 0;

                    Log.Info("检测到未认证，开始登录…");
                    var res = client.Login(username, cred, loginPage: loginPage);
                    if (res.GetString("result") == "success")
                    {
                        Bump("login_ok");
                        ClearAuthIssues();
                        authClean = true;
                        if (failStreak > 0)
                        {
                            Bump("reconnects");
                            Log.Info("断线自动重连成功（累计自动重连 {0} 次）", stats["reconnects"]);
                        }
                        failStreak = 0;

                        if (!string.IsNullOrEmpty(res.GetString("userIndex")))
                        {
                            persistedIndex = res.GetString("userIndex");
                            State.Update(new JsonObject
                            {
                                ["userIndex"] = persistedIndex,
                                ["loginTime"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                            }, "last_error");
                        }

                        var info = client.OnlineInfo();
                        var extra = "";
                        if (info != null)
                        {
                            extra = $" | IP={info.GetString("userIp", "?")} 服务={info.GetString("service", "?")}" +
                                    $" 套餐={info.GetString("userGroup", "?")}";
                        }
                        Log.Info("登录成功{0}", extra);
                        EventLog.Record("login_ok", "认证成功" + extra);

                        if (Wait(listener, verifyIv) == "STOP") return 0;
                        var (online3, _) = client.CheckOnline();
                        if (online3 == false)
                        {
                            staleStreak++;
                            Bump("stale_recover");
                            Log.Warn("登录后复核未通过（疑似门户残留会话/认证异常，连续第 {0} 次），先主动下线清理再重新认证…",
                                staleStreak);
                            var (loOk, loMsg) = LogoutStale(client, persistedIndex, username, cred);
                            if (!loOk) Log.Warn("主动下线未确认：{0}", loMsg);
                            if (staleStreak >= 3 && !staleNotified)
                            {
                                staleNotified = true;
                                Notify.System("校园网认证异常", "门户返回成功但会话未建立（残留会话），已自动清理并重试");
                            }
                            if (Wait(listener, interval) == "STOP") return 0;
                            continue;
                        }
                        Notify.Custom(new NotifyRequest
                        {
                            Kind = NotifyKind.Success,
                            Title = "网络已连接",
                            Message = "校园网认证成功",
                            Context = Diagnostic(cfg),
                            AutoCloseSeconds = 6,
                        });
                        if (Wait(listener, interval) == "STOP") return 0;
                        continue;
                    }

                    // ---- 登录失败 ----
                    Bump("login_fail");
                    failStreak++;
                    authClean = false;
                    var blocked = res.GetString("_blocked");
                    if (blocked.Length > 0)
                    {
                        if (blocked != blockedReason)
                        {
                            blockedReason = blocked;
                            Log.Error("无法自动认证：{0}", blocked);
                            EventLog.Record("blocked", "无法自动认证：" + blocked);
                            State.Update(new JsonObject
                            {
                                ["last_error"] = new JsonObject
                                {
                                    ["t"] = Utils.Now(),
                                    ["msg"] = blocked,
                                },
                            });
                            Notify.Custom(new NotifyRequest
                            {
                                Kind = NotifyKind.Error,
                                Title = "需要人工认证",
                                Message = blocked,
                                Context = Diagnostic(cfg),
                                ShowOpenSettings = true,
                                AutoCloseSeconds = 30,
                            });
                        }
                        var blockedWait = Math.Max(maxInterval, 1800);
                        Log.Error("按门户改造处理：{0}s 后再试一次（其间不会反复提交凭据）", blockedWait);
                        if (Wait(listener, blockedWait) == "STOP") return 0;
                        continue;
                    }
                    blockedReason = "";

                    var msg = res.GetString("message");
                    var hint = res.GetString("_hint");
                    var display = msg + (hint.Length > 0 ? $"（{hint}）" : "");
                    var waitFail = Math.Min(retry * failStreak, maxInterval);
                    Log.Error("登录失败: {0}{1} | 累计失败={2} | 下次重试={3}s",
                        msg, hint.Length > 0 ? $"（{hint}）" : "",
                        stats["login_fail"], waitFail);
                    EventLog.Record("login_fail", "登录失败：" + (msg.Length > 0 ? msg : hint));

                    var prevMsg = (State.Load()["last_error"] as JsonObject)
                        ?.GetString("msg") ?? "";
                    if (display.Length > 0 && display != prevMsg)
                    {
                        State.Update(new JsonObject
                        {
                            ["last_error"] = new JsonObject
                            {
                                ["t"] = Utils.Now(),
                                ["msg"] = display,
                            },
                        });
                        Notify.Custom(new NotifyRequest
                        {
                            Kind = NotifyKind.Error,
                            Title = "认证失败",
                            Message = display,
                            Context = Diagnostic(cfg),
                            ShowOpenSettings = true,
                            AutoCloseSeconds = 30,
                        });
                    }

                    if (new[] { "PASS", "密码", "USER", "账号", "EXIST", "FORBIDDEN" }
                        .Any(k => msg.Contains(k, StringComparison.Ordinal)))
                    {
                        Log.Error("疑似账号/密码问题，请运行 setup 重新配置");
                        waitFail = Math.Min(retry * 20, maxInterval);
                    }

                    if (Wait(listener, waitFail) == "STOP") return 0;
                }
                catch (Exception e)
                {
                    Log.Error("守护循环异常: {0}", e.Message);
                    if (Wait(listener, 10) == "STOP") return 0;
                }
            }
        }
        finally
        {
            try { listener.Stop(); } catch { }
            RemovePid();
        }
    }

    private static (bool Ok, string Message) LogoutStale(PortalClient client, string? ui,
        string username, string password)
    {
        try
        {
            var res = client.Logout(ui, forceByCred: true, username: username, password: password);
            var ok = res.GetString("result").ToLowerInvariant() == "success";
            return (ok, Utils.SafeMessage(res));
        }
        catch (Exception e)
        {
            return (false, e.Message);
        }
    }

    /// <summary>
    /// 可中断等待：STOP 返回 "STOP"，CHECK 返回 "CHECK"，超时返回 null。
    /// 空连接/VER/NOTIFY 不打断等待。
    /// </summary>
    private static string? Wait(TcpListener listener, double seconds)
    {
        var deadline = DateTime.UtcNow.AddSeconds(Math.Max(0, seconds));
        while (true)
        {
            var remain = deadline - DateTime.UtcNow;
            if (remain <= TimeSpan.Zero) return null;
            if (!listener.Pending())
            {
                Thread.Sleep((int)Math.Min(100, remain.TotalMilliseconds));
                continue;
            }

            try
            {
                using var client = listener.AcceptTcpClient();
                var stream = client.GetStream();
                stream.ReadTimeout = 300;
                var buffer = new byte[64];
                var n = 0;
                try { n = stream.Read(buffer, 0, buffer.Length); } catch { }
                var command = Encoding.UTF8.GetString(buffer, 0, n).Trim().ToUpperInvariant();
                if (command == "STOP")
                {
                    Reply(stream, "OK");
                    return "STOP";
                }
                if (command == "CHECK")
                {
                    Reply(stream, "OK");
                    return "CHECK";
                }
                if (command == "VER")
                {
                    Reply(stream, $"OK {AppInfo.Version}\t{AppInfo.ExePath}");
                    continue;
                }
                if (command == "NOTIFY")
                {
                    Notify.Custom(new NotifyRequest
                    {
                        Kind = NotifyKind.Success,
                        Title = "通知测试",
                        Message = "守护进程自绘横幅正常",
                        Context = Diagnostic(Config.Load()),
                        AutoCloseSeconds = 6,
                    });
                    Reply(stream, "OK");
                    continue;
                }
                if (command == "NOTIFYERR")
                {
                    // 隐藏测试指令：验证带按钮的错误卡（示例信息，不含真实账号）
                    Notify.Custom(new NotifyRequest
                    {
                        Kind = NotifyKind.Error,
                        Title = "认证失败（测试）",
                        Message = "门户登录页已启用验证码，无法自动认证",
                        Context = "门户 10.0.0.1:80 · 学号 1234567890 · 运营商 自动",
                        ShowOpenSettings = true,
                        AutoCloseSeconds = 20,
                    });
                    Reply(stream, "OK");
                    continue;
                }
                // 空连接/未知指令：不打断
            }
            catch
            {
                // 连接异常忽略，继续等待
            }
        }
    }

    private static void Reply(NetworkStream stream, string text)
    {
        try
        {
            stream.Write(Encoding.UTF8.GetBytes(text));
            stream.Flush();
        }
        catch { }
    }
}