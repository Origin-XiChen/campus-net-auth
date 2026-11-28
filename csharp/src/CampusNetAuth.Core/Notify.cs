using System.Runtime.InteropServices;

namespace CampusNetAuth;

public enum NotifyKind
{
    Success,
    Info,
    Warning,
    Error,
}

/// <summary>自定义右下角横幅/错误卡（由 App 层的 NotificationUi 绘制）。</summary>
public sealed class NotifyRequest
{
    public NotifyKind Kind { get; init; } = NotifyKind.Info;
    public string Title { get; init; } = "";
    public string Message { get; init; } = "";
    public string? Context { get; init; }
    public bool ShowOpenSettings { get; init; }
    public int AutoCloseSeconds { get; init; } = 6;
}

/// <summary>
/// 右下角系统通知（AUMID + Shell_NotifyIcon 气泡，零第三方依赖）。
/// 界面进程挂着常驻托盘时优先用托盘图标弹（不闪临时图标）；
/// 守护进程没有托盘，走后台线程的临时图标——两条通道都要能弹，
/// 否则"界面没开"的无人值守场景就收不到验证码/异常提醒。
/// 自定义横幅由 App 层注册 CustomHandler；未注册（如测试/CLI）回退系统气泡。
/// </summary>
public static class Notify
{
    private const string Aumid = "CampusNetAuth.Tray";

    private static readonly object Sync = new();
    private static readonly Dictionary<string, long> LastSent = new();
    private static bool _aumidDone;
    private static bool _classDone;
    private static WndProcDelegate? _wndProc; // 必须持有引用，否则注册回调被 GC 回收

    /// <summary>界面进程注入：返回 true 表示已派发；false 则回退到临时图标。</summary>
    public static Func<string, string, int, bool>? UiBalloon;

    /// <summary>App 层注册的自绘横幅处理器（守护进程与界面进程都能用）。</summary>
    public static Action<NotifyRequest>? CustomHandler;

    public static bool Custom(NotifyRequest request)
    {
        try
        {
            var handler = CustomHandler;
            if (handler != null)
            {
                handler(request);
                return true;
            }
            var text = request.Message +
                       (string.IsNullOrWhiteSpace(request.Context) ? "" : "\n" + request.Context);
            return System(request.Title, text, Math.Max(3, request.AutoCloseSeconds));
        }
        catch (Exception e)
        {
            Log.Debug("自定义通知发送失败: {0}", e.Message);
            return false;
        }
    }

    public static bool System(string title, string message, int timeout = 10, bool dedup = true)
    {
        try
        {
            if (dedup)
            {
                var key = title + "\u0000" + message;
                var now = Utils.Now();
                lock (Sync)
                {
                    if (LastSent.TryGetValue(key, out var last) && now - last < 60) return false;
                    LastSent[key] = now;
                    if (LastSent.Count > 32)
                    {
                        foreach (var k in LastSent.Keys.Take(16).ToList()) LastSent.Remove(k);
                    }
                }
            }
            Log.Info("通知：{0} - {1}", title, message);

            var hook = UiBalloon;
            if (hook != null)
            {
                try
                {
                    if (hook(title, message, timeout)) return true;
                }
                catch
                {
                    // 界面侧失败（正在退出等）→ 走临时图标
                }
            }
            new Thread(() => BalloonWorker(title, message, timeout))
            {
                IsBackground = true,
                Name = "cna-notify",
            }.Start();
            return true;
        }
        catch (Exception e)
        {
            Log.Debug("系统通知发送失败（已忽略）: {0}", e.Message);
            return false;
        }
    }

    /// <summary>
    /// Win10 1607+ / Win11 的硬性前提：没有 AUMID 的进程调 Shell_NotifyIcon
    /// 发气泡会被系统静默丢弃（API 返回成功但什么都不显示）。失败不抛。
    /// </summary>
    private static void EnsureAumid()
    {
        if (_aumidDone) return;
        lock (Sync)
        {
            if (_aumidDone) return;
            try
            {
                SetCurrentProcessExplicitAppUserModelID(Aumid);
            }
            catch
            {
                // 退化：通知可能不可见，不影响认证
            }
            _aumidDone = true;
        }
    }

    private static bool EnsureWindowClass()
    {
        if (_classDone) return true;
        lock (Sync)
        {
            if (_classDone) return true;
            _wndProc = (h, msg, wp, lp) =>
            {
                if (msg == WmDestroy) PostQuitMessage(0);
                return DefWindowProcW(h, msg, wp, lp);
            };
            var wc = new WndClassW
            {
                lpfnWndProc = Marshal.GetFunctionPointerForDelegate(_wndProc),
                hInstance = GetModuleHandleW(null),
                lpszClassName = ClassName,
            };
            if (RegisterClassW(ref wc) == 0) return false;
            _classDone = true;
            return true;
        }
    }

