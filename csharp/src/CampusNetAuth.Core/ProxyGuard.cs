using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using Microsoft.Win32;

namespace CampusNetAuth;

/// <summary>
/// 系统代理守卫：开机/认证前处理 WinINET 系统代理与 PAC。
/// 规则：校园网内一律关；校外只关"死代理"（本地代理端口无人监听）。
/// </summary>
public static class ProxyGuard
{
    private const string InternetSettingsKey =
        @"Software\Microsoft\Windows\CurrentVersion\Internet Settings";

    private const int InternetOptionRefresh = 37;
    private const int InternetOptionSettingsChanged = 39;

    [DllImport("wininet.dll", SetLastError = true)]
    private static extern bool InternetSetOptionW(IntPtr hInternet, int dwOption,
        IntPtr lpBuffer, int dwBufferLength);

    public sealed record ProxyState(
        bool Enabled, string Server, string Pac, bool PacWpad, bool Active, string Detail);

    public static ProxyState GetState()
    {
        var pe = ReadDword("ProxyEnable");
        var ps = ReadString("ProxyServer");
        var pac = ReadString("AutoConfigURL");
        var wpad = ReadDword("AutoDetect");
        var enabled = pe;
        var active = enabled || !string.IsNullOrEmpty(pac);
        var bits = new List<string>();
        if (enabled)
            bits.Add("系统代理已开启" + (ps.Length > 0 ? $"（{ps}）" : ""));
        if (pac.Length > 0)
            bits.Add("PAC 自动配置：" + (pac.Length > 120 ? pac[..120] : pac));
        if (!active) bits.Add("未启用系统代理");
        return new ProxyState(enabled, ps, pac, wpad, active, string.Join("；", bits));
    }

    /// <summary>关闭系统代理与 PAC（幂等）。changed 只在真正写入成功时为真。</summary>
    public static (bool Ok, bool Changed, ProxyState Before) Disable(
        string reason = "开机自动认证前清理代理", bool record = true)
    {
        var st = GetState();
        if (!st.Active) return (true, false, st);

        if (record)
        {
            try
            {
                State.Mutate(s =>
                {
                    if (s["proxy_backup"] is JsonObject) return;
                    s["proxy_backup"] = new JsonObject
                    {
                        ["t"] = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                        ["ProxyEnable"] = st.Enabled ? 1 : 0,
                        ["ProxyServer"] = st.Server,
                        ["AutoConfigURL"] = st.Pac,
                        ["reason"] = reason,
                    };
                });
            }
            catch (Exception e)
            {
                Log.Debug("备份代理设置失败（继续关闭）: {0}", e.Message);
            }
        }

        var ok = true;
        if (st.Enabled) ok = WriteDword("ProxyEnable", 0) && ok;
        if (st.Pac.Length > 0) ok = DeleteValue("AutoConfigURL") && ok;
        NotifyChanged();

        if (ok)
            Log.Info("已关闭系统代理（{0}）{1}", reason,
                record ? "｜原设置已备份，可随时恢复" : "");
        else
            Log.Warn("关闭系统代理部分失败（{0}）", reason);
        return (ok, ok, st);
    }

    /// <summary>按备份恢复关闭前的代理设置。</summary>
    public static (bool Ok, string Message) Restore()
    {
        JsonObject? bak = null;
        try
        {
            bak = State.Load()["proxy_backup"] as JsonObject;
        }
        catch { }
        if (bak == null) return (false, "没有可恢复的代理设置（未记录过关闭前的状态）");

        var pe = bak.GetInt("ProxyEnable", 0) != 0;
        var ps = bak.GetString("ProxyServer");
        var pac = bak.GetString("AutoConfigURL");
        var ok = WriteDword("ProxyEnable", pe ? 1 : 0);
        if (ps.Length > 0) ok = WriteString("ProxyServer", ps) && ok;
        if (pac.Length > 0) ok = WriteString("AutoConfigURL", pac) && ok;
        NotifyChanged();
        if (!ok) return (false, "恢复代理设置失败，详见日志");

        try
        {
            State.Mutate(s => s.Remove("proxy_backup"));
        }
        catch { }
        Log.Info("系统代理已恢复");
        return (true, "已恢复关闭前的代理设置");
    }

