using CampusNetAuth;

namespace CampusNetAuth.Tests;

/// <summary>最小测试框架：离线、零第三方依赖（与项目"自检"风格一致）。</summary>
public sealed class AssertionException : Exception
{
    public AssertionException(string message) : base(message) { }
}

public static class Assert
{
    public static void Equal<T>(T expected, T actual, string? what = null)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
            throw new AssertionException($"{what ?? "值"}不相等\n  期望: {expected}\n  实际: {actual}");
    }

    public static void True(bool cond, string? what = null)
    {
        if (!cond) throw new AssertionException(what ?? "期望为 true");
    }

    public static void False(bool cond, string? what = null)
    {
        if (cond) throw new AssertionException(what ?? "期望为 false");
    }

    public static void Contains(string needle, string haystack, string? what = null)
    {
        if (!haystack.Contains(needle, StringComparison.Ordinal))
            throw new AssertionException($"{what ?? "字符串"}不包含 {needle}");
    }

    public static void DoesNotContain(string needle, string haystack, string? what = null)
    {
        if (haystack.Contains(needle, StringComparison.Ordinal))
            throw new AssertionException($"{what ?? "字符串"}不应包含 {needle}");
    }

    public static void IsNull(object? value, string? what = null)
    {
        if (value != null) throw new AssertionException($"{what ?? "值"}应为 null，实际 {value}");
    }
}

public static class Program
{
    private static int _passed;
    private static readonly List<string> Failures = new();

    private static void Run(string name, Action test)
    {
        try
        {
            test();
            _passed++;
            Console.WriteLine($"[PASS] {name}");
        }
        catch (Exception e)
        {
            Failures.Add(name + ": " + e.Message);
            Console.WriteLine($"[FAIL] {name}\n       {e.Message.Replace("\n", "\n       ")}");
        }
    }

    /// <summary>手动探针：确认真实系统气泡能弹（含守护进程那条临时图标通道），默认测试不触发。</summary>
    private static int ManualProbe(string[] args)
    {
        if (args.Length >= 2 && args[0] == "--pe")
        {
            var path = args[1];
            var info = Upgrade.PeVersion(path);
            var dir = Path.GetDirectoryName(Path.GetFullPath(path)) ?? "";
            Console.WriteLine($"exists={File.Exists(path)} name={Path.GetFileName(path)}");
            Console.WriteLine($"version={info?.Version} product={info?.Product}");
            Console.WriteLine($"newerThanCurrent={AppInfo.IsNewer(AppInfo.Version, info?.Version)}");
            Console.WriteLine($"unsafeDir={LegacyCleanup.IsUnsafeDir(dir)} dir={dir}");
            foreach (var m in new[]
                     {
                         "CampusNetAuthUninstall.exe", "CampusNetAuthDaemon.vbs",
                         "CampusNetAuthUI.vbs", "state.json", "config.json",
                         "campusnet.log", "daemon.pid",
                     })
            {
                Console.WriteLine($"marker {m}={File.Exists(Path.Combine(dir, m))}");
            }
            return 0;
        }
        if (!args.Contains("--notify")) return -1;
        var ok = Notify.System("校园网无感认证", "通知通道自检（临时图标通道）", 5, dedup: false);
        Console.WriteLine(ok ? "notify dispatched" : "notify failed");
        Thread.Sleep(1500);
        return ok ? 0 : 1;
    }

    private static void Main(string[] args)
    {
        // 自检不碰真实配置/状态：数据目录指向构建输出目录下的临时目录
        // （受限沙箱里工作区内的 exe 不能往 %TEMP% 写；bin 已在 .gitignore 中）
        var dataDir = Path.Combine(AppContext.BaseDirectory, "selftest_data");
        try
        {
            if (Directory.Exists(dataDir)) Directory.Delete(dataDir, true);
        }
        catch
        {
        }
        Directory.CreateDirectory(dataDir);
        Environment.SetEnvironmentVariable("CNA_DATA_DIR", dataDir);
        try
        {
            RunAll(args);
        }
        finally
        {
            try
            {
                if (Directory.Exists(dataDir)) Directory.Delete(dataDir, true);
            }
            catch
            {
                // 临时目录清理失败不影响测试结论
            }
        }
    }

    private static void RunAll(string[] args)
    {
        var probe = ManualProbe(args);
        if (probe >= 0)
        {
            Environment.ExitCode = probe;
            return;
        }

        Run("RSA 黄金向量", RsaGoldenTests.GoldenVectors);
        Run("RSA 分块结构", RsaGoldenTests.BlockStructure);
        Run("反转按 UTF-16 code unit", RsaGoldenTests.ReverseUnitsMatchesJs);
        Run("双重 URL 编码", RsaGoldenTests.DoubleUrlEncode);
        Run("版本比较", VersionTests.ComparesNumerically);
        Run("版本比较（预发布后缀）", VersionTests.PrereleaseSuffix);
        Run("PE 版本/产品名读取", VersionTests.PeProductNameNotTruncated);
        Run("配置归一（坏值回落）", ConfigTests.SanitizeFallsBack);
        Run("配置归一（hints）", ConfigTests.SanitizeHints);
        Run("配置归一（边界钳制）", ConfigTests.SanitizeClamps);
        Run("密码明文中 mac 缺省", CryptoExtraTests.LoginPlainDefaultMac);
        Run("登录页判定（学生页/管理台）", HtmlTests.LooksLikeLoginPage);
        Run("input 解析（id 优先、name 回退）", HtmlTests.ParseInputs);
        Run("表单编码（空格/特殊字符）", HtmlTests.FormEncoding);
        Run("工具函数（掩码/时长）", UtilsTests.MaskAndUptime);
        Run("手动下线静默期（落盘/清除）", StateTests.LogoutPauseRoundTrip);
        Run("事件记录（合并/上限）", StateTests.EventLogMergeAndTrim);
        Run("旧版清理安全阀", LegacyCleanupTests.SafetyValve);
        Run("旧版清理路径归属", LegacyCleanupTests.PathUnder);
        Run("守护手动停止标记", DaemonGuardTests.ManualStopMarker);
        Run("凭据格式（Python 头/旧裸格式）", CredentialStoreTests.PythonHeaderAndLegacyRaw);

        Console.WriteLine();
        Console.WriteLine(Failures.Count == 0
            ? $"===== 全部通过（{_passed} 项）====="
            : $"===== 失败 {Failures.Count} 项 / 通过 {_passed} 项 =====");
        Environment.ExitCode = Failures.Count == 0 ? 0 : 1;
    }
}