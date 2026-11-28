using System.Runtime.InteropServices;
using CampusNetAuth.Ui;

namespace CampusNetAuth.Setup;

/// <summary>
/// 自绘安装引导：无边框、浅色、单一主色，与主程序同一套视觉。
/// 只做三件事——选安装位置、开始安装、安装后启动；其余收在右侧「选项」。
/// </summary>
internal sealed class SetupForm : ShadowForm
{
    private const int WmNclButtonDown = 0xA1;
    private const int HtCaption = 2;

    private static readonly Color Bg = Color.FromArgb(0xF5, 0xF5, 0xF7);
    private static readonly Color SideBg = Color.FromArgb(0xEC, 0xEC, 0xF1);
    private static readonly Color Primary = Color.FromArgb(0x00, 0x7A, 0xFF);
    private static readonly Color TextMain = Color.FromArgb(0x1C, 0x1C, 0x1E);
    private static readonly Color TextSub = Color.FromArgb(0x6E, 0x6E, 0x73);

    private readonly string _existingDir;
    private readonly string? _existingVer;
    private readonly bool _hasExisting;

    private readonly TextBox _path;
    private readonly RoundButton _browse;
    private readonly CheckBox _launchBack;
    private readonly Label _hint;
    private readonly Panel _content;
    private readonly Panel _progressPanel;
    private readonly ProgressBar _progress;
    private readonly Label _status;
    private readonly RoundButton _retry;
    private readonly RoundButton _closeAfter;
    private readonly RoundButton _install;
    private readonly RoundButton _cancel;
    private bool _busy;

    public SetupForm()
    {
        _existingDir = Installer.ExistingDir() ?? "";
        _existingVer = Installer.ExistingVersion();
        _hasExisting = _existingDir.Length > 0 && File.Exists(
            Path.Combine(_existingDir, "Update.exe"));

        Text = "安装 校园网无感认证";
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Bg;
        Font = new Font("Microsoft YaHei UI", 9F);
        // AutoScale 必须早于 ClientSize/控件布局，否则 Form 不缩放、子控件却缩放 → 右侧/底部被裁
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(800, 470);
        DoubleBuffered = true;
        KeyPreview = true;

        Controls.Add(_content = new Panel { Dock = DockStyle.Fill, BackColor = Bg });
        Controls.Add(BuildTitleBar());

        var version = Installer.Version;
        var sameVersion = _hasExisting && string.Equals(
            _existingVer, version, StringComparison.OrdinalIgnoreCase);
        var icon = new PictureBox
        {
            Location = new Point(36, 66),
            Size = new Size(56, 56),
            SizeMode = PictureBoxSizeMode.Zoom,
            Image = Icon.ExtractAssociatedIcon(Application.ExecutablePath)?.ToBitmap(),
        };
        _content.Controls.Add(icon);

        var heading = new Label
        {
            Text = "校园网无感认证",
            Font = new Font("Microsoft YaHei UI", 16F, FontStyle.Bold),
            ForeColor = TextMain,
            AutoSize = true,
            Location = new Point(108, 62),
        };
        _content.Controls.Add(heading);

        var sub = new Label
        {
            Text = $"版本 {version} · 安装到当前用户，无需管理员权限",
            ForeColor = TextSub,
            AutoSize = true,
            Location = new Point(110, 98),
        };
        _content.Controls.Add(sub);

        var pathLabel = new Label
        {
            Text = sameVersion
                ? "已安装位置（重新安装时不可更改）"
                : _hasExisting ? "已安装位置（更新时不可更改）" : "安装位置",
            ForeColor = TextSub,
            AutoSize = true,
            Location = new Point(36, 150),
        };
        _content.Controls.Add(pathLabel);

        _path = new TextBox
        {
            Location = new Point(36, 172),
            Size = new Size(440, 30),
            Text = _hasExisting ? _existingDir : Installer.DefaultDir,
            ReadOnly = _hasExisting,
            BackColor = _hasExisting ? Color.FromArgb(0xEE, 0xEE, 0xF2) : Color.White,
            BorderStyle = BorderStyle.FixedSingle,
        };
        _content.Controls.Add(_path);

        _browse = new RoundButton
        {
            Text = "更改…",
            Location = new Point(484, 171),
            Size = new Size(84, 30),
            Kind = RoundButton.Style.Ghost,
        };
        _browse.Click += (_, _) => ChooseFolder();
        _browse.Enabled = !_hasExisting;
        _content.Controls.Add(_browse);

        _hint = new Label
        {
            Text = sameVersion
                ? $"已安装 v{_existingVer}；重新安装会原地覆盖程序文件，配置、凭据与开机自启都会保留。"
                : _hasExisting
                    ? $"已安装 v{_existingVer ?? "?"}，将更新到 v{version}。配置、凭据与开机自启都会保留。"
                    : "安装完成后会自动导入旧版配置、凭据与开机自启，旧目录移入回收站（可恢复）。",
            ForeColor = TextSub,
            Location = new Point(36, 214),
            Size = new Size(500, 54),
        };
        _content.Controls.Add(_hint);

        var side = new Panel
        {
            Location = new Point(580, 64),
            Size = new Size(184, 200),
            BackColor = SideBg,
            Anchor = AnchorStyles.Top | AnchorStyles.Right,
        };
        _content.Controls.Add(side);
        side.Controls.Add(new Label
        {
            Text = "选项",
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Bold),
            ForeColor = TextMain,
            AutoSize = true,
            Location = new Point(14, 14),
        });
        _launchBack = new CheckBox
        {
            Text = "安装完成后启动",
            Checked = true,
            AutoSize = true,
            ForeColor = TextMain,
            Location = new Point(12, 46),
        };
        side.Controls.Add(_launchBack);
        side.Controls.Add(new Label
        {
            Text = "数据目录\n%APPDATA%\\CampusNetAuth\n卸载时默认保留",
            Font = new Font("Microsoft YaHei UI", 8.5F),
            ForeColor = TextSub,
            AutoSize = false,
            Size = new Size(156, 90),
            Location = new Point(14, 82),
        });

