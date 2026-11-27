using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>state.json：跨进程共享的运行状态（userIndex、events、缓存、统计等）。</summary>
public static class State
{
    public static JsonObject Load()
    {
        try
        {
            return File.Exists(AppInfo.StatePath)
                ? Json.LoadObject(AppInfo.StatePath)
                : new JsonObject();
        }
        catch (Exception e)
        {
            Log.Warn("state.json 读取失败（按空状态处理）: {0}", e.Message);
            return new JsonObject();
        }
    }

    public static void Save(JsonObject state)
    {
        try
        {
            Json.SaveAtomic(AppInfo.StatePath, state);
        }
        catch (Exception e)
        {
            Log.Error("保存 state.json 失败: {0}", e.Message);
        }
    }

    /// <summary>合并式更新：保留其它字段（与 Python update_state 一致）。</summary>
    public static void Update(JsonObject patch, params string[] drop)
    {
        using var _ = new CrossProcessLock(AppInfo.StatePath + ".lock");
        var st = Load();
        foreach (var k in drop) st.Remove(k);
        foreach (var kv in patch) st[kv.Key] = kv.Value?.DeepClone();
        Save(st);
    }

    /// <summary>带锁的读-改-写。</summary>
    public static void Mutate(Action<JsonObject> mutate)
    {
        using var _ = new CrossProcessLock(AppInfo.StatePath + ".lock");
        var st = Load();
        mutate(st);
        Save(st);
    }

    public static string GetString(JsonObject st, string key, string def = "") =>
        st.GetString(key, def);
}

/// <summary>
/// 跨进程文件锁（O_CREAT|O_EXCL 语义）。超时/失败降级为"不加锁"，
/// 防止锁文件残留把写入永久卡死（与 Python 的 fail-open 一致）。
/// </summary>
public sealed class CrossProcessLock : IDisposable
{
    private FileStream? _fs;
    private readonly string _path;

    public CrossProcessLock(string path, TimeSpan? timeout = null)
    {
        _path = path;
        var deadline = DateTime.UtcNow + (timeout ?? TimeSpan.FromSeconds(3));
        while (true)
        {
            try
            {
                _fs = new FileStream(_path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
                return;
            }
            catch (IOException)
            {
                if (DateTime.UtcNow >= deadline) return; // fail-open
                Thread.Sleep(50);
            }
            catch
            {
                return; // 权限/路径异常：不加锁
            }
        }
    }

    public void Dispose()
    {
        try { _fs?.Dispose(); } catch { }
        _fs = null;
        try { File.Delete(_path); } catch { }
    }
}