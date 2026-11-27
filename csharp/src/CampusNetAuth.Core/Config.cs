using System.Text.Json.Nodes;

namespace CampusNetAuth;

/// <summary>config.json 的默认值、归一与读写（与 Python 版 _sanitize_config 语义一致）。</summary>
public static class Config
{
    private static readonly string[] IntKeys =
    {
        "portal_port", "interval", "retry_interval", "max_interval",
        "offline_interval", "timeout", "probe_timeout",
        "keepalive_interval", "verify_interval", "recheck_interval",
        "logout_pause",
    };

    public static JsonObject Defaults()
    {
        return new JsonObject
        {
            ["portal_host"] = "172.16.54.18",
            ["portal_port"] = 80,
            ["portal_base"] = "/eportal",
            ["username"] = "",
            ["service"] = "default",
            ["interval"] = 30,
            ["retry_interval"] = 30,
            ["max_interval"] = 600,
            ["offline_interval"] = 60,
            ["timeout"] = 8,
            ["probe_timeout"] = 3,
            ["keepalive_interval"] = 300,
            ["verify_interval"] = 5,
            ["recheck_interval"] = 30,
            ["logout_pause"] = 600,
            ["detect_targets"] = new JsonArray
            {
                new JsonObject
                {
                    ["host"] = "www.msftconnecttest.com",
                    ["path"] = "/connecttest.txt",
                    ["expect"] = "Microsoft Connect Test",
                },
                new JsonObject
                {
                    ["host"] = "captive.apple.com",
                    ["path"] = "/hotspot-detect.html",
                    ["expect"] = "Success",
                },
                new JsonObject
                {
                    ["host"] = "www.baidu.com",
                    ["path"] = "/",
                    ["expect"] = "",
                },
            },
            ["login_page_hints"] = new JsonObject
            {
                ["wlanacname"] = "logic",
                ["nasip"] = "10.253.0.17",
                ["url"] = "",
            },
            ["pin_pubkey"] = true,
            ["proxy_guard"] = "auto",
            ["log_level"] = "INFO",
        };
    }

    public static JsonObject Load()
    {
        var cfg = Defaults();
        if (File.Exists(AppInfo.ConfigPath))
        {
            try
            {
                var user = Json.TryLoadObject(AppInfo.ConfigPath);
                foreach (var kv in user)
                {
                    cfg[kv.Key] = kv.Value?.DeepClone();
                }
            }
            catch (Exception e)
            {
                Log.Warn("配置文件读取失败，使用默认配置: {0}", e.Message);
            }
        }
        return Sanitize(cfg);
    }

    public static void Save(JsonObject cfg)
    {
        try
        {
            Json.SaveAtomic(AppInfo.ConfigPath, cfg);
        }
        catch (Exception e)
        {
            Log.Error("保存配置失败: {0}", e.Message);
        }
    }

    private static int ParseInt(JsonNode? node, int def)
    {
        if (node == null) return def;
        try
        {
            var raw = node.ToJsonString().Trim('"');
            return int.TryParse(raw, out var v) ? v : def;
        }
        catch
        {
            return def;
        }
    }

    /// <summary>坏值回退默认或钳到合法区间，避免守护循环被非法配置拖死。</summary>
    public static JsonObject Sanitize(JsonObject cfg)
    {
        var def = Defaults();
        foreach (var k in IntKeys)
        {
            cfg[k] = ParseInt(cfg[k], def.GetInt(k));
        }

        cfg["interval"] = Math.Max(5, cfg.GetInt("interval", 30));
        cfg["retry_interval"] = Math.Max(5, cfg.GetInt("retry_interval", 30));
        cfg["max_interval"] = Math.Max(60, cfg.GetInt("max_interval", 600));
        cfg["offline_interval"] = Math.Max(5, cfg.GetInt("offline_interval", 60));
        cfg["timeout"] = Math.Clamp(cfg.GetInt("timeout", 8), 2, 60);
        cfg["probe_timeout"] = Math.Clamp(cfg.GetInt("probe_timeout", 3), 1, 30);
        cfg["keepalive_interval"] = Math.Max(30, cfg.GetInt("keepalive_interval", 300));
        cfg["verify_interval"] = Math.Max(2, cfg.GetInt("verify_interval", 5));
        cfg["recheck_interval"] = Math.Max(5, cfg.GetInt("recheck_interval", 30));
        cfg["logout_pause"] = Math.Clamp(cfg.GetInt("logout_pause", 600), 0, 86400);

        var targets = cfg["detect_targets"] as JsonArray;
        var cleaned = new JsonArray();
        if (targets != null)
        {
            foreach (var t in targets)
            {
                if (t is JsonObject obj && !string.IsNullOrWhiteSpace(obj.GetString("host")))
                {
                    cleaned.Add(obj.DeepClone());
                }
            }
        }
        cfg["detect_targets"] = cleaned.Count > 0
            ? cleaned
            : def["detect_targets"]!.DeepClone();

        if (string.IsNullOrEmpty(cfg.GetString("portal_host")))
        {
            cfg["portal_host"] = def.GetString("portal_host");
        }

        cfg["username"] = cfg.GetString("username").Trim();

        var svc = cfg.GetString("service").Trim();
        var lower = svc.ToLowerInvariant();
        if (lower is "default" or "auto")
        {
            cfg["service"] = lower;
        }
        else if (svc.ToUpperInvariant() is "DX" or "LT" or "YD")
        {
            cfg["service"] = svc.ToUpperInvariant();
        }
        else
        {
            if (svc.Length > 0)
            {
                Log.Warn("配置里的 service={0} 不是合法值，已回落 default（可选：default / DX / LT / YD / auto）", svc);
            }
            cfg["service"] = "default";
        }

        var basePath = cfg.GetString("portal_base").Trim();
        if (!basePath.StartsWith('/')) basePath = "/" + basePath;
        cfg["portal_base"] = basePath.TrimEnd('/') is { Length: > 0 } b ? b : "/eportal";

        var hints = cfg["login_page_hints"] as JsonObject;
        if (hints == null || hints.Count == 0)
        {
            cfg["login_page_hints"] = new JsonObject();
        }
        else
        {
            var merged = (JsonObject)Defaults()["login_page_hints"]!.DeepClone();
            foreach (var kv in hints)
            {
                var v = kv.Value;
                if (v is JsonValue jv)
                {
                    try
                    {
                        var kind = jv.GetValueKind();
                        if (kind is System.Text.Json.JsonValueKind.String
                            or System.Text.Json.JsonValueKind.Number)
                        {
                            merged[kv.Key] = v.DeepClone();
                        }
                    }
                    catch { }
                }
            }
            var result = new JsonObject();
            foreach (var k in new[] { "wlanacname", "nasip", "wlanuserip", "wlanparameter", "url" })
            {
                result[k] = merged.GetString(k).Trim();
            }
            cfg["login_page_hints"] = result;
        }

        var pin = cfg["pin_pubkey"];
        cfg["pin_pubkey"] = pin is JsonValue pv2 && pv2.GetValueKind() is System.Text.Json.JsonValueKind.True
            or System.Text.Json.JsonValueKind.False
            ? pin!.DeepClone()
            : true;

        var pg = cfg.GetString("proxy_guard", "auto").Trim().ToLowerInvariant();
        cfg["proxy_guard"] = pg is "auto" or "off" ? pg : "auto";

        return cfg;
    }
}