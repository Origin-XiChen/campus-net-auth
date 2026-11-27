using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>JSON 读写：state/config 都是"动态字段"的字典语义，用 JsonObject 最接近 Python dict。</summary>
public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        AllowTrailingCommas = true,
        ReadCommentHandling = JsonCommentHandling.Skip,
    };

    private static readonly UTF8Encoding Utf8NoBom = new(false);

    public static JsonObject LoadObject(string path)
    {
        using var fs = File.OpenRead(path);
        var node = JsonNode.Parse(fs, documentOptions: new JsonDocumentOptions
        {
            AllowTrailingCommas = true,
            CommentHandling = JsonCommentHandling.Skip,
        });
        return node as JsonObject ?? new JsonObject();
    }

    public static JsonObject TryLoadObject(string path)
    {
        try
        {
            return File.Exists(path) ? LoadObject(path) : new JsonObject();
        }
        catch
        {
            return new JsonObject();
        }
    }

    /// <summary>原子写：临时文件 + 覆盖换位，避免截断。</summary>
    public static void SaveAtomic(string path, JsonNode node)
    {
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, node.ToJsonString(Options), Utf8NoBom);
        File.Move(tmp, path, overwrite: true);
    }

    /// <summary>等价于 json.loads(json.dumps(x)) 的深拷贝。</summary>
    public static JsonNode? Clone(JsonNode? node) =>
        node == null ? null : JsonNode.Parse(node.ToJsonString());

    public static string GetString(this JsonObject o, string key, string def = "")
    {
        var n = o[key];
        if (n == null) return def;
        try { return n.GetValue<string>(); }
        catch
        {
            try { return n.ToJsonString().Trim('"'); }
            catch { return def; }
        }
    }

    public static int GetInt(this JsonObject o, string key, int def = 0)
    {
        var n = o[key];
        if (n == null) return def;
        try
        {
            var raw = n.ToJsonString();
            return int.TryParse(raw, out var v) ? v : def;
        }
        catch { return def; }
    }

    public static bool GetBool(this JsonObject o, string key, bool def = false)
    {
        var n = o[key];
        if (n == null) return def;
        try
        {
            var raw = n.GetValueKind();
            if (raw == JsonValueKind.True) return true;
            if (raw == JsonValueKind.False) return false;
        }
        catch { }
        return def;
    }

    public static JsonObject? GetObject(this JsonObject o, string key) => o[key] as JsonObject;

    public static void Set(this JsonObject o, string key, JsonNode? value)
    {
        if (value == null) o.Remove(key);
        else o[key] = value;
    }
}