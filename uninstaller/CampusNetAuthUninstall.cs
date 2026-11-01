// CampusNetAuthUninstall.cs —— 校园网无感认证 · 彻底卸载程序
// ---------------------------------------------------------------------------
// 为什么需要它（独立文件，而不是"主 exe 的某个参数"）：
//   Windows 上**正在运行的 exe 无法删除自己**。主程序（CampusNetAuth.exe）
//   无论怎么设计都删不掉自己所在的文件与目录 —— 只有另一个可执行文件才能
//   把它（以及它的整个目录）清理干净。所以这里内置一个约 15KB 的独立卸载器，
//   随主 exe 一起打包，用户第一次打开主程序时自动释放到同目录。
//
// 为什么用 C# + .NET Framework：
//   体积小（~15KB，对比 PyInstaller 打包约 8~19MB）、不依赖 Python、**不需要
//   解压临时目录**（卸载器自己就不会残留 _MEI），且 .NET Framework 4.x 是
//   Windows 10/11 的自带组件，无需额外安装。编译用系统自带的
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe（见 build_uninstaller.bat）。
//
// 清理范围（目标是"删完像没装过"，但绝不碰无关文件）：
//   1. 终止程序目录下的全部本程序进程（含 onefile 的引导父进程 —— 它持有
//      exe 文件句柄，不杀掉就删不动文件，这正是历史上"卸载删不掉"的根因）
//   2. HKCU 开机自启 Run 键（仅当它指向本目录/已失效）+ 历史计划任务残留
//   3. %TEMP%\*_MEI* 残留解包目录（按内容特征识别，绝不误删别人的 _MEI）
//   4. 程序目录里的全部产物文件/日志/缓存目录（.webview2-cache 等）
//   5. 自己（改名为临时文件 → 由脱离的 cmd 延时删除）
//
// 用法：
//   CampusNetAuthUninstall.exe              图形确认后彻底卸载
//   CampusNetAuthUninstall.exe --silent     静默卸载（退出码 0=干净、1=有残留）
//   CampusNetAuthUninstall.exe --keep-data  保留账号配置与凭据（config/cred/state）
//   CampusNetAuthUninstall.exe --dir=PATH   指定要清理的程序目录
// ---------------------------------------------------------------------------
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace CampusNetAuthUninstall
{
    internal static class Program
    {
        internal const string AppName = "CampusNetAuth";
        internal const string RunValueName = "CampusNetAuth";
        internal const int SingletonPort = 47667;

        [STAThread]
        private static int Main(string[] args)
        {
            bool silent = false, keepData = false;
            string dir = null;
            string report = null;
            foreach (string a in args)
            {
                string s = (a ?? "").Trim();
                if (s.Equals("--silent", StringComparison.OrdinalIgnoreCase)
                    || s.Equals("-s", StringComparison.OrdinalIgnoreCase)
                    || s.Equals("/silent", StringComparison.OrdinalIgnoreCase))
                    silent = true;
                else if (s.Equals("--keep-data", StringComparison.OrdinalIgnoreCase)
                    || s.Equals("/keep-data", StringComparison.OrdinalIgnoreCase))
                    keepData = true;
                else if (s.StartsWith("--dir=", StringComparison.OrdinalIgnoreCase))
                    dir = s.Substring("--dir=".Length).Trim('"', ' ');
                else if (s.StartsWith("--report=", StringComparison.OrdinalIgnoreCase))
                    report = s.Substring("--report=".Length).Trim('"', ' ');
            }
            if (dir == null || dir.Length == 0)
            {
                dir = Path.GetDirectoryName(Application.ExecutablePath);
                if (dir == null || dir.Length == 0)
                    dir = AppDomain.CurrentDomain.BaseDirectory;
            }
            dir = Path.GetFullPath(dir);

            var job = new UninstallJob(dir, keepData);
            if (report != null && report.Length > 0) job.ReportPath = report;
            if (silent)
            {
                job.Run(null);
                job.WriteReport();
                return job.Failed.Count == 0 ? 0 : 1;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var form = new UninstallForm(job))
            {
                Application.Run(form);
                job.WriteReport();
                return form.ExitCode;
            }
        }
    }

    // ======================= 卸载作业（与界面解耦，便于 --silent 复用） ======
    internal sealed class UninstallJob
    {
        internal readonly string Dir;
        internal bool KeepData;
        internal readonly List<string> Removed = new List<string>();
        internal readonly List<string> Failed = new List<string>();
        internal readonly List<string> Kept = new List<string>();
        internal bool DirRemoved;
        private volatile string _phase = "";
        internal string ReportPath;

        // 产品文件清单（明确的，绝不用通配乱删）
        private static readonly string[] ProductFiles =
        {
            "CampusNetAuth.exe", "CampusNetAuth.exe.old.exe",
            "CampusNetAuthDaemon.exe", "CampusNetAuthDaemon.vbs",
            "CampusNetAuthUI.vbs",
            "daemon.pid", "state.json.lock", "config.json.tmp", "state.json.tmp",
            "_ui_trace.log", "_ui_crash.log", "autostart.log",
        };
        // 用户数据（--keep-data 时保留）
        private static readonly string[] DataFiles =
        {
            "config.json", "cred.bin", "state.json",
            "campusnet.log", "campusnet.log.1", "campusnet.log.2",
            "campusnet.log.3",
        };
        // 目录型产物
        private static readonly string[] ProductDirs =
        {
            ".webview2-cache", "probe", "__pycache__",
        };
        // 卸载器自身（必须最后删）
        private const string SelfName = "CampusNetAuthUninstall.exe";

        internal UninstallJob(string dir, bool keepData)
        {
            Dir = dir;
            KeepData = keepData;
        }

        internal string Phase { get { return _phase; } }

        internal void Run(Action<string> progress)
        {
            Step(progress, "正在停止后台守护与程序进程…");
            StopProgramProcesses();

            Step(progress, "正在移除开机自启与计划任务…");
            RemoveAutostart();

            Step(progress, "正在清理 %TEMP% 解包残留…");
            CleanTempResidue();

            Step(progress, "正在删除程序文件…");
            DeleteProductFiles();

            Step(progress, "正在删除缓存目录…");
            DeleteProductDirs();

            Step(progress, "正在收尾…");
            RemoveDirIfEmpty();
            SelfDelete(progress);
        }

        private void Step(Action<string> progress, string text)
        {
            _phase = text;
            if (progress != null) progress(text);
        }

        /// <summary>把清理明细写成文本（--report=path）：静默部署 / 排障取证用。</summary>
        internal void WriteReport()
        {
            if (string.IsNullOrEmpty(ReportPath)) return;
            var sb = new StringBuilder();
            sb.AppendLine("CampusNetAuth 卸载报告");
            sb.AppendLine("时间: " + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"));
            sb.AppendLine("目录: " + Dir);
            sb.AppendLine("保留用户数据: " + (KeepData ? "是" : "否"));
            sb.AppendLine("结果: " + (Failed.Count == 0 ? "干净" : "有残留"));
            sb.AppendLine();
            sb.AppendLine("[已删除] " + Removed.Count);
            foreach (string s in Removed) sb.AppendLine("  " + s);
            sb.AppendLine("[已保留] " + Kept.Count);
            foreach (string s in Kept) sb.AppendLine("  " + s);
            sb.AppendLine("[失败] " + Failed.Count);
            foreach (string s in Failed) sb.AppendLine("  " + s);
            sb.AppendLine("程序目录已删除: " + (DirRemoved ? "是" : "否"));
            try { File.WriteAllText(ReportPath, sb.ToString(), Encoding.UTF8); }
            catch { }
        }

        // ---------------------------------------------------------------- 1
        /// <summary>
        /// 终止"程序目录里的本程序进程"：判定依据是**映像文件全路径在目录内**，
        /// 绝不按映像名判定（同名多份部署很常见，按名杀会误伤另一份实例）。
        /// 覆盖 onefile 的引导父进程 —— 它持有 exe 文件句柄，不杀掉就删不动文件。
        /// </summary>
        private void StopProgramProcesses()
        {
            int me = Process.GetCurrentProcess().Id;
            for (int attempt = 0; attempt < 6; attempt++)
            {
                List<int> victims = new List<int>();
                foreach (Native.ProcInfo pi in Native.ListProcesses())
                {
                    if (pi.Pid == me) continue;
                    string img = Native.ImagePath(pi.Pid);
                    if (img == null) continue;
                    if (!IsUnderDir(img)) continue;
                    if (!pi.Name.Equals(Program.AppName + ".exe",
                                        StringComparison.OrdinalIgnoreCase)
                        && !pi.Name.Equals(Program.AppName + "Uninstall.exe",
                                           StringComparison.OrdinalIgnoreCase)
                        && !pi.Name.Equals(Program.AppName + "Daemon.exe",
                                           StringComparison.OrdinalIgnoreCase))
                        continue;   // 目录内其它名字的进程不动（不是我们的产物）
                    victims.Add(pi.Pid);
                }
                if (victims.Count == 0) return;
                if (attempt > 0) Thread.Sleep(400);
                foreach (int pid in victims) Native.Kill(pid);
                Thread.Sleep(400);
            }
            // 最后确认一次：仍活着的记入失败清单
            foreach (Native.ProcInfo pi in Native.ListProcesses())
            {
                if (pi.Pid == me) continue;
                string img = Native.ImagePath(pi.Pid);
                if (img != null && IsUnderDir(img))
                    Failed.Add("进程未退出: " + pi.Name + " (PID " + pi.Pid + ")");
            }
        }

        private bool IsUnderDir(string path)
        {
            try
            {
                string p = Path.GetFullPath(path).TrimEnd('\\');
                string d = Dir.TrimEnd('\\');
                return p.StartsWith(d + "\\", StringComparison.OrdinalIgnoreCase);
            }
            catch { return false; }
        }

        // ---------------------------------------------------------------- 2
        /// <summary>
        /// 删除 HKCU 开机自启 Run 键。
        /// 保守规则：只有"指向本目录"或"指向的文件已不存在"时才删 —— 用户
        /// 可能同时装了两份程序，卸载 A 不能把 B 的自启删掉。
        /// </summary>
        private void RemoveAutostart()
        {
            const string runKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(runKey, true))
                {
                    if (k != null)
                    {
                        object v = k.GetValue(Program.RunValueName);
                        if (v != null)
                        {
                            string cmd = Convert.ToString(v);
                            string refPath = ExtractPath(cmd);
                            bool ours = refPath != null
                                && (IsUnderDir(refPath) || !File.Exists(refPath));
                            if (ours)
                            {
                                k.DeleteValue(Program.RunValueName, false);
                                Removed.Add("开机自启注册表项 (HKCU\\...\\Run\\"
                                            + Program.RunValueName + ")");
                            }
                            else
                            {
                                Kept.Add("开机自启注册表项（指向另一份安装，保留）");
                            }
                        }
                    }
                }
            }
            catch (Exception e)
            {
                Failed.Add("删除开机自启注册表项失败: " + e.Message);
            }
            // 历史版本用 schtasks 建的计划任务（尽力而为）
            try
            {
                var psi = new ProcessStartInfo("schtasks.exe",
                    "/Delete /TN \"" + Program.RunValueName + "\" /F");
                psi.CreateNoWindow = true;
                psi.UseShellExecute = false;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                Process p = Process.Start(psi);
                if (p != null) p.WaitForExit(5000);
            }
            catch { /* 计划任务不存在 / schtasks 被策略禁止：都不是错误 */ }
            // 通知用的 AppUserModelID 键（正常不存在，存在则清理）
            try
            {
                Registry.CurrentUser.DeleteSubKeyTree(
                    @"Software\Classes\AppUserModelId\" + "CampusNetAuth.Tray", false);
            }
            catch { }
        }

        private static string ExtractPath(string cmd)
        {
            if (string.IsNullOrEmpty(cmd)) return null;
            int q1 = cmd.IndexOf('"');
            if (q1 >= 0)
            {
                int q2 = cmd.IndexOf('"', q1 + 1);
                if (q2 > q1) return cmd.Substring(q1 + 1, q2 - q1 - 1);
            }
            string[] parts = cmd.Trim().Split(' ');
            return parts.Length > 0 ? parts[0] : null;
        }

        // ---------------------------------------------------------------- 3
        /// <summary>
        /// 清理 %TEMP% 下的 _MEI*（PyInstaller onefile 解包目录）。
        ///
        /// 两道保险，避免伤到"别的正在运行的实例"（同名多份部署很常见）：
        ///   1. 只删带本程序特征的（目录内含本程序内嵌资源，如卸载器 exe
        ///      或旧版的启动器 vbs），免得把别的 PyInstaller 应用解包目录删掉；
        ///   2. **先探测是否正在被使用**：目录里有任何被映射/占用的文件
        ///      （python3xx.dll 等）就整体跳过，一个文件都不动 —— 否则会把
        ///      另一个正在跑的实例的解包目录删残，还会误报"卸载有残留"。
        /// 只有"确实没被占用"的残留（多半是本程序刚被收掉的进程留下的）才删。
        /// </summary>
        private void CleanTempResidue()
        {
            string tmp = Path.GetTempPath();
            // 0.6.0 起不再内嵌 VBS，卸载器自身就是主 exe 解包目录里的特征文件。
            // 保留两个 vbs 标记以清理旧版部署留下的 _MEI 残留。
            string[] markers = { SelfName, "CampusNetAuthDaemon.vbs",
                                  "CampusNetAuthUI.vbs" };
            string[] dirs;
            try { dirs = Directory.GetDirectories(tmp, "_MEI*"); }
            catch { return; }
            foreach (string d in dirs)
            {
                bool ours = false;
                foreach (string m in markers)
                {
                    try
                    {
                        if (File.Exists(Path.Combine(d, m))) { ours = true; break; }
                    }
                    catch { }
                }
                if (!ours) continue;
                if (IsDirInUse(d))
                {
                    // 另一个实例还在用：一个文件都别动
                    Kept.Add("临时解包目录仍在使用中，已跳过: " + Path.GetFileName(d));
                    continue;
                }
                if (DeleteTree(d, 4))
                    Removed.Add("临时解包目录 " + Path.GetFileName(d));
                else
                    Kept.Add("临时解包目录未能删除（可能刚被重新占用），已跳过: "
                             + Path.GetFileName(d));
            }
            // 构建脚本的临时备份（若在开发机上运行卸载器）
            foreach (string n in new[] { "cna_build_config.json", "cna_build_cred.bin" })
            {
                try
                {
                    string f = Path.Combine(tmp, n);
                    if (File.Exists(f)) { File.Delete(f); Removed.Add("临时文件 " + n); }
                }
                catch { }
            }
        }

        /// <summary>
        /// 目录是否正在被别的进程使用（有文件被映射/占用）。
        /// 判据：能否以"读写 + 共享读写删除"打开目录里的可执行类文件 ——
        /// 被映射的 DLL/PYD/EXE 一律打不开，从而准确识别"活着的实例"。
        /// 只探这几类文件（几百个里只有几十个），避免逐个文件探测拖慢卸载。
        /// </summary>
        private static bool IsDirInUse(string dir)
        {
            string[] patterns = { "*.dll", "*.pyd", "*.exe" };
            foreach (string pat in patterns)
            {
                string[] files;
                try { files = Directory.GetFiles(dir, pat, SearchOption.AllDirectories); }
                catch { return true; }
                foreach (string f in files)
                {
                    try
                    {
                        using (new FileStream(f, FileMode.Open, FileAccess.ReadWrite,
                                              FileShare.ReadWrite | FileShare.Delete))
                        { }
                    }
                    catch { return true; }
                }
            }
            return false;
        }

        // ---------------------------------------------------------------- 4
        private void DeleteProductFiles()
        {
            var names = new List<string>(ProductFiles);
            if (!KeepData) names.AddRange(DataFiles);
            else foreach (string d in DataFiles) Kept.Add("用户数据（--keep-data）: " + d);

            // 主程序目录里所有 CampusNetAuth*.exe（含 "(1).exe"、".new.exe" 等
            // 用户丢进来的升级副本）—— 同属本程序命名空间，一并清掉才算干净；
            // 卸载器自身留到最后单独处理。
            try
            {
                foreach (string f in Directory.GetFiles(Dir, "CampusNetAuth*.exe"))
                    names.Add(Path.GetFileName(f));
                foreach (string f in Directory.GetFiles(Dir, "*.old.exe"))
                    names.Add(Path.GetFileName(f));
            }
            catch { }

            foreach (string n in names)
            {
                if (n.Equals(SelfName, StringComparison.OrdinalIgnoreCase)) continue;
                string p = Path.Combine(Dir, n);
                if (!File.Exists(p)) continue;
                if (KeepData && Array.IndexOf(DataFiles, n) >= 0) continue;
                if (DeleteFileWithRetry(p)) Removed.Add(n);
                else Failed.Add("文件未能删除（可能仍被占用）: " + n);
            }
            // _verify*/_test_tmp 等开发期临时目录（发布目录里通常没有）
            try
            {
                foreach (string d in Directory.GetDirectories(Dir, "_verify*"))
                    if (DeleteTree(d)) Removed.Add(Path.GetFileName(d));
                foreach (string d in Directory.GetDirectories(Dir, "_test_tmp"))
                    if (DeleteTree(d)) Removed.Add(Path.GetFileName(d));
            }
            catch { }
        }

        private void DeleteProductDirs()
        {
            foreach (string d in ProductDirs)
            {
                string p = Path.Combine(Dir, d);
                if (!Directory.Exists(p)) continue;
                if (DeleteTree(p)) Removed.Add(d + "\\");
                else Failed.Add("缓存目录未能删除: " + d);
            }
        }

        private static bool DeleteFileWithRetry(string path)
        {
            for (int i = 0; i < 20; i++)
            {
                try
                {
                    if (!File.Exists(path)) return true;
                    File.SetAttributes(path, FileAttributes.Normal);
                    File.Delete(path);
                    if (!File.Exists(path)) return true;
                }
                catch { }
                Thread.Sleep(300);   // onefile 引导进程释放句柄需要一点时间
            }
            return !File.Exists(path);
        }

        private static bool DeleteTree(string dir, int tries = 12)
        {
            for (int i = 0; i < tries; i++)
            {
                try
                {
                    if (!Directory.Exists(dir)) return true;
                    ClearAttributes(dir);
                    Directory.Delete(dir, true);
                    if (!Directory.Exists(dir)) return true;
                }
                catch { }
                Thread.Sleep(300);
            }
            return !Directory.Exists(dir);
        }

        private static void ClearAttributes(string dir)
        {
            try
            {
                foreach (string f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories))
                {
                    try { File.SetAttributes(f, FileAttributes.Normal); } catch { }
                }
            }
            catch { }
        }

        // ---------------------------------------------------------------- 5
        private void RemoveDirIfEmpty()
        {
            try
            {
                if (!Directory.Exists(Dir)) { DirRemoved = true; return; }
                string[] rest = Directory.GetFileSystemEntries(Dir);
                if (rest.Length > 0) return;      // 还有别的东西（用户自己的文件）→ 保留目录
                Directory.Delete(Dir, false);
                DirRemoved = true;
            }
            catch { }
        }

        /// <summary>
        /// 自删：先把自己改名（Windows 允许重命名运行中的 exe，而**删除**不允许），
        /// 再交给一个脱离的 cmd 延时删除；万一 cmd 起不来，登记"重启后删除"兜底。
        /// </summary>
        private void SelfDelete(Action<string> progress)
        {
            string self = Application.ExecutablePath;
            string tmp = Path.Combine(Dir,
                "~uninstall-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".tmp");
            try
            {
                File.Move(self, tmp);
                self = tmp;
            }
            catch { /* 改名失败就按原名删 */ }
            try
            {
                // ping 只是"睡 2 秒"（比 timeout 更可靠：不依赖控制台）
                string cmd = "/c ping -n 3 127.0.0.1 >nul & del /f /q \"" + self + "\"";
                var psi = new ProcessStartInfo("cmd.exe", cmd);
                psi.CreateNoWindow = true;
                psi.UseShellExecute = false;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                Process.Start(psi);
                Removed.Add("卸载程序自身（延时删除）");
            }
            catch
            {
                if (Native.MoveFileEx(self, null, Native.MOVEFILE_DELAY_UNTIL_REBOOT))
                    Removed.Add("卸载程序自身（已登记重启后删除）");
                else
                    Failed.Add("卸载程序自身未能删除，请手动删除: " + self);
            }
        }
    }

    // ======================= Win32 P/Invoke（零子进程枚举/终止） ==============
    internal static class Native
    {
        internal const uint MOVEFILE_DELAY_UNTIL_REBOOT = 0x4;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct PROCESSENTRY32W
        {
            public uint dwSize;
            public uint cntUsage;
            public uint th32ProcessID;
            public IntPtr th32DefaultHeapID;
            public uint th32ModuleID;
            public uint cntThreads;
            public uint th32ParentProcessID;
            public int pcPriClassBase;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string szExeFile;
        }

        internal struct ProcInfo
        {
            internal int Pid;
            internal int ParentPid;
            internal string Name;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32FirstW(IntPtr snap, ref PROCESSENTRY32W pe);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool Process32NextW(IntPtr snap, ref PROCESSENTRY32W pe);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool QueryFullProcessImageNameW(
            IntPtr h, uint flags, StringBuilder buf, ref uint size);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateProcess(IntPtr h, uint code);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern bool MoveFileEx(string existing, string newName,
                                              uint flags);

        private const uint TH32CS_SNAPPROCESS = 0x00000002;
        private const uint PROCESS_TERMINATE = 0x0001;
        private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;

        internal static List<ProcInfo> ListProcesses()
        {
            var list = new List<ProcInfo>();
            IntPtr snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
            if (snap == IntPtr.Zero || snap == new IntPtr(-1)) return list;
            try
            {
                var pe = new PROCESSENTRY32W();
                pe.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32W));
                bool ok = Process32FirstW(snap, ref pe);
                while (ok)
                {
                    ProcInfo pi = new ProcInfo();
                    pi.Pid = (int)pe.th32ProcessID;
                    pi.ParentPid = (int)pe.th32ParentProcessID;
                    pi.Name = pe.szExeFile ?? "";
                    list.Add(pi);
                    ok = Process32NextW(snap, ref pe);
                }
            }
            catch { }
            finally { CloseHandle(snap); }
            return list;
        }

        internal static string ImagePath(int pid)
        {
            IntPtr h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
            if (h == IntPtr.Zero) return null;
            try
            {
                var sb = new StringBuilder(1024);
                uint size = (uint)sb.Capacity;
                if (!QueryFullProcessImageNameW(h, 0, sb, ref size)) return null;
                string s = sb.ToString();
                return s.Length == 0 ? null : s;
            }
            catch { return null; }
            finally { CloseHandle(h); }
        }

        internal static bool Kill(int pid)
        {
            IntPtr h = OpenProcess(PROCESS_TERMINATE, false, pid);
            if (h == IntPtr.Zero) return false;
            try { return TerminateProcess(h, 1); }
            catch { return false; }
            finally { CloseHandle(h); }
        }
    }

    // ============================== 图形界面 ==================================
    internal sealed class UninstallForm : Form
    {
        private readonly UninstallJob _job;
        private readonly CheckBox _keepData;
        private readonly Label _dirLabel;
        private readonly Label _status;
        private readonly ProgressBar _bar;
        private readonly Button _ok;
        private readonly Button _cancel;
        internal int ExitCode = 1;

        internal UninstallForm(UninstallJob job)
        {
            _job = job;
            Text = "卸载 · 校园网无感认证";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(460, 250);
            Font = new Font("Microsoft YaHei UI", 9F, FontStyle.Regular,
                            GraphicsUnit.Point);
            BackColor = Color.FromArgb(245, 245, 247);

            var title = new Label();
            title.Text = "卸载 校园网无感认证";
            title.Font = new Font(Font.FontFamily, 13F, FontStyle.Bold);
            title.ForeColor = Color.FromArgb(28, 28, 30);
            title.AutoSize = true;
            title.Location = new Point(22, 18);
            Controls.Add(title);

            _dirLabel = new Label();
            _dirLabel.Text = "程序目录：" + job.Dir;
            _dirLabel.ForeColor = Color.FromArgb(110, 110, 115);
            _dirLabel.AutoEllipsis = true;
            _dirLabel.Size = new Size(412, 22);
            _dirLabel.Location = new Point(24, 50);
            Controls.Add(_dirLabel);

            var body = new Label();
            body.Text = "将停止后台守护并删除程序文件、缓存与开机自启项。\r\n" +
                        "不会删除其它任何文件；程序目录里若有你自己的文件，目录会保留。";
            body.ForeColor = Color.FromArgb(60, 60, 64);
            body.Size = new Size(412, 48);
            body.Location = new Point(24, 76);
            Controls.Add(body);

            _keepData = new CheckBox();
            _keepData.Text = "保留账号配置与凭据（config.json / cred.bin / state.json）";
            _keepData.Checked = job.KeepData;
            _keepData.AutoSize = true;
            _keepData.ForeColor = Color.FromArgb(60, 60, 64);
            _keepData.Location = new Point(24, 128);
            Controls.Add(_keepData);

            _status = new Label();
            _status.ForeColor = Color.FromArgb(60, 60, 64);
            _status.Size = new Size(412, 20);
            _status.Location = new Point(24, 158);
            Controls.Add(_status);

            _bar = new ProgressBar();
            _bar.Style = ProgressBarStyle.Marquee;
            _bar.MarqueeAnimationSpeed = 0;
            _bar.Size = new Size(412, 6);
            _bar.Location = new Point(24, 180);
            Controls.Add(_bar);

            _ok = new Button();
            _ok.Text = "开始卸载";
            _ok.Size = new Size(104, 32);
            _ok.Location = new Point(228, 202);
            _ok.BackColor = Color.FromArgb(255, 59, 48);
            _ok.ForeColor = Color.White;
            _ok.FlatStyle = FlatStyle.Flat;
            _ok.FlatAppearance.BorderSize = 0;
            _ok.Click += OnUninstall;
            Controls.Add(_ok);

            _cancel = new Button();
            _cancel.Text = "取消";
            _cancel.Size = new Size(88, 32);
            _cancel.Location = new Point(344, 202);
            _cancel.FlatStyle = FlatStyle.Flat;
            _cancel.Click += delegate { Close(); };
            Controls.Add(_cancel);

            AcceptButton = _ok;
            CancelButton = _cancel;
        }

        private void OnUninstall(object sender, EventArgs e)
        {
            _ok.Enabled = false;
            _cancel.Enabled = false;
            _keepData.Enabled = false;
            _bar.MarqueeAnimationSpeed = 30;
            _job.KeepData = _keepData.Checked;
            var worker = new Thread(delegate ()
            {
                try
                {
                    _job.Run(delegate (string text) { SetStatus(text); });
                }
                catch (Exception ex)
                {
                    _job.Failed.Add("卸载过程异常: " + ex.Message);
                }
                try { BeginInvoke(new Action(ShowResult)); }
                catch { }
            });
            worker.IsBackground = true;
            worker.Start();
        }

        private void SetStatus(string text)
        {
            try
            {
                if (InvokeRequired) { BeginInvoke(new Action<string>(SetStatus), text); return; }
                _status.Text = text;
            }
            catch { }
        }

        private void ShowResult()
        {
            _bar.MarqueeAnimationSpeed = 0;
            ExitCode = _job.Failed.Count == 0 ? 0 : 1;
            var sb = new StringBuilder();
            if (_job.Failed.Count == 0)
                sb.AppendLine("卸载完成，已清理干净。");
            else
                sb.AppendLine("卸载完成，但有 " + _job.Failed.Count + " 项未能处理：");
            sb.AppendLine();
            sb.AppendLine("已删除 " + _job.Removed.Count + " 项：");
            AppendList(sb, _job.Removed, 6);
            if (_job.Kept.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("已保留 " + _job.Kept.Count + " 项：");
                AppendList(sb, _job.Kept, 4);
            }
            if (_job.Failed.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("需要手动处理：");
                AppendList(sb, _job.Failed, 8);
            }
            sb.AppendLine();
            sb.AppendLine(_job.DirRemoved
                ? "程序目录已删除。"
                : "程序目录已保留（其中还有非本程序的文件，请自行确认后删除）。");
            _status.Text = _job.Failed.Count == 0 ? "完成。" :
                "完成（有残留，详见对话框）。";
            MessageBox.Show(this, sb.ToString(),
                _job.Failed.Count == 0 ? "卸载完成" : "卸载完成（有残留）",
                MessageBoxButtons.OK,
                _job.Failed.Count == 0 ? MessageBoxIcon.Information
                                       : MessageBoxIcon.Warning);
            Close();
        }

        private static void AppendList(StringBuilder sb, List<string> items, int max)
        {
            int n = Math.Min(items.Count, max);
            for (int i = 0; i < n; i++) sb.AppendLine("  · " + items[i]);
            if (items.Count > n)
                sb.AppendLine("  · …另有 " + (items.Count - n) + " 项");
        }
    }
}