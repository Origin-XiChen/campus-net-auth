using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace CampusNetAuth.App;

/// <summary>
/// 主窗口：无边框 + WebView2（加载现有 HTML UI）+ 托盘。
/// 自绘标题栏（.drag-region）通过 web message 请求系统级拖动，风格与页面一致。
/// </summary>
public sealed class MainForm : Form
{
    private readonly ApiServer _server;
    private readonly WebView2 _web = new();
    private readonly NotifyIcon _tray;
    private bool _closing;

    private const int WmNclButtonDown = 0xA1;
    private const int WmNcCalcSize = 0x0083;
    private const int HtCaption = 2;
    private const int WsThickFrame = 0x00040000;
    private const int WsMinimizeBox = 0x00020000;
    private const int WsMaximizeBox = 0x00010000;
    private const int WsSysMenu = 0x00080000;
    private const int WmSysCommand = 0x0112;
    private const uint TpmReturnCmd = 0x0100;
    private const uint TpmRightButton = 0x0002;
    private const int DwmwaWindowCornerPreference = 33;
    private const int DwmwcpRound = 2;

    // 边缘缩放：JS 命中区发消息 → 系统级 sizing 循环（与自绘标题栏拖动同一机制）
    private static readonly Dictionary<string, int> ResizeHits = new()
    {
        ["left"] = 10, ["right"] = 11, ["top"] = 12,
        ["topleft"] = 13, ["topright"] = 14,
        ["bottom"] = 15, ["bottomleft"] = 16, ["bottomright"] = 17,
    };

    [StructLayout(LayoutKind.Sequential)]
    private struct Margins
    {
        public int Left, Right, Top, Bottom;
    }

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessageW(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr,
        ref int value, int size);

