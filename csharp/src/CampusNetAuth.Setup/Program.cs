namespace CampusNetAuth.Setup;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        var opts = Options.Parse(args);
        if (opts.Silent)
        {
            var dir = opts.Dir ?? Installer.ExistingDir() ?? Installer.DefaultDir;
            var (ok, detail) = Installer.Install(dir, launch: !opts.NoLaunch);
            try
            {
                File.AppendAllText(
                    Path.Combine(Path.GetTempPath(), "CampusNetAuthSetup.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + detail + Environment.NewLine);
            }
            catch
            {
            }
            return ok ? 0 : 1;
        }

        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new SetupForm());
        return 0;
    }
}