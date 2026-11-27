using CampusNetAuth.App;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        return Cli.Run(args);
    }
}