    /// <summary>注册消息窗口 → 加临时托盘图标 → 弹气泡 → 泵消息 → 清理。任何一步失败都静默退出。</summary>
    private static void BalloonWorker(string title, string message, int timeout)
    {
        var hwnd = IntPtr.Zero;
        var nid = default(NotifyIconDataW);
        var added = false;
        try
        {
            EnsureAumid();
            if (!EnsureWindowClass()) return;

            hwnd = CreateWindowExW(0, ClassName, ClassName, 0, 0, 0, 0, 0,
                HwndMessage, IntPtr.Zero, GetModuleHandleW(null), IntPtr.Zero);
            if (hwnd == IntPtr.Zero) return;

            nid = new NotifyIconDataW
            {
                cbSize = (uint)Marshal.SizeOf<NotifyIconDataW>(),
                hWnd = hwnd,
                uID = 1,
                uFlags = NifIcon | NifTip | NifMessage,
                uCallbackMessage = WmUser + 1,
                hIcon = LoadIconW(IntPtr.Zero, (IntPtr)IdiInformation),
                szTip = AppInfo.AppName,
                szInfo = "",
                szInfoTitle = "",
            };
            if (!Shell_NotifyIconW(NimAdd, ref nid))
            {
                DestroyWindow(hwnd);
                hwnd = IntPtr.Zero;
                return;
            }
            added = true;

            nid.uFlags = NifInfo;
            nid.dwInfoFlags = NiifInfo;
            nid.uTimeout = (uint)(Math.Max(5, timeout) * 1000);
            nid.szInfoTitle = Truncate(title, 63);
            nid.szInfo = Truncate(message, 255);
            Shell_NotifyIconW(NimModify, ref nid);

            // 保持图标 + 消息泵存活，确保通知送达并被系统登记
            var deadline = Environment.TickCount64 + (Math.Max(3, timeout) + 2) * 1000L;
            while (Environment.TickCount64 < deadline)
            {
                while (PeekMessageW(out var m, IntPtr.Zero, 0, 0, PmRemove))
                {
                    TranslateMessage(ref m);
                    DispatchMessageW(ref m);
                }
                Thread.Sleep(50);
            }
        }
        catch (Exception e)
        {
            Log.Debug("系统通知发送失败（已忽略）: {0}", e.Message);
        }
        finally
        {
            try
            {
                if (added) Shell_NotifyIconW(NimDelete, ref nid);
                if (hwnd != IntPtr.Zero) DestroyWindow(hwnd);
            }
            catch
            {
                // 清理失败只会留下一个不可见的消息窗口，随进程退出
            }
        }
    }

    private static string Truncate(string s, int max) =>
        string.IsNullOrEmpty(s) ? "" : (s.Length <= max ? s : s[..max]);

    private const string ClassName = "CampusNetAuthNotify";
    private const uint NimAdd = 0x0, NimModify = 0x1, NimDelete = 0x2;
    private const uint NifMessage = 0x1, NifIcon = 0x2, NifTip = 0x4, NifInfo = 0x10;
    private const uint NiifInfo = 0x1;
    private const int IdiInformation = 32516;
    private const uint WmUser = 0x0400, WmDestroy = 0x0002;
    private const uint PmRemove = 0x0001;
    private static readonly IntPtr HwndMessage = new(-3);

    private delegate IntPtr WndProcDelegate(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NotifyIconDataW
    {
        public uint cbSize;
        public IntPtr hWnd;
        public uint uID;
        public uint uFlags;
        public uint uCallbackMessage;
        public IntPtr hIcon;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string szTip;
        public uint dwState;
        public uint dwStateMask;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string szInfo;
        public uint uTimeout;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string szInfoTitle;
        public uint dwInfoFlags;
        public Guid guidItem;
        public IntPtr hBalloonIcon;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct WndClassW
    {
        public uint style;
        public IntPtr lpfnWndProc;
        public int cbClsExtra;
        public int cbWndExtra;
        public IntPtr hInstance;
        public IntPtr hIcon;
        public IntPtr hCursor;
        public IntPtr hbrBackground;
        [MarshalAs(UnmanagedType.LPWStr)] public string? lpszMenuName;
        [MarshalAs(UnmanagedType.LPWStr)] public string lpszClassName;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Msg
    {
        public IntPtr hwnd;
        public uint message;
        public IntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public int ptX;
        public int ptY;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool Shell_NotifyIconW(uint message, ref NotifyIconDataW data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern ushort RegisterClassW(ref WndClassW wc);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateWindowExW(uint exStyle, string className,
        string windowName, uint style, int x, int y, int width, int height,
        IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);

    [DllImport("user32.dll")]
    private static extern bool DestroyWindow(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr DefWindowProcW(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern void PostQuitMessage(int exitCode);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr GetModuleHandleW(string? moduleName);

    [DllImport("user32.dll")]
    private static extern IntPtr LoadIconW(IntPtr instance, IntPtr iconName);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool PeekMessageW(out Msg msg, IntPtr hWnd,
        uint filterMin, uint filterMax, uint removeMsg);

    [DllImport("user32.dll")]
    private static extern bool TranslateMessage(ref Msg msg);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr DispatchMessageW(ref Msg msg);
}