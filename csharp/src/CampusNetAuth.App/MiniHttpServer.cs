using System.Net;
using System.Net.Sockets;
using System.Text;

namespace CampusNetAuth.App;

public sealed class MiniHttpRequest
{
    public string Method { get; init; } = "GET";
    public string Path { get; init; } = "/";
    public string Query { get; init; } = "";
    public Dictionary<string, string> Headers { get; init; } = new(StringComparer.OrdinalIgnoreCase);
    public byte[] Body { get; init; } = Array.Empty<byte>();

    public string? QueryValue(string name)
    {
        foreach (var part in Query.Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var idx = part.IndexOf('=');
            var k = idx >= 0 ? part[..idx] : part;
            if (!k.Equals(name, StringComparison.Ordinal)) continue;
            var v = idx >= 0 ? part[(idx + 1)..] : "";
            return Uri.UnescapeDataString(v.Replace("+", " "));
        }
        return null;
    }
}

public readonly record struct MiniHttpResponse(int Status, string ContentType, byte[] Body);

/// <summary>
/// 极简 HTTP/1.1 服务（TcpListener 自建，不用 HTTP.sys）：
/// 与本项目 Python 版的 ThreadingHTTPServer 思路一致，沙箱/受限环境也能跑。
/// </summary>
public static class MiniHttp
{
    public static async Task ServeAsync(TcpListener listener,
        Func<MiniHttpRequest, MiniHttpResponse> handler, CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            TcpClient client;
            try
            {
                client = await listener.AcceptTcpClientAsync(token);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch
            {
                if (token.IsCancellationRequested) return;
                continue;
            }
            _ = Task.Run(() => HandleClient(client, handler));
        }
    }

    private static void HandleClient(TcpClient client, Func<MiniHttpRequest, MiniHttpResponse> handler)
    {
        try
        {
            using (client)
            {
                client.ReceiveTimeout = 15000;
                var stream = client.GetStream();

                var headerBytes = new List<byte>(1024);
                var one = new byte[1];
                while (headerBytes.Count < 64 * 1024)
                {
                    var n = stream.Read(one, 0, 1);
                    if (n <= 0) return;
                    headerBytes.Add(one[0]);
                    var count = headerBytes.Count;
                    if (count >= 4
                        && headerBytes[count - 4] == (byte)'\r' && headerBytes[count - 3] == (byte)'\n'
                        && headerBytes[count - 2] == (byte)'\r' && headerBytes[count - 1] == (byte)'\n')
                        break;
                }
                if (headerBytes.Count == 0) return;

                var headerText = Encoding.UTF8.GetString(headerBytes.ToArray());
                var lines = headerText.Split("\r\n", StringSplitOptions.RemoveEmptyEntries);
                if (lines.Length == 0) return;
                var requestLine = lines[0].Split(' ');
                if (requestLine.Length < 2) return;

                var method = requestLine[0].ToUpperInvariant();
                var target = requestLine[1];
                var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                for (var i = 1; i < lines.Length; i++)
                {
                    var idx = lines[i].IndexOf(':');
                    if (idx <= 0) continue;
                    headers[lines[i][..idx].Trim()] = lines[i][(idx + 1)..].Trim();
                }

                var path = target;
                var query = "";
                var qIdx = target.IndexOf('?');
                if (qIdx >= 0)
                {
                    path = target[..qIdx];
                    query = target[(qIdx + 1)..];
                }

                var body = Array.Empty<byte>();
                if (headers.TryGetValue("Content-Length", out var lenText)
                    && int.TryParse(lenText, out var len) && len > 0 && len <= 8 * 1024 * 1024)
                {
                    body = new byte[len];
                    var read = 0;
                    while (read < len)
                    {
                        var n = stream.Read(body, read, len - read);
                        if (n <= 0) break;
                        read += n;
                    }
                    if (read != len) body = body[..read];
                }

                var request = new MiniHttpRequest
                {
                    Method = method,
                    Path = path,
                    Query = query,
                    Headers = headers,
                    Body = body,
                };

                MiniHttpResponse response;
                try
                {
                    response = handler(request);
                }
                catch (Exception e)
                {
                    response = new MiniHttpResponse(500, "application/json; charset=utf-8",
                        Encoding.UTF8.GetBytes($"{{\"ok\":false,\"error\":\"{e.Message.Replace("\"", "'")}\"}}"));
                }

                var reason = response.Status switch
                {
                    200 => "OK",
                    403 => "Forbidden",
                    404 => "Not Found",
                    500 => "Internal Server Error",
                    _ => "OK",
                };
                var head = Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 {response.Status} {reason}\r\n" +
                    $"Content-Type: {response.ContentType}\r\n" +
                    $"Content-Length: {response.Body.Length}\r\n" +
                    "Cache-Control: no-store\r\n" +
                    "Connection: close\r\n\r\n");
                stream.Write(head, 0, head.Length);
                stream.Write(response.Body, 0, response.Body.Length);
                stream.Flush();
            }
        }
        catch
        {
            // 单个连接异常不影响服务
        }
    }

    public static MiniHttpResponse Text(int status, string contentType, string text) =>
        new(status, contentType, Encoding.UTF8.GetBytes(text));
}