using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

// 用 WebView2 渲染 assets/icon.html（HTML/CSS/SVG）并截图为 PNG（保留透明）。
// 用法: IconRenderer <html路径> <输出png> [尺寸]
internal static class Program
{
    private static int _result = 1;
    private static readonly ManualResetEventSlim Done = new(false);

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length < 2) return 2;
        var html = Path.GetFullPath(args[0]);
        var output = Path.GetFullPath(args[1]);
        var size = args.Length >= 3 && int.TryParse(args[2], out var s) ? s : 1024;

        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();

        using var form = new Form
        {
            FormBorderStyle = FormBorderStyle.None,
            ShowInTaskbar = false,
            StartPosition = FormStartPosition.Manual,
            Location = new Point(-4000, -4000),
            Size = new Size(size, size),
        };
        var web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.Transparent };
        form.Controls.Add(web);

        form.Load += async (_, _) =>
        {
            try
            {
                // 受限沙箱里工作区内的 exe 不能往工作区外写：用户数据目录放在工具目录下
                var dataDir = Path.Combine(AppContext.BaseDirectory, "webview2");
                Directory.CreateDirectory(dataDir);
                var env = await CoreWebView2Environment.CreateAsync(null, dataDir);
                await web.EnsureCoreWebView2Async(env);
                web.CoreWebView2.NavigationCompleted += async (_, e) =>
                {
                    try
                    {
                        if (!e.IsSuccess) throw new InvalidOperationException("navigation failed");
                        await Task.Delay(400);
                        await using var ms = new MemoryStream();
                        await web.CoreWebView2.CapturePreviewAsync(
                            CoreWebView2CapturePreviewImageFormat.Png, ms);
                        File.WriteAllBytes(output, ms.ToArray());
                        _result = 0;
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine(ex);
                    }
                    finally
                    {
                        Done.Set();
                        form.Close();
                    }
                };
                web.CoreWebView2.Navigate(new Uri(html).AbsoluteUri);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(ex);
                Done.Set();
                form.Close();
            }
        };

        var timer = new System.Windows.Forms.Timer { Interval = 40000 };
        timer.Tick += (_, _) => { Console.Error.WriteLine("render timeout"); form.Close(); };
        timer.Start();

        Application.Run(form);
        timer.Stop();
        return _result;
    }
}
