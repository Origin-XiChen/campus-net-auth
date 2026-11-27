using System.Security.Cryptography;
using System.Text;

namespace CampusNetAuth;

/// <summary>凭据：Windows DPAPI（当前用户）加密存 cred.bin。</summary>
public static class CredentialStore
{
    public static string? Load()
    {
        try
        {
            if (!File.Exists(AppInfo.CredPath)) return null;
            var enc = File.ReadAllBytes(AppInfo.CredPath);
            var raw = ProtectedData.Unprotect(enc, null, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(raw);
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
            File.WriteAllBytes(AppInfo.CredPath, enc);
            return true;
        }
        catch (Exception e)
        {
            Log.Error("凭据加密保存失败: {0}", e.Message);
            return false;
        }
    }

    public static bool Exists => File.Exists(AppInfo.CredPath);
}