    /// <summary>
    /// 统一入口：校园网内一律关；校外只关死代理（本地端口无人监听）；
    /// client 为 null 表示显式动作，只看配置。
    /// </summary>
    public static (bool Ok, bool Changed) MaybeClear(JsonObject cfg, string reason,
        PortalClient? client = null)
    {
        try
        {
            var mode = cfg.GetString("proxy_guard", "auto").ToLowerInvariant();
            if (mode == "off") return (true, false);

            if (client != null)
            {
                bool onCampus;
                try { onCampus = client.PortalReachable(); }
                catch { onCampus = false; }
                if (!onCampus)
                {
                    var st = GetState();
                    if (!st.Active) return (true, false);
                    if (LocalEndpointAlive(st.Server) != false)
                    {
                        Log.Debug("校外的系统代理仍可用（端口有监听或无法判断），跳过代理守卫");
                        return (true, false);
                    }
                    reason += "（校外死代理）";
                }
            }

            var (ok, changed, _) = Disable(reason);
            return (ok, changed);
        }
        catch (Exception e)
        {
            Log.Debug("代理守卫失败（已忽略）: {0}", e.Message);
            return (false, false);
        }
    }

    public static List<(string Host, int Port)> Endpoints(string server)
    {
        var result = new List<(string, int)>();
        foreach (var raw in (server ?? "").Replace(" ", "").Split(';',
                     StringSplitOptions.RemoveEmptyEntries))
        {
            var part = raw;
            var eq = part.IndexOf('=');
            if (eq >= 0) part = part[(eq + 1)..];
            var scheme = part.IndexOf("://", StringComparison.Ordinal);
            if (scheme >= 0) part = part[(scheme + 3)..];
            part = part.TrimEnd('/');
            var idx = part.LastIndexOf(':');
            if (idx <= 0) continue;
            if (int.TryParse(part[(idx + 1)..], out var port))
                result.Add((part[..idx], port));
        }
        return result;
    }

    /// <summary>系统代理指向的**本机**端口是否有人监听：true/false/null（无法判断）。</summary>
    public static bool? LocalEndpointAlive(string server, int timeoutMs = 400)
    {
        var eps = Endpoints(server)
            .Where(e => e.Host.Equals("127.0.0.1", StringComparison.OrdinalIgnoreCase)
                        || e.Host.Equals("localhost", StringComparison.OrdinalIgnoreCase)
                        || e.Host == "::1")
            .ToList();
        if (eps.Count == 0) return null;
        foreach (var (host, port) in eps)
        {
            try
            {
                using var tcp = new TcpClient();
                if (tcp.ConnectAsync(host, port).Wait(timeoutMs))
                    return true;
            }
            catch
            {
                // 连接失败 = 该端口无人监听，继续看下一个端点
            }
        }
        return false;
    }

    private static bool ReadDword(string name)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(InternetSettingsKey);
            var v = key?.GetValue(name);
            if (v is int i) return i != 0;
            if (v is string s) return int.TryParse(s, out var n) && n != 0;
            return false;
        }
        catch
        {
            return false;
        }
    }

    private static string ReadString(string name)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(InternetSettingsKey);
            return key?.GetValue(name) as string ?? "";
        }
        catch
        {
            return "";
        }
    }

    private static bool WriteDword(string name, int value)
    {
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(InternetSettingsKey, writable: true);
            key.SetValue(name, value, RegistryValueKind.DWord);
            return true;
        }
        catch (Exception e)
        {
            Log.Debug("写注册表 {0} 失败: {1}", name, e.Message);
            return false;
        }
    }

    private static bool WriteString(string name, string value)
    {
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(InternetSettingsKey, writable: true);
            key.SetValue(name, value, RegistryValueKind.String);
            return true;
        }
        catch (Exception e)
        {
            Log.Debug("写注册表 {0} 失败: {1}", name, e.Message);
            return false;
        }
    }

    private static bool DeleteValue(string name)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(InternetSettingsKey, writable: true);
            key?.DeleteValue(name, throwOnMissingValue: false);
            return true;
        }
        catch (Exception e)
        {
            Log.Debug("删除注册表 {0} 失败: {1}", name, e.Message);
            return false;
        }
    }

    private static void NotifyChanged()
    {
        try
        {
            InternetSetOptionW(IntPtr.Zero, InternetOptionSettingsChanged, IntPtr.Zero, 0);
            InternetSetOptionW(IntPtr.Zero, InternetOptionRefresh, IntPtr.Zero, 0);
        }
        catch
        {
            // 失败只是延后生效，不影响正确性
        }
    }
}