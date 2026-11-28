using System.ComponentModel;
using System.Runtime.InteropServices;

namespace CampusNetAuth.Ui;

/// <summary>
/// 无边框但保留 DWM 投影/圆角/系统动画的窗体基类。
/// WS_CAPTION + WS_THICKFRAME 让 DWM 画阴影；WM_NCCALCSIZE 去掉非客户区，
/// WM_NCHITTEST 一律回 HTCLIENT 防止用户拖边缩放（拖动由标题区触发）。
/// </summary>
internal class ShadowForm : Form
{
    private const int WmNcCalcSize = 0x0083;
    private const int WmNcHitTest = 0x0084;
    private const int HtClient = 1;
    private const int WsCaption = 0x00C00000;
    private const int WsThickFrame = 0x00040000;
    private const int CsDropShadow = 0x20000;
    private const int DwmwaWindowCornerPreference = 33;
    private const int DwmwcpRound = 2;

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            cp.Style |= WsCaption | WsThickFrame;
            cp.ClassStyle |= CsDropShadow;
            return cp;
        }
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WmNcCalcSize && m.WParam != IntPtr.Zero)
        {
            m.Result = IntPtr.Zero;
            return;
        }
        if (m.Msg == WmNcHitTest)
        {
            m.Result = (IntPtr)HtClient;
            return;
        }
        base.WndProc(ref m);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        try
        {
            var pref = DwmwcpRound;
            DwmSetWindowAttribute(Handle, DwmwaWindowCornerPreference, ref pref, sizeof(int));
            var margins = new Margins { Left = 1, Right = 1, Top = 1, Bottom = 1 };
            DwmExtendFrameIntoClientArea(Handle, ref margins);
        }
        catch
        {
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Margins
    {
        public int Left, Right, Top, Bottom;
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    [DllImport("dwmapi.dll")]
    private static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd, ref Margins margins);
}

/// <summary>
/// 与应用/安装器同风格的自绘提示窗（替代系统 MessageBox）：无边框、浅色、单一主色、
/// 圆角 + 投影，按钮走圆角样式。主程序与安装引导器共用本文件。
/// </summary>
internal sealed class ThemedDialog : ShadowForm
{
    private const int WmNclButtonDown = 0xA1;
    private const int HtCaption = 2;

    private static readonly Color Bg = Color.FromArgb(0xF5, 0xF5, 0xF7);
    private static readonly Color Primary = Color.FromArgb(0x00, 0x7A, 0xFF);
    private static readonly Color TextMain = Color.FromArgb(0x1C, 0x1C, 0x1E);
    private static readonly Color TextSub = Color.FromArgb(0x6E, 0x6E, 0x73);

    private readonly bool _confirm;

