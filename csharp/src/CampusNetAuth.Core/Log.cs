using System.Text;

namespace CampusNetAuth;

public enum LogLevel
{
    Debug = 0,
    Info = 1,
    Warning = 2,
    Error = 3,
}

/// <summary>文件日志（UTF-8，2MB 轮转一次，最多保留 3 个历史文件）。</summary>
public static class Log
{
    private static readonly object Sync = new();
    private static readonly UTF8Encoding Utf8NoBom = new(false);
    private const long MaxBytes = 2 * 1024 * 1024;

    private static LogLevel _level = LogLevel.Info;

    /// <summary>是否同时写到控制台（守护/CLI 前台运行时由调用方打开）。</summary>
    public static bool ToConsole { get; set; }

    public static void SetLevel(string? name)
    {
        if (Enum.TryParse<LogLevel>(name, ignoreCase: true, out var lvl)) _level = lvl;
    }

    public static void Debug(string message) => Write(LogLevel.Debug, message, null);
    public static void Info(string message) => Write(LogLevel.Info, message, null);
    public static void Warn(string message) => Write(LogLevel.Warning, message, null);
    public static void Error(string message) => Write(LogLevel.Error, message, null);

    public static void Info(string format, params object?[] args) =>
        Write(LogLevel.Info, string.Format(format, args), null);

    public static void Warn(string format, params object?[] args) =>
        Write(LogLevel.Warning, string.Format(format, args), null);

    public static void Error(string format, params object?[] args) =>
        Write(LogLevel.Error, string.Format(format, args), null);

    public static void Debug(string format, params object?[] args) =>
        Write(LogLevel.Debug, string.Format(format, args), null);

    private static void Write(LogLevel lvl, string message, Exception? ex)
    {
        if (lvl < _level) return;
        var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} [{lvl.ToString().ToUpperInvariant()}] {message}";
        if (ex != null) line += Environment.NewLine + ex;
        lock (Sync)
        {
            try
            {
                RotateIfNeeded();
                File.AppendAllText(AppInfo.LogPath, line + Environment.NewLine, Utf8NoBom);
            }
            catch
            {
                // 日志失败绝不拖垮主流程
            }
            if (ToConsole)
            {
                try { Console.Error.WriteLine(line); } catch { }
            }
        }
    }

    private static void RotateIfNeeded()
    {
        try
        {
            var fi = new FileInfo(AppInfo.LogPath);
            if (!fi.Exists || fi.Length < MaxBytes) return;
            var first = AppInfo.LogPath + ".1";
            if (File.Exists(first)) File.Delete(first);
            File.Move(AppInfo.LogPath, first);
        }
        catch
        {
            // 轮转失败继续写主文件
        }
    }
}