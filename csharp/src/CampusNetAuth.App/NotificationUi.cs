using System.ComponentModel;
using System.Diagnostics;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using CampusNetAuth.Ui;

namespace CampusNetAuth.App;

/// <summary>
/// 后台进程也能用的右下角自绘横幅：独立 STA 消息线程，从屏幕右侧滑入，
/// 成功显示蓝色圆圈+勾，错误卡带「打开主程序设置 / 取消」按钮。
/// </summary>
internal static class NotificationUi
{
    private static readonly object Sync = new();
    private static Control? _dispatcher;
    private static NotificationForm? _current;

    public static void Register() => Notify.CustomHandler = Show;

    private static void Show(NotifyRequest request)
    {
        EnsureThread();
        var dispatcher = _dispatcher;
        if (dispatcher == null)
        {
            Notify.System(request.Title, request.Message, Math.Max(3, request.AutoCloseSeconds));
            return;
        }
        try
        {
            dispatcher.BeginInvoke((Action)(() => ShowOnUi(request)));
        }
        catch
        {
        }
    }

    private static void ShowOnUi(NotifyRequest request)
    {
        try
        {
            _current?.CloseImmediately();
            var form = new NotificationForm(request);
            _current = form;
            form.FormClosed += (_, _) =>
            {
                if (ReferenceEquals(_current, form)) _current = null;
            };
            form.Show();
        }
        catch (Exception e)
        {
            Log.Warn("显示自定义通知失败: {0}", e.Message);
        }
    }

    private static void EnsureThread()
    {
        if (_dispatcher is { IsHandleCreated: true }) return;
        lock (Sync)
        {
            if (_dispatcher is { IsHandleCreated: true }) return;
            var ready = new ManualResetEventSlim();
            var thread = new Thread(() =>
            {
                try
                {
                    // UI 进程可能已经初始化过，重复调用用 try 包住
                    try { Application.SetHighDpiMode(HighDpiMode.PerMonitorV2); } catch { }
                    try { Application.EnableVisualStyles(); } catch { }
                    try { Application.SetCompatibleTextRenderingDefault(false); } catch { }
                    try { Application.SetDefaultFont(new Font("Microsoft YaHei UI", 9F)); } catch { }
                    var host = new Form
                    {
                        FormBorderStyle = FormBorderStyle.None,
                        ShowInTaskbar = false,
                        StartPosition = FormStartPosition.Manual,
                        Location = new Point(-32000, -32000),
                        Size = new Size(1, 1),
                        Opacity = 0,
                    };
                    host.Show();
                    _dispatcher = host;
                    ready.Set();
                    Application.Run();
                }
                catch (Exception e)
                {
                    Log.Warn("通知 UI 线程启动失败: {0}", e.Message);
                    ready.Set();
                }
            })
            {
                IsBackground = true,
                Name = "cna-notification-ui",
            };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            ready.Wait(2500);
        }
    }

    public static void OpenMainSettings()
    {
        try
        {
            var hwnd = FindWindowW(null, "校园网无感认证");
            if (hwnd != IntPtr.Zero)
            {
                ShowWindow(hwnd, 9); // SW_RESTORE
                SetForegroundWindow(hwnd);
                return;
            }
            Process.Start(new ProcessStartInfo(AppInfo.ExePath) { UseShellExecute = true });
        }
        catch (Exception e)
        {
            Log.Warn("打开主程序失败: {0}", e.Message);
        }
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr FindWindowW(string? className, string windowName);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);
}

internal sealed class NotificationForm : ShadowForm
{
    private static readonly Color Card = Color.FromArgb(0xFF, 0xFF, 0xFF);
    private static readonly Color BorderColor = Color.FromArgb(0xE3, 0xE3, 0xE8);
    private static readonly Color TextMain = Color.FromArgb(0x1C, 0x1C, 0x1E);
    private static readonly Color TextSub = Color.FromArgb(0x6E, 0x6E, 0x73);
    private static readonly Color Primary = Color.FromArgb(0x00, 0x7A, 0xFF);
    private static readonly Color ErrorColor = Color.FromArgb(0xC0, 0x39, 0x2B);