    private ThemedDialog(string title, string message, string? detail,
        bool confirm, string okText, string cancelText)
    {
        _confirm = confirm;
        Text = title;
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        BackColor = Bg;
        Font = new Font("Microsoft YaHei UI", 9F);
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        DoubleBuffered = true;
        KeyPreview = true;

        const int width = 480;
        const int pad = 28;
        var inner = width - pad * 2;
        var messageFont = new Font("Microsoft YaHei UI", 9.5F);
        var messageSize = TextRenderer.MeasureText(message, messageFont,
            new Size(inner, int.MaxValue), TextFormatFlags.WordBreak);
        var detailFont = new Font("Microsoft YaHei UI", 8.5F);
        var detailSize = string.IsNullOrEmpty(detail)
            ? Size.Empty
            : TextRenderer.MeasureText(detail, detailFont,
                new Size(inner, int.MaxValue), TextFormatFlags.WordBreak);

        var height = 44 + 16 + messageSize.Height
                     + (detailSize.Height > 0 ? 10 + detailSize.Height : 0)
                     + 18 + 38 + 20;
        ClientSize = new Size(width, Math.Max(200, height));

        Controls.Add(BuildTitleBar(title));

        Controls.Add(new Label
        {
            Text = message,
            Font = messageFont,
            ForeColor = TextMain,
            AutoSize = false,
            Location = new Point(pad, 60),
            Size = new Size(inner, messageSize.Height),
        });

        if (detailSize.Height > 0)
        {
            Controls.Add(new Label
            {
                Text = detail,
                Font = detailFont,
                ForeColor = TextSub,
                AutoSize = false,
                Location = new Point(pad, 60 + messageSize.Height + 10),
                Size = new Size(inner, detailSize.Height),
            });
        }

        var ok = new RoundButton
        {
            Text = okText,
            Size = new Size(112, 38),
            Kind = RoundButton.Style.Primary,
            Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
        };
        ok.Location = new Point(ClientSize.Width - pad - ok.Width, ClientSize.Height - 20 - ok.Height);
        ok.Click += (_, _) =>
        {
            DialogResult = DialogResult.OK;
            Close();
        };
        Controls.Add(ok);
        AcceptButton = ok;

        if (confirm)
        {
            var cancel = new RoundButton
            {
                Text = cancelText,
                Size = new Size(100, 38),
                Kind = RoundButton.Style.Ghost,
                Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
            };
            cancel.Location = new Point(ok.Left - 10 - cancel.Width, ok.Top);
            cancel.Click += (_, _) =>
            {
                DialogResult = DialogResult.Cancel;
                Close();
            };
            Controls.Add(cancel);
            CancelButton = cancel;
        }

        KeyDown += (_, e) =>
        {
            if (e.KeyCode == Keys.Escape)
            {
                DialogResult = _confirm ? DialogResult.Cancel : DialogResult.OK;
                Close();
            }
        };
    }

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            cp.ExStyle |= 0x00000080; // WS_EX_TOOLWINDOW：不占任务栏/Alt-Tab
            return cp;
        }
    }

    private Control BuildTitleBar(string title)
    {
        var bar = new Panel
        {
            Dock = DockStyle.Top,
            Height = 44,
            BackColor = Bg,
        };
        var label = new Label
        {
            Text = title,
            Font = new Font("Microsoft YaHei UI", 9.5F, FontStyle.Bold),
            ForeColor = TextMain,
            AutoSize = true,
            Location = new Point(24, 13),
        };
        bar.Controls.Add(label);

        var closeHost = new Panel { Dock = DockStyle.Right, Width = 52, BackColor = Bg };
        var close = new Label
        {
            Text = "✕",
            Font = new Font("Microsoft YaHei UI", 11F),
            ForeColor = TextSub,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
            Cursor = Cursors.Hand,
        };
        close.MouseEnter += (_, _) => close.ForeColor = TextMain;
        close.MouseLeave += (_, _) => close.ForeColor = TextSub;
        close.Click += (_, _) =>
        {
            DialogResult = _confirm ? DialogResult.Cancel : DialogResult.OK;
            Close();
        };
        closeHost.Controls.Add(close);
        bar.Controls.Add(closeHost);

        void Drag(object? _, MouseEventArgs e)
        {
            if (e.Button != MouseButtons.Left) return;
            ReleaseCapture();
            SendMessage(Handle, WmNclButtonDown, (IntPtr)HtCaption, IntPtr.Zero);
        }
        bar.MouseDown += Drag;
        label.MouseDown += Drag;
        return bar;
    }

    public static void Info(IWin32Window? owner, string title, string message, string? detail = null)
        => Show(owner, title, message, detail);

    public static void Error(IWin32Window? owner, string title, string message, string? detail = null)
        => Show(owner, title, message, detail);

    public static bool Confirm(IWin32Window? owner, string title, string message,
        string okText = "确定", string cancelText = "取消", string? detail = null)
    {
        using var dlg = new ThemedDialog(title, message, detail, true, okText, cancelText);
        dlg.StartPosition = owner == null
            ? FormStartPosition.CenterScreen
            : FormStartPosition.CenterParent;
        return dlg.ShowDialog(owner) == DialogResult.OK;
    }

    private static void Show(IWin32Window? owner, string title, string message, string? detail)
    {
        using var dlg = new ThemedDialog(title, message, detail, false, "确定", "");
        dlg.StartPosition = owner == null
            ? FormStartPosition.CenterScreen
            : FormStartPosition.CenterParent;
        dlg.ShowDialog(owner);
    }

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
}

/// <summary>圆角按钮：主按钮蓝底白字，次级按钮透明底灰字。</summary>
internal sealed class RoundButton : Button
{
    public enum Style
    {
        Primary,
        Ghost,
    }

    [Browsable(false)]
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public Style Kind { get; set; } = Style.Primary;

    public RoundButton()
    {
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        Font = new Font("Microsoft YaHei UI", 9.5F, FontStyle.Bold);
        Cursor = Cursors.Hand;
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint |
                 ControlStyles.OptimizedDoubleBuffer, true);
        MouseEnter += (_, _) => Invalidate();
        MouseLeave += (_, _) => Invalidate();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.Clear(Parent?.BackColor ?? Color.FromArgb(0xF5, 0xF5, 0xF7));
        g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
        var rect = new Rectangle(0, 0, Width - 1, Height - 1);
        var primary = Kind == Style.Primary;
        var back = primary
            ? (Enabled ? Color.FromArgb(0x00, 0x7A, 0xFF) : Color.FromArgb(0xB8, 0xD8, 0xFA))
            : Color.Transparent;
        if (Enabled && ClientRectangle.Contains(PointToClient(Cursor.Position)))
            back = primary ? Color.FromArgb(0x00, 0x6F, 0xE6) : Color.FromArgb(0xE3, 0xE3, 0xEA);
        using (var path = Rounded(rect, 8))
        {
            using var brush = new SolidBrush(back);
            g.FillPath(brush, path);
        }
        var color = primary ? Color.White : Color.FromArgb(0x3A, 0x3A, 0x3C);
        TextRenderer.DrawText(g, Text, Font, ClientRectangle, color,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter |
            TextFormatFlags.NoPadding);
    }

    private static System.Drawing.Drawing2D.GraphicsPath Rounded(Rectangle r, int radius)
    {
        var d = radius * 2;
        var path = new System.Drawing.Drawing2D.GraphicsPath();
        path.AddArc(r.X, r.Y, d, d, 180, 90);
        path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }
}