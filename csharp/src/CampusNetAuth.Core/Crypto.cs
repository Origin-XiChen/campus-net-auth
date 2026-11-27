using System.Globalization;
using System.Numerics;

namespace CampusNetAuth;

/// <summary>ePortal 协议里的加密/编码原语（逐字节复刻 Python 版实现）。</summary>
public static class Crypto
{
    /// <summary>JS: s.split("").reverse().join("") —— 按 UTF-16 code unit 反转。</summary>
    public static string ReverseUnits(string s)
    {
        var arr = s.ToCharArray();
        Array.Reverse(arr);
        return new string(arr);
    }

    /// <summary>双重 URL 编码，等价于 Python 的 quote(quote(x, safe=""), safe="")。</summary>
    public static string Dq(string? s) =>
        Uri.EscapeDataString(Uri.EscapeDataString(s ?? string.Empty));

    private static BigInteger ParseHex(string hex)
    {
        var h = (hex ?? string.Empty).Trim();
        if (h.Length == 0) throw new ArgumentException("RSA 参数为空");
        return BigInteger.Parse("0" + h, NumberStyles.HexNumber, CultureInfo.InvariantCulture);
    }

    public static int BitLength(BigInteger v)
    {
        if (v.Sign <= 0) return 0;
        var bytes = v.ToByteArray(isUnsigned: true, isBigEndian: true);
        if (bytes.Length == 0) return 0;
        var msb = bytes[0];
        var bits = 0;
        while (msb > 0)
        {
            bits++;
            msb >>= 1;
        }
        return (bytes.Length - 1) * 8 + bits;
    }

    /// <summary>
    /// 复刻 ePortal security.js：UTF-16 code unit → 末尾补 0 对齐 chunkSize →
    /// 16-bit 小端分块 → 裸 RSA（无填充）→ 每块输出 n*4 位小写 hex，块间空格。
    /// </summary>
    public static string RsaEncrypt(string plain, string exponentHex, string modulusHex)
    {
        var e = ParseHex(exponentHex);
        var m = ParseHex(modulusHex);
        if (m.IsZero) throw new ArgumentException("RSA modulus 为空");

        var highIndex = Math.Max(0, (BitLength(m) + 15) / 16 - 1);
        var chunkSize = 2 * highIndex;
        if (chunkSize <= 0) throw new ArgumentException("modulus 异常，无法计算 chunkSize");

        var data = new List<int>(plain.Length);
        foreach (var ch in plain) data.Add(ch);
        while (data.Count % chunkSize != 0) data.Add(0);

        var blocks = new List<string>();
        for (var i = 0; i < data.Count; i += chunkSize)
        {
            var block = BigInteger.Zero;
            for (var j = 0; j < chunkSize / 2; j++)
            {
                long digit = data[i + 2 * j] + (data[i + 2 * j + 1] << 8);
                block += (BigInteger)digit << (16 * j);
            }
            var crypt = BigInteger.ModPow(block, e, m);
            var digits = Math.Max(1, (BitLength(crypt) + 15) / 16);
            // BigInteger 的 "x" 格式对最高位为 1 的正数会补一个前导 0（符号位），
            // 这里先去掉再按位数左侧补零，才能与 Python format(...) 逐字节一致
            var hex = crypt.ToString("x").TrimStart('0');
            if (hex.Length == 0) hex = "0";
            blocks.Add(hex.PadLeft(digits * 4, '0'));
        }
        return string.Join(" ", blocks);
    }

    /// <summary>登录密码明文：密码 + ">" + mac（mac 缺省按门户口径用 111111111）。</summary>
    public static string LoginPlain(string password, string mac) =>
        ReverseUnits(password + ">" + (string.IsNullOrEmpty(mac) ? "111111111" : mac));
}