        _install = new RoundButton
        {
            Text = sameVersion ? "重新安装" : _hasExisting ? "更新" : "安装",
            Location = new Point(640, 372),
            Size = new Size(124, 38),
            Kind = RoundButton.Style.Primary,
            Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
        };
        _install.Click += (_, _) => BeginInstall();
        _content.Controls.Add(_install);

        _cancel = new RoundButton
        {
            Text = "取消",
            Location = new Point(510, 372),
            Size = new Size(110, 38),
            Kind = RoundButton.Style.Ghost,
            Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
        };
        _cancel.Click += (_, _) => { if (!_busy) Close(); };
        _content.Controls.Add(_cancel);

        _progressPanel = new Panel
        {
            Location = new Point(36, 148),
            Size = new Size(728, 208),
            BackColor = Bg,
            Visible = false,
            Anchor = AnchorStyles.Top | AnchorStyles.Bottom |
                     AnchorStyles.Left | AnchorStyles.Right,
        };
        _content.Controls.Add(_progressPanel);
        _status = new Label
        {
            Text = "正在安装…",
            Font = new Font("Microsoft YaHei UI", 11F),
            ForeColor = TextMain,
            Location = new Point(0, 10),
            Size = new Size(688, 60),
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
        };
        _progressPanel.Controls.Add(_status);
        _progress = new ProgressBar
        {
            Location = new Point(0, 78),
            Size = new Size(688, 10),
            Style = ProgressBarStyle.Marquee,
            MarqueeAnimationSpeed = 24,
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
        };
        _progressPanel.Controls.Add(_progress);

        _retry = new RoundButton
        {
            Text = "重试",
            Location = new Point(568, 156),
            Size = new Size(124, 38),
            Kind = RoundButton.Style.Primary,
            Visible = false,
            Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
        };
        _retry.Click += (_, _) => BeginInstall();
        _progressPanel.Controls.Add(_retry);

        _closeAfter = new RoundButton
        {
            Text = "关闭",
            Location = new Point(448, 156),
            Size = new Size(110, 38),
            Kind = RoundButton.Style.Ghost,
            Visible = false,
            Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
        };
        _closeAfter.Click += (_, _) => Close();
        _progressPanel.Controls.Add(_closeAfter);

