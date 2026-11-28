using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>GitHub Release 更新检查 / 资产下载（与 Python 版语义一致）。</summary>
public static class UpdateCheck
{
    private const int SuccessTtl = 6 * 3600;
    private const int FailTtl = 300;

    private static readonly object Sync = new();
    private static JsonObject? _cache;
    private static long _cacheAt;

    private static readonly HttpClient Gh = new(new SocketsHttpHandler
    {
        AllowAutoRedirect = true,
        ConnectTimeout = TimeSpan.FromSeconds(5),
    })
    {
        Timeout = TimeSpan.FromSeconds(5),
    };

    private static readonly HttpClient Downloader = new(new SocketsHttpHandler
    {
        AllowAutoRedirect = true,
        ConnectTimeout = TimeSpan.FromSeconds(15),
    })
    {
        Timeout = TimeSpan.FromSeconds(30),
    };

    public static JsonObject Default() => new()
    {
        ["ok"] = false,
        ["current"] = AppInfo.Version,
        ["latest"] = "",
        ["available"] = false,
        ["url"] = AppInfo.GitHubReleasesUrl,
        ["name"] = "",
        ["published_at"] = "",
        ["checked_at"] = 0,
        ["error"] = "",
        ["asset_url"] = "",
        ["asset_size"] = 0,
        ["asset_sha256"] = "",
    };

    private static JsonObject Clone(JsonObject o) => (JsonObject)o.DeepClone();

    public static JsonObject Get()
    {
        lock (Sync)
        {
            if (_cache == null) SeedFromState();
            _cache ??= Default();
            var result = Clone(_cache);
            var latest = result.GetString("latest");
            result["available"] = latest.Length > 0 && AppInfo.IsNewer(latest, AppInfo.Version);
            return result;
        }
    }

    private static void SeedFromState()
    {
        if (State.Load()[AppInfo.UpdateStateKey] is not JsonObject rec) return;
        var data = Default();
        foreach (var kv in rec)
        {
            if (data.ContainsKey(kv.Key)) data[kv.Key] = kv.Value?.DeepClone();
        }
        data["current"] = AppInfo.Version;
        var latest = data.GetString("latest");
        data["available"] = latest.Length > 0 && AppInfo.IsNewer(latest, AppInfo.Version);
        _cache = data;
        _cacheAt = data.GetInt64("checked_at");
    }

    public static string NormalizeTag(string? tag)
    {
        var t = (tag ?? "").Trim();
        return t.StartsWith('v') || t.StartsWith('V') ? t[1..] : t;
    }

    public static bool AllowedPage(string url) =>
        url.StartsWith($"https://github.com/{AppInfo.GitHubRepo}/releases/", StringComparison.Ordinal);

    public static bool AllowedAsset(string url) =>
        url.StartsWith($"https://github.com/{AppInfo.GitHubRepo}/releases/download/",
            StringComparison.Ordinal);

