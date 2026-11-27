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
    private const int HtCaption = 2;

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessageW(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    public MainForm(ApiServer server)
    {
        _server = server;
        Text = "校园网无感认证";
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        Size = new Size(1180, 820);
        BackColor = Color.FromArgb(0xF5, 0xF5, 0xF7);
        DoubleBuffered = true;
        KeyPreview = true;

        _web.Dock = DockStyle.Fill;
        _web.DefaultBackgroundColor = Color.FromArgb(0xF5, 0xF5, 0xF7);
        Controls.Add(_web);

        _server.WindowAction += OnWindowAction;

        _tray = new NotifyIcon
        {
            Icon = SystemIcons.Application,   // 图标里程碑替换为自绘图标
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
            // 这里在 WebView2 侧接管为系统级拖动（不修改 UI 代码）
            await core.AddScriptToExecuteOnDocumentCreatedAsync(@"
(function(){
  if (window.__cnaDragHooked) return;
  window.__cnaDragHooked = true;
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
})();
");
            core.WebMessageReceived += (_, e) =>
            {
                try
                {
                    if (JsonNode.Parse(e.WebMessageAsJson) is JsonObject obj
                        && obj.GetString("type") == "drag")
                    {
                        ReleaseCapture();
                        SendMessageW(Handle, WmNclButtonDown, (IntPtr)HtCaption, IntPtr.Zero);
                    }
                }
                catch { }
            };

            _web.Source = new Uri(_server.Url);
        }
        catch (Exception e)
        {
            Log.Error("WebView2 初始化失败: {0}", e.Message);
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