    private readonly NotifyRequest _request;
    private readonly System.Windows.Forms.Timer _timer = new() { Interval = 15 };
    private readonly Stopwatch _clock = new();
    private int _phase; // 0 入场 / 1 停留 / 2 退场
    private double _phaseStart;
    private int _startX;
    private int _targetX;
    private long _holdUntil;

    public NotificationForm(NotifyRequest request)
    {
        _request = request;
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        TopMost = true;
        StartPosition = FormStartPosition.Manual;
        Location = new Point(-32000, -32000);
        BackColor = Card;
        Font = new Font("Microsoft YaHei UI", 9F);
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        DoubleBuffered = true;

        var success = request.Kind == NotifyKind.Success;
        var width = success ? 372 : 480;
        var textWidth = width - 96;
        var messageFont = new Font("Microsoft YaHei UI", success ? 9.5F : 9F);
        var messageHeight = TextRenderer.MeasureText(request.Message, messageFont,
            new Size(textWidth, int.MaxValue), TextFormatFlags.WordBreak).Height;
        var contextFont = new Font("Microsoft YaHei UI", 8.25F);
        var contextHeight = string.IsNullOrEmpty(request.Context)
            ? 0
            : TextRenderer.MeasureText(request.Context, contextFont,
                new Size(textWidth, int.MaxValue), TextFormatFlags.WordBreak).Height;

        int height;
        if (success)
        {
            height = 16 + 22 + 8 + Math.Max(18, messageHeight) +
                     (contextHeight > 0 ? 6 + contextHeight : 0) + 16;
        }
        else
        {
            height = 18 + 22 + 8 + Math.Max(20, messageHeight) +
                     (contextHeight > 0 ? 6 + contextHeight : 0) +
                     16 + 34 + 16;
        }
        ClientSize = new Size(width, Math.Max(84, height));

        var icon = new NotificationIcon
        {
            Success = success,
            Location = new Point(success ? 18 : 20, success ? 24 : 20),
            Size = new Size(success ? 36 : 34, success ? 36 : 34),
        };
        Controls.Add(icon);

        var title = new Label
        {
            Text = request.Title,
            Font = new Font("Microsoft YaHei UI", 10.5F, FontStyle.Bold),
            ForeColor = TextMain,
            AutoSize = true,
            Location = new Point(success ? 66 : 66, success ? 16 : 18),
        };
        Controls.Add(title);

        var message = new Label
        {
            Text = request.Message,
            Font = messageFont,
            ForeColor = success ? TextSub : TextMain,
            AutoSize = false,
            Location = new Point(66, success ? 40 : 42),
            Size = new Size(textWidth, Math.Max(18, messageHeight)),
        };
        Controls.Add(message);

        if (contextHeight > 0)
        {
            Controls.Add(new Label
            {
                Text = request.Context,
                Font = contextFont,
                ForeColor = TextSub,
                AutoSize = false,
                Location = new Point(66, (success ? 40 : 42) + Math.Max(18, messageHeight) + 6),
                Size = new Size(textWidth, contextHeight),
            });
        }

        if (!success)
        {
            var open = new RoundButton
            {
                Text = "打开主程序设置",
                Size = new Size(146, 34),
                Kind = RoundButton.Style.Primary,
                Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
            };
            open.Location = new Point(ClientSize.Width - 18 - open.Width,
                ClientSize.Height - 16 - open.Height);
            open.Click += (_, _) =>
            {
                NotificationUi.OpenMainSettings();
                CloseAnimated();
            };
            Controls.Add(open);

            var cancel = new RoundButton
            {
                Text = "取消",
                Size = new Size(84, 34),
                Kind = RoundButton.Style.Ghost,
                Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
            };
            cancel.Location = new Point(open.Left - 10 - cancel.Width, open.Top);
            cancel.Click += (_, _) => CloseAnimated();
            Controls.Add(cancel);
        }

        Click += (_, _) => { if (success) CloseAnimated(); };
        foreach (Control c in Controls)
        {
            if (success) c.Click += (_, _) => CloseAnimated();
        }
        _timer.Tick += OnTick;
        Shown += (_, _) => BeginShow();
        Resize += (_, _) => UpdateRegion();
    }

