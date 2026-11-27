using System.Net.Http;
using System.Text;

namespace CampusNetAuth;

/// <summary>portal HTTP 响应（对齐 Python HttpResponse：status=-1 表示请求异常）。</summary>
public sealed class HttpResponse
{
    public int Status { get; init; }
    public Dictionary<string, string> Headers { get; init; } = new(StringComparer.OrdinalIgnoreCase);
    public byte[] Body { get; init; } = Array.Empty<byte>();
    public string Url { get; init; } = string.Empty;

    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    public string Text()
    {
        try { return StrictUtf8.GetString(Body); }
        catch (DecoderFallbackException) { }
        foreach (var cp in new[] { 936, 54936 })
        {
            try { return Encoding.GetEncoding(cp).GetString(Body); }
            catch { }
        }
        return Encoding.UTF8.GetString(Body);
    }

    public string? Get(string name) =>
        Headers.TryGetValue(name, out var v) ? v : null;
}

/// <summary>极简 Cookie 罐：按 (host, port) 保存 name=value（单一门户够用）。</summary>
public sealed class CookieJar
{
    private readonly Dictionary<(string Host, int Port), Dictionary<string, string>> _store = new();

    public string Get(string host, int port)
    {
        if (!_store.TryGetValue((host, port), out var cookies) || cookies.Count == 0)
            return string.Empty;
        return string.Join("; ", cookies.Select(kv => kv.Key + "=" + kv.Value));
    }

    public void Update(string host, int port, IEnumerable<string> setCookies)
    {
        var key = (host, port);
        if (!_store.TryGetValue(key, out var cookies))
        {
            cookies = new Dictionary<string, string>(StringComparer.Ordinal);
            _store[key] = cookies;
        }
        foreach (var raw in setCookies)
        {
            var first = raw.Split(';')[0].Trim();
            var idx = first.IndexOf('=');
            if (idx <= 0) continue;
            var name = first[..idx].Trim();
            var value = first[(idx + 1)..].Trim();
            if (name.Length == 0) continue;
            cookies[name] = value;
        }
    }
}

/// <summary>
/// HTTP 客户端：不跟随重定向、不使用系统代理（认证探测必须绕过 WinINET 代理）、
/// 不自动带 Cookie（由 CookieJar 显式管理），与 Python http_request 语义一致。
/// </summary>
public static class Http
{
    public const string UserAgent =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

    private static readonly HttpClient Client = new(new SocketsHttpHandler
    {
        UseProxy = false,
        AllowAutoRedirect = false,
        UseCookies = false,
        ConnectTimeout = TimeSpan.FromSeconds(10),
        PooledConnectionLifetime = TimeSpan.FromMinutes(2),
    })
    {
        Timeout = System.Threading.Timeout.InfiniteTimeSpan,
    };

    public static HttpResponse Request(
        string method,
        string host,
        int port,
        string path,
        Dictionary<string, string>? headers = null,
        byte[]? body = null,
        double timeout = 8,
        CookieJar? cookieJar = null)
    {
        var url = $"http://{host}:{port}{path}";
        try
        {
            using var req = new HttpRequestMessage(new HttpMethod(method), url);
            req.Headers.TryAddWithoutValidation("User-Agent", UserAgent);
            req.Headers.TryAddWithoutValidation("Accept", "*/*");
            req.Headers.ConnectionClose = true;

            if (cookieJar != null)
            {
                var cookie = cookieJar.Get(host, port);
                if (!string.IsNullOrEmpty(cookie))
                    req.Headers.TryAddWithoutValidation("Cookie", cookie);
            }
            if (headers != null)
            {
                foreach (var kv in headers)
                    req.Headers.TryAddWithoutValidation(kv.Key, kv.Value);
            }
            if (body != null)
            {
                req.Content = new ByteArrayContent(body);
                req.Content.Headers.TryAddWithoutValidation(
                    "Content-Type", "application/x-www-form-urlencoded; charset=UTF-8");
                req.Content.Headers.ContentLength = body.Length;
            }

            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(timeout));
            using var resp = Client.Send(req, HttpCompletionOption.ResponseContentRead, cts.Token);
            var data = resp.Content.ReadAsByteArrayAsync(cts.Token).GetAwaiter().GetResult();

            var hdrs = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var h in resp.Headers) hdrs[h.Key] = string.Join(", ", h.Value);
            foreach (var h in resp.Content.Headers) hdrs[h.Key] = string.Join(", ", h.Value);

            if (cookieJar != null && resp.Headers.TryGetValues("Set-Cookie", out var setCookies))
                cookieJar.Update(host, port, setCookies);

            return new HttpResponse
            {
                Status = (int)resp.StatusCode,
                Headers = hdrs,
                Body = data,
                Url = url,
            };
        }
        catch (Exception e)
        {
            var text = e is OperationCanceledException ? "timeout" : e.Message;
            return new HttpResponse
            {
                Status = -1,
                Body = Encoding.UTF8.GetBytes(text),
                Url = url,
            };
        }
    }
}