namespace CampusNetAuth;

/// <summary>
/// 系统通知（右下角）。当前里程碑先记录日志并返回成功；
/// 托盘/气泡通知将在 UI（WebView2 + NotifyIcon）里程碑接入。
/// </summary>
public static class Notify
{
    private static readonly object Sync = new();
    private static readonly Dictionary<string, long> LastSent = new();

    public static bool System(string title, string message, int timeout = 10, bool dedup = true)
    {
        try
        {
            if (dedup)
            {
                var key = title + "\u0000" + message;
                var now = Utils.Now();
                lock (Sync)
                {
                    if (LastSent.TryGetValue(key, out var last) && now - last < 60) return false;
                    LastSent[key] = now;
                    if (LastSent.Count > 32)
                    {
                        foreach (var k in LastSent.Keys.Take(16).ToList()) LastSent.Remove(k);
                    }
                }
            }
            Log.Info("通知：{0} - {1}", title, message);
            return true;
        }
        catch (Exception e)
        {
            Log.Debug("系统通知发送失败（已忽略）: {0}", e.Message);
            return false;
        }
    }
}