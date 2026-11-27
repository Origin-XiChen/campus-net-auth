using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>状态事件（最近 8 条，同事件 60s 内合并）。</summary>
public static class EventLog
{
    public static void Record(string kind, string message)
    {
        try
        {
            State.Mutate(st =>
            {
                var events = st["events"] as JsonArray ?? new JsonArray();
                var now = Utils.Now();
                if (events.Count > 0 && events[^1] is JsonObject last
                    && last.GetString("kind") == kind && now - last.GetInt64("t") < 60)
                {
                    last["t"] = now;
                }
                else
                {
                    events.Add(new JsonObject
                    {
                        ["t"] = now,
                        ["kind"] = kind,
                        ["msg"] = message,
                    });
                }
                while (events.Count > 8) events.RemoveAt(0);
                st["events"] = events;
            });
        }
        catch (Exception e)
        {
            Log.Debug("记录事件失败: {0}", e.Message);
        }
    }
}

/// <summary>手动下线静默期：state.json 的 logout_pause_until（跨进程生效）。</summary>
public static class LogoutPause
{
    public static long Left()
    {
        try
        {
            var until = State.Load().GetInt64(AppInfo.LogoutPauseKey);
            return Math.Max(0, until - Utils.Now());
        }
        catch
        {
            return 0;
        }
    }

    public static long Set(int seconds)
    {
        if (seconds <= 0)
        {
            Clear();
            return 0;
        }
        var until = Utils.Now() + seconds;
        State.Update(new JsonObject { [AppInfo.LogoutPauseKey] = until });
        return until;
    }

    public static bool Clear()
    {
        if (Left() == 0)
        {
            // 已经过期也清掉残留值
            try
            {
                var st = State.Load();
                if (st[AppInfo.LogoutPauseKey] != null)
                {
                    State.Mutate(s => s.Remove(AppInfo.LogoutPauseKey));
                    return true;
                }
            }
            catch { }
            return false;
        }
        State.Mutate(s => s.Remove(AppInfo.LogoutPauseKey));
        Log.Info("已解除手动下线静默期");
        return true;
    }
}

/// <summary>跨进程累计计数（守护重启不归零；60s 写盘限流）。</summary>
public static class StatsTotals
{
    private static readonly object Sync = new();
    private static long _lastWrite;

    public static JsonObject Load()
    {
        try
        {
            return State.Load()["stats"] as JsonObject ?? new JsonObject();
        }
        catch
        {
            return new JsonObject();
        }
    }

    public static void Save(JsonObject totals, double minGap = 60)
    {
        var now = Utils.Now();
        lock (Sync)
        {
            if (minGap > 0 && now - _lastWrite < minGap) return;
            _lastWrite = now;
        }
        try
        {
            State.Update(new JsonObject { ["stats"] = totals.DeepClone() });
        }
        catch (Exception e)
        {
            Log.Debug("保存累计计数失败: {0}", e.Message);
        }
    }
}