        KeyDown += (_, e) =>
        {
            if (e.KeyCode == Keys.Escape && !_busy) Close();
        };
        FormClosing += (_, e) =>
        {
            if (_busy) e.Cancel = true;
        };
        // 避免打开时路径框获得焦点并全选高亮
        Shown += (_, _) => _install.Focus();
    }

    private Control BuildTitleBar()
    {
        var bar = new Panel
        {
            Dock = DockStyle.Top,
            Height = 42,
            BackColor = Bg,
        };
        var title = new Label
        {
            Text = "安装 校园网无感认证",
            Font = new Font("Microsoft YaHei UI", 9.5F, FontStyle.Bold),
            ForeColor = TextMain,
            AutoSize = true,
            Location = new Point(20, 13),
        };
        bar.Controls.Add(title);

        var closeHost = new Panel
        {
            Dock = DockStyle.Right,
            Width = 52,
            BackColor = Bg,
        };
        bar.Controls.Add(closeHost);

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
        close.Click += (_, _) => { if (!_busy) Close(); };
        closeHost.Controls.Add(close);

        void Drag(object? _, MouseEventArgs e)
        {
            if (e.Button != MouseButtons.Left) return;
            ReleaseCapture();
            SendMessage(Handle, WmNclButtonDown, (IntPtr)HtCaption, IntPtr.Zero);
        }
        bar.MouseDown += Drag;
        title.MouseDown += Drag;
        return bar;
    }

    private void ChooseFolder()
    {
        using var dlg = new FolderBrowserDialog
        {
            Description = "选择安装位置",
            UseDescriptionForTitle = true,
            SelectedPath = Directory.Exists(_path.Text) ? _path.Text : Installer.DefaultDir,
            ShowNewFolderButton = true,
        };
        if (dlg.ShowDialog(this) == DialogResult.OK && dlg.SelectedPath.Length > 0)
            _path.Text = dlg.SelectedPath;
    }

    private void BeginInstall()
    {
        if (_busy) return;
        var dir = _hasExisting ? _existingDir : _path.Text;
        var (valid, error) = Installer.ValidateDir(dir);
        if (valid == null)
        {
            ThemedDialog.Error(this, "安装位置无效", error ?? "安装位置无效");
            return;
        }

        _busy = true;
        _install.Enabled = _cancel.Enabled = _browse.Enabled = _path.Enabled = _launchBack.Enabled = false;
        _progressPanel.Visible = true;
        _progressPanel.BringToFront();
        _status.ForeColor = TextMain;
        _status.Text = _hasExisting ? "正在更新…" : "正在安装…";
        _progress.Visible = true;
        _retry.Visible = false;
        _closeAfter.Visible = false;

        var launch = _launchBack.Checked;
        var target = valid;
        var thread = new Thread(() =>
        {
            var (ok, detail) = Installer.Install(target, launch);
            try
            {
                BeginInvoke((Action)(() => Finish(ok, detail, launch)));
            }
            catch
            {
            }
        })
        {
            IsBackground = true,
            Name = "cna-install",
        };
        thread.Start();
    }

    private void Finish(bool ok, string detail, bool launch)
    {
        _busy = false;
        _progress.Visible = false;
        if (ok)
        {
            _status.ForeColor = Color.FromArgb(0x1B, 0x7F, 0x3B);
            _status.Text = "安装完成" + (launch ? "，正在启动…" : "");
            _closeAfter.Visible = true;
            if (launch)
            {
                var t = new System.Windows.Forms.Timer { Interval = 600 };
                t.Tick += (_, _) => { t.Stop(); Close(); };
                t.Start();
            }
        }
        else
        {
            _status.ForeColor = Color.FromArgb(0xC0, 0x39, 0x2B);
            _status.Text = "安装失败\r\n" + detail;
            _retry.Visible = true;
            _closeAfter.Visible = true;
        }
    }

    private const int WmSysCommand = 0x0112;

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
}