    protected override bool ShowWithoutActivation => true;

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            cp.ExStyle |= 0x08000000 | 0x00000080 | 0x00000008; // NOACTIVATE | TOOLWINDOW | TOPMOST
            return cp;
        }
    }

    public void CloseImmediately()
    {
        _timer.Stop();
        Close();
        Dispose();
    }

    private void BeginShow()
    {
        UpdateRegion();
        var work = Screen.PrimaryScreen?.WorkingArea ?? Screen.GetWorkingArea(this);
        _startX = work.Right + 12;
        _targetX = work.Right - Width - 20;
        Top = work.Bottom - Height - 20;
        Left = _startX;
        _phase = 0;
        _clock.Restart();
        _timer.Start();
    }

    private void CloseAnimated()
    {
        if (_phase == 2 || IsDisposed) return;
        _phase = 2;
        _phaseStart = _clock.Elapsed.TotalMilliseconds;
        _timer.Start();
    }

    private void OnTick(object? sender, EventArgs e)
    {
        var now = _clock.Elapsed.TotalMilliseconds;
        switch (_phase)
        {
            case 0:
            {
                var t = Math.Min(1, now / 260.0);
                var eased = 1 - Math.Pow(1 - t, 3);
                Left = (int)(_startX + (_targetX - _startX) * eased);
                if (t >= 1)
                {
                    _phase = 1;
                    _holdUntil = Environment.TickCount64 + Math.Max(3, _request.AutoCloseSeconds) * 1000;
                }
                break;
            }
            case 1:
                if (Environment.TickCount64 >= _holdUntil) CloseAnimated();
                break;
            case 2:
            {
                var t = Math.Min(1, (now - _phaseStart) / 200.0);
                var eased = t * t;
                Left = (int)(_targetX + (_startX - _targetX) * eased);
                if (t >= 1)
                {
                    _timer.Stop();
                    Close();
                }
                break;
            }
        }
    }

    private void UpdateRegion()
    {
        if (Width <= 0 || Height <= 0) return;
        try
        {
            using var path = Rounded(new Rectangle(0, 0, Width, Height), 14);
            Region = new Region(path);
        }
        catch
        {
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var pen = new Pen(BorderColor);
        using var path = Rounded(new Rectangle(0, 0, Width - 1, Height - 1), 14);
        e.Graphics.DrawPath(pen, path);
    }

    private static GraphicsPath Rounded(Rectangle r, int radius)
    {
        var d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(r.X, r.Y, d, d, 180, 90);
        path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }
}

/// <summary>成功蓝圈+勾 / 错误红圈+感叹号。</summary>
internal sealed class NotificationIcon : Panel
{
    [Browsable(false)]
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public bool Success { get; set; }

    public NotificationIcon()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor, true);
        BackColor = Color.Transparent;
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        var rect = new Rectangle(1, 1, Width - 3, Height - 3);
        using var brush = new SolidBrush(Success
            ? Color.FromArgb(0x00, 0x7A, 0xFF)
            : Color.FromArgb(0xC0, 0x39, 0x2B));
        g.FillEllipse(brush, rect);
        using var pen = new Pen(Color.White, 2.6F)
        {
            StartCap = LineCap.Round,
            EndCap = LineCap.Round,
        };
        if (Success)
        {
            var cx = Width / 2f;
            var cy = Height / 2f;
            g.DrawLines(pen, new[]
            {
                new PointF(cx - 7, cy + 0.5f),
                new PointF(cx - 2, cy + 5.5f),
                new PointF(cx + 8, cy - 5.5f),
            });
        }
        else
        {
            using var font = new Font("Microsoft YaHei UI", 13F, FontStyle.Bold);
            TextRenderer.DrawText(g, "!", font,
                new Rectangle(0, 0, Width, Height - 1), Color.White,
                TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);
        }
    }
}