    [DllImport("dwmapi.dll")]
    private static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd,
        ref Margins margins);

    [StructLayout(LayoutKind.Sequential)]
    private struct Point32
    {
        public int X, Y;
    }

    [DllImport("user32.dll")]
    private static extern IntPtr GetSystemMenu(IntPtr hWnd, bool revert);

    [DllImport("user32.dll")]
    private static extern int TrackPopupMenuEx(IntPtr hMenu, uint flags,
        int x, int y, IntPtr hWnd, IntPtr lptpm);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out Point32 pt);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    private bool _revealed;

    public MainForm(ApiServer server)
    {
        _server = server;
        Text = "校园网无感认证";
        FormBorderStyle = FormBorderStyle.None;
        // 先离屏创建：等 WebView2 首帧加载完成再移动到屏幕中央，避免"白屏闪现"
        StartPosition = FormStartPosition.Manual;
        Location = new Point(-20000, -20000);
        Size = new Size(1180, 820);
        MinimumSize = new Size(980, 660);
        BackColor = Color.FromArgb(0xF5, 0xF5, 0xF7);
        DoubleBuffered = true;
        KeyPreview = true;

        _web.Dock = DockStyle.Fill;
        _web.DefaultBackgroundColor = Color.FromArgb(0xF5, 0xF5, 0xF7);
        Controls.Add(_web);

        _server.WindowAction += OnWindowAction;

        var appIcon = Icon.ExtractAssociatedIcon(AppInfo.ExePath) ?? SystemIcons.Application;
        Icon = appIcon;
        _tray = new NotifyIcon
        {
            Icon = appIcon,
            Text = "校园网无感认证",
            Visible = true,
        };
        var menu = new ContextMenuStrip();
        menu.Items.Add("打开界面", null, (_, _) => RestoreWindow());
        menu.Items.Add("启动守护", null, (_, _) => Daemon.Start());
        menu.Items.Add("停止守护", null, (_, _) => Daemon.Stop());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出", null, (_, _) => Close());
        _tray.ContextMenuStrip = menu;
        _tray.DoubleClick += (_, _) => RestoreWindow();

        Load += async (_, _) => await InitWebViewAsync();
        FormClosed += OnFormClosed;
        KeyDown += (_, e) =>
        {
            if (e.KeyCode == Keys.Escape) Close();
        };

        // 兜底：WebView2 初始化异常时也要让窗口出现在用户面前
        var revealTimer = new System.Windows.Forms.Timer { Interval = 6000 };
        revealTimer.Tick += (_, _) =>
        {
            revealTimer.Stop();
            RevealWindow();
        };
        revealTimer.Start();
    }

    /// <summary>无边框但保留系统缩放能力（配合 JS 边缘命中区与 WM_NCCALCSIZE）。</summary>
    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            // 无边框但具备原生窗口能力：缩放、最小化/最大化动画、系统菜单（Alt+Space）
            cp.Style |= WsThickFrame | WsMinimizeBox | WsMaximizeBox | WsSysMenu;
            return cp;
        }
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        try
        {
            // Win11：系统圆角 + DWM 投影（与自绘 UI 的圆角风格一致）
            var pref = DwmwcpRound;
            DwmSetWindowAttribute(Handle, DwmwaWindowCornerPreference, ref pref, sizeof(int));
            var margins = new Margins { Left = 1, Right = 1, Top = 1, Bottom = 1 };
            DwmExtendFrameIntoClientArea(Handle, ref margins);
        }
        catch
        {
            // Win10/旧系统：忽略（无系统圆角，窗口仍可用）
        }
        UpdateMaximizedBounds();
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WmNcCalcSize && m.WParam != IntPtr.Zero)
        {
            // 去掉非客户区（边框），内容铺满窗口；缩放交给 JS 命中区
            m.Result = IntPtr.Zero;
            return;
        }
        base.WndProc(ref m);
    }

    /// <summary>首帧就绪后把离屏窗口移到屏幕中央（系统自带的窗口出现动画随即可见）。</summary>
    private void RevealWindow()
    {
        if (_revealed) return;
        _revealed = true;
        try
        {
            var work = Screen.FromPoint(Cursor.Position).WorkingArea;
            Location = new Point(
                work.Left + Math.Max(0, (work.Width - Width) / 2),
                work.Top + Math.Max(0, (work.Height - Height) / 2));
            Activate();
        }
        catch { }
    }

    /// <summary>标题栏右键 → 原生系统菜单（还原/移动/大小/最小化/最大化/关闭）。</summary>
    private void ShowSystemMenu()
    {
        try
        {
            var menu = GetSystemMenu(Handle, false);
            if (menu == IntPtr.Zero) return;
            GetCursorPos(out var pt);
            SetForegroundWindow(Handle);
            var cmd = TrackPopupMenuEx(menu, TpmReturnCmd | TpmRightButton,
                pt.X, pt.Y, Handle, IntPtr.Zero);
            if (cmd != 0)
                SendMessageW(Handle, WmSysCommand, (IntPtr)cmd, IntPtr.Zero);
        }
        catch { }
    }

    private void UpdateMaximizedBounds()
    {
        try
        {
            MaximizedBounds = Screen.FromControl(this).WorkingArea;
        }
        catch { }
    }

    private async Task InitWebViewAsync()
    {
        try
        {
            var dataDir = PrepareWebViewDataDir();
            var env = await CoreWebView2Environment.CreateAsync(null, dataDir);
            await _web.EnsureCoreWebView2Async(env);

            var core = _web.CoreWebView2;
            core.Settings.AreDefaultContextMenusEnabled = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.AreDevToolsEnabled = false;
            core.Settings.IsZoomControlEnabled = false;
            core.Settings.AreBrowserAcceleratorKeysEnabled = false;

            // 自绘标题栏拖动：HTML 已有 .drag-region（原本给 pywebview 用），
            // 这里在 WebView2 侧接管为系统级拖动/缩放（不修改 UI 代码）
            await core.AddScriptToExecuteOnDocumentCreatedAsync(@"
(function(){
  if (window.__cnaChromeHooked) return;
  window.__cnaChromeHooked = true;

  // 标题栏拖动：.drag-region（原本给 pywebview 用）
  window.addEventListener('mousedown', function(e){
    try{
      if (e.button !== 0) return;
      var t = e.target;
      if (!t || !t.closest) return;
      if (t.closest('.tb-btns')) return;
      if (!t.closest('.drag-region')) return;
      window.chrome.webview.postMessage({type:'drag'});
    }catch(_){}
  }, true);

  // 标题栏右键 → 原生系统菜单
  window.addEventListener('contextmenu', function(e){
    try{
      var t = e.target;
      if (t && t.closest && t.closest('.drag-region') && !t.closest('.tb-btns')){
        e.preventDefault();
        window.chrome.webview.postMessage({type:'sysmenu'});
      }
    }catch(_){}
  }, true);

  // 边缘缩放：8 个不可见命中区（5px 边 / 10px 角），触发系统 sizing
  function addResizeZones(){
    try{
      if (document.getElementById('cna-resize-zones')) return;
      var wrap = document.createElement('div');
      wrap.id = 'cna-resize-zones';
      wrap.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none';
      var zones = [
        ['top', 'left:10px;right:10px;top:0;height:5px;cursor:n-resize'],
        ['bottom', 'left:10px;right:10px;bottom:0;height:5px;cursor:s-resize'],
        ['left', 'left:0;top:10px;bottom:10px;width:5px;cursor:w-resize'],
        ['right', 'right:0;top:10px;bottom:10px;width:5px;cursor:e-resize'],
        ['topleft', 'left:0;top:0;width:10px;height:10px;cursor:nw-resize'],
        ['topright', 'right:0;top:0;width:10px;height:10px;cursor:ne-resize'],
        ['bottomleft', 'left:0;bottom:0;width:10px;height:10px;cursor:sw-resize'],
        ['bottomright', 'right:0;bottom:0;width:10px;height:10px;cursor:se-resize']
      ];
      zones.forEach(function(z){
        var d = document.createElement('div');
        d.style.cssText = 'position:absolute;pointer-events:auto;' + z[1];
        d.addEventListener('mousedown', function(e){
          if (e.button !== 0) return;
          e.preventDefault(); e.stopPropagation();
          window.chrome.webview.postMessage({type:'resize', dir:z[0]});
        });
        wrap.appendChild(d);
      });
      document.body.appendChild(wrap);
    }catch(_){}
  }
  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', addResizeZones);
  } else {
    addResizeZones();
  }
})();
");
            core.WebMessageReceived += (_, e) =>
            {
                try
                {
                    if (JsonNode.Parse(e.WebMessageAsJson) is not JsonObject obj) return;
                    var type = obj.GetString("type");
                    if (type == "drag")
                    {
                        ReleaseCapture();
                        SendMessageW(Handle, WmNclButtonDown, (IntPtr)HtCaption, IntPtr.Zero);
                    }
                    else if (type == "resize"
                             && ResizeHits.TryGetValue(obj.GetString("dir"), out var hit))
                    {
                        ReleaseCapture();
                        SendMessageW(Handle, WmNclButtonDown, (IntPtr)hit, IntPtr.Zero);
                    }
                    else if (type == "sysmenu")
                    {
                        ShowSystemMenu();
                    }
                }
                catch { }
            };

            _web.Source = new Uri(_server.Url);
            core.NavigationCompleted += (_, e) =>
            {
                if (e.IsSuccess) RevealWindow();
            };
        }
        catch (Exception e)
        {
            Log.Error("WebView2 初始化失败: {0}", e.Message);
            RevealWindow();
            MessageBox.Show("界面组件（WebView2）初始化失败：\n" + e.Message +
                            "\n\n请确认系统已安装 Microsoft Edge WebView2 Runtime。",
                "校园网无感认证");
            Close();
        }
    }

    /// <summary>
    /// WebView2 用户数据目录：优先 %LOCALAPPDATA%，其次程序目录（受限沙箱里
    /// 工作区内的 exe 不能往工作区外写），最后 %TEMP%。
    /// </summary>
    private static string PrepareWebViewDataDir()
    {
        var candidates = new[]
        {
            Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "CampusNetAuth", "WebView2"),
            Path.Combine(AppInfo.BaseDir, "webview2"),
            Path.Combine(Path.GetTempPath(), "CampusNetAuth.WebView2"),
        };
        foreach (var dir in candidates)
        {
            try
            {
                Directory.CreateDirectory(dir);
                var probe = Path.Combine(dir, "_write_probe.tmp");
                File.WriteAllText(probe, "x");
                File.Delete(probe);
                return dir;
            }
            catch
            {
                // 换下一个候选
            }
        }
        throw new InvalidOperationException("没有可写的 WebView2 数据目录");
    }

    private void OnWindowAction(string action)
    {
        if (_closing || IsDisposed) return;
        BeginInvoke((Action)(() =>
        {
            switch (action)
            {
                case "minimize":
                    WindowState = FormWindowState.Minimized;
                    break;
                case "maximize":
                    UpdateMaximizedBounds();
                    WindowState = WindowState == FormWindowState.Maximized
                        ? FormWindowState.Normal
                        : FormWindowState.Maximized;
                    break;
                case "close":
                    Close();
                    break;
            }
        }));
    }

    private void RestoreWindow()
    {
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
    }

    private void OnFormClosed(object? sender, FormClosedEventArgs e)
    {
        _closing = true;
        _server.WindowAction -= OnWindowAction;
        _tray.Visible = false;
        _tray.Dispose();
        _server.Dispose();
    }
}