    public static JsonObject CheckLatest(bool force = false, double timeout = 5)
    {
        lock (Sync)
        {
            if (_cache != null && !force)
            {
                var ttl = _cache.GetBool("ok") ? SuccessTtl : FailTtl;
                if (Utils.Now() - _cacheAt < ttl) return Clone(_cache);
            }
        }

        var info = Default();
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, AppInfo.GitHubApiLatest);
            req.Headers.TryAddWithoutValidation("User-Agent", "CampusNetAuth/" + AppInfo.Version);
            req.Headers.TryAddWithoutValidation("Accept", "application/vnd.github+json");
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeout));
            using var resp = Gh.Send(req, HttpCompletionOption.ResponseContentRead, cts.Token);
            var text = resp.Content.ReadAsStringAsync(cts.Token).GetAwaiter().GetResult();
            if (!resp.IsSuccessStatusCode) throw new Exception($"HTTP {(int)resp.StatusCode}");
            var payload = JsonNode.Parse(text) as JsonObject
                          ?? throw new Exception("GitHub API 返回格式异常");

            var latest = NormalizeTag(payload.GetString("tag_name"));
            if (AppInfo.VerKey(latest) == null)
                throw new Exception("无法解析 Release 版本号: " + payload.GetString("tag_name"));

            info["ok"] = true;
            info["latest"] = latest;
            info["available"] = AppInfo.IsNewer(latest, AppInfo.Version);
            info["name"] = payload.GetString("name");
            info["published_at"] = payload.GetString("published_at");

            var html = payload.GetString("html_url");
            if (AllowedPage(html)) info["url"] = html;

            if (payload["assets"] is JsonArray assets)
            {
                foreach (var node in assets)
                {
                    if (node is not JsonObject asset) continue;
                    if (asset.GetString("name") != AppInfo.AppName + ".exe") continue;
                    var dl = asset.GetString("browser_download_url");
                    if (!AllowedAsset(dl)) continue;
                    info["asset_url"] = dl;
                    info["asset_size"] = asset.GetInt64("size");
                    var digest = asset.GetString("digest").ToLowerInvariant();
                    if (digest.StartsWith("sha256:", StringComparison.Ordinal))
                        info["asset_sha256"] = digest[7..].Trim();
                    break;
                }
            }
            Log.Debug("Release 检查：当前 {0} / 最新 {1} / 可用更新={2}",
                AppInfo.Version, latest, info.GetBool("available"));
        }
        catch (Exception e)
        {
            info["error"] = $"{e.GetType().Name}: {e.Message}";
            Log.Debug("检查 GitHub Release 失败: {0}", e.Message);
        }

        info["checked_at"] = Utils.Now();
        try
        {
            State.Update(new JsonObject
            {
                [AppInfo.UpdateStateKey] = PersistShape(info),
            });
        }
        catch { }

        lock (Sync)
        {
            _cache = Clone(info);
            _cacheAt = Utils.Now();
        }
        return info;
    }

    private static JsonObject PersistShape(JsonObject info)
    {
        var rec = new JsonObject();
        foreach (var k in new[]
                 {
                     "ok", "latest", "available", "url", "name", "published_at",
                     "checked_at", "error", "asset_url", "asset_size", "asset_sha256",
                 })
        {
            rec[k] = info[k]?.DeepClone();
        }
        return rec;
    }

    public static (bool Due, JsonObject Info) CheckDue(long? now = null)
    {
        var info = Get();
        var checkedAt = info.GetInt64("checked_at");
        if (checkedAt <= 0) return (true, info);
        var interval = info.GetBool("ok") ? SuccessTtl : FailTtl;
        return ((now ?? Utils.Now()) - checkedAt >= interval, info);
    }

    /// <summary>下载更新资产并校验（大小/SHA256/PE 版本/ProductName），不执行文件。</summary>
    public static (string? Path, string Error) DownloadAsset(JsonObject info, string? dest = null)
    {
        var url = info.GetString("asset_url");
        if (url.Length == 0) return (null, "没有可用的下载地址（请先点「检查更新」重新获取）");
        if (!AllowedAsset(url)) return (null, "下载地址不是本仓库的 Release 资产，已拒绝");

        var expectSize = info.GetInt64("asset_size");
        var expectSha = info.GetString("asset_sha256").ToLowerInvariant();
        dest ??= Path.Combine(AppInfo.BaseDir, AppInfo.DownloadTmpName);
        var part = dest + ".part";

        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            req.Headers.TryAddWithoutValidation("User-Agent", "CampusNetAuth/" + AppInfo.Version);
            using var cts = new CancellationTokenSource(TimeSpan.FromMinutes(5));
            using var resp = Downloader.Send(req, HttpCompletionOption.ResponseHeadersRead, cts.Token);
            if (!resp.IsSuccessStatusCode) throw new Exception($"HTTP {(int)resp.StatusCode}");

            long got = 0;
            using var sha = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
            using (var src = resp.Content.ReadAsStream(cts.Token))
            using (var dst = File.Create(part))
            {
                var buffer = new byte[64 * 1024];
                while (true)
                {
                    var n = src.Read(buffer, 0, buffer.Length);
                    if (n <= 0) break;
                    dst.Write(buffer, 0, n);
                    sha.AppendData(buffer, 0, n);
                    got += n;
                    if (expectSize > 0 && got > expectSize)
                        throw new Exception($"下载内容超过声明大小（{expectSize} 字节）");
                }
                dst.Flush(true);
            }

            if (expectSize > 0 && got != expectSize)
                throw new Exception($"大小不符（收到 {got} / 应为 {expectSize} 字节）");
            if (expectSha.Length > 0)
            {
                var actual = Convert.ToHexString(sha.GetHashAndReset()).ToLowerInvariant();
                if (actual != expectSha) throw new Exception("SHA256 校验失败（下载可能损坏或被篡改）");
            }

            var info2 = Upgrade.PeVersion(part);
            if (info2?.Version == null) throw new Exception("下载的文件没有版本信息，可能不是有效的程序文件");
            if (info2.Value.Product != null && info2.Value.Product != AppInfo.AppName)
                throw new Exception($"该文件的 ProductName 是 {info2.Value.Product}，不是 {AppInfo.AppName}");
            if (!AppInfo.IsNewer(info2.Value.Version, AppInfo.Version))
                throw new Exception($"下载的版本 {info2.Value.Version} 不高于当前 {AppInfo.Version}");

            File.Move(part, dest, overwrite: true);
            Log.Info("更新包已下载并校验通过：v{0}（{1} 字节）", info2.Value.Version, got);
            return (dest, "");
        }
        catch (Exception e)
        {
            foreach (var p in new[] { part, dest })
            {
                try { if (File.Exists(p)) File.Delete(p); } catch { }
            }
            Log.Warn("下载更新失败: {0}", e.Message);
            return (null, e.Message);
        }
    }

    public static bool OpenReleasePage()
    {
        var info = Get();
        var url = info.GetString("url", AppInfo.GitHubReleasesUrl);
        if (url != AppInfo.GitHubReleasesUrl && !AllowedPage(url))
        {
            Log.Warn("拒绝打开非本仓库 Release 地址: {0}", url);
            return false;
        }
        try
        {
            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            return true;
        }
        catch (Exception e)
        {
            Log.Warn("打开 Release 页面失败: {0}", e.Message);
            return false;
        }
    }
}