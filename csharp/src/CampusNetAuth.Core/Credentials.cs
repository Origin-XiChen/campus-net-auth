using System.Security.Cryptography;
using System.Text;

namespace CampusNetAuth;

/// <summary>
/// 凭据：Windows DPAPI（当前用户）加密存 cred.bin。
/// 文件格式与 Python 版一致："DPAPI\x01" 头 + DPAPI blob（首次以 0.7.0 C# 裸 blob
/// 写入的文件也能读，避免升级后自己打不开）。
/// </summary>
public static class CredentialStore
{
    private static readonly byte[] DpapiMagic = Encoding.ASCII.GetBytes("DPAPI\u0001");
    private static readonly byte[] PlainMagic = Encoding.ASCII.GetBytes("PLAIN\u0001");

    public static string? Load()
    {
        try
        {
            if (!File.Exists(AppInfo.CredPath)) return null;
            var blob = File.ReadAllBytes(AppInfo.CredPath);
            if (HasPrefix(blob, DpapiMagic))
            {
                var raw = ProtectedData.Unprotect(
                    blob[DpapiMagic.Length..], null, DataProtectionScope.CurrentUser);
                return Encoding.UTF8.GetString(raw);
            }
            if (HasPrefix(blob, PlainMagic))
            {
                // Python 非 Windows 的退化格式（实际不会出现在 Windows 部署里）
                var text = Encoding.ASCII.GetString(blob[PlainMagic.Length..]);
                return Encoding.UTF8.GetString(Convert.FromBase64String(text));
            }
            // 兼容 0.7.0 早期 C# 写入的无标记裸 DPAPI
            var legacy = ProtectedData.Unprotect(blob, null, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(legacy);
        }
        catch (Exception e)
        {
            Log.Warn("凭据解密失败: {0}", e.Message);
            return null;
        }
    }

    public static bool Save(string password)
    {
        try
        {
            var enc = ProtectedData.Protect(
                Encoding.UTF8.GetBytes(password), null, DataProtectionScope.CurrentUser);
            var payload = new byte[DpapiMagic.Length + enc.Length];
            Buffer.BlockCopy(DpapiMagic, 0, payload, 0, DpapiMagic.Length);
            Buffer.BlockCopy(enc, 0, payload, DpapiMagic.Length, enc.Length);
            File.WriteAllBytes(AppInfo.CredPath, payload);
            return true;
        }
        catch (Exception e)
        {
            Log.Error("凭据加密保存失败: {0}", e.Message);
            return false;
        }
    }

    public static bool Exists => File.Exists(AppInfo.CredPath);

    private static bool HasPrefix(byte[] data, byte[] prefix) =>
        data.Length >= prefix.Length && data.AsSpan(0, prefix.Length).SequenceEqual(prefix);
}