using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace LiveMonitorDesktop
{
    internal sealed class ClientError : Exception
    {
        public ClientError(string message) : base(message) { }
    }

    internal sealed class NativeResult
    {
        public int ExitCode;
        public Dictionary<string, object> Output;
        public Dictionary<string, object> Error;

        public bool Ready
        {
            get { return ExitCode == 0 && Flag(Output, "ok") && Flag(Output, "board_ready"); }
        }

        public bool Stopped
        {
            get { return ExitCode == 0 && Flag(Output, "ok") && Text(Output, "state") == "stopped"; }
        }

        public static bool Flag(Dictionary<string, object> map, string key)
        {
            object result;
            return map != null && map.TryGetValue(key, out result) && result is bool && (bool)result;
        }

        public static string Text(Dictionary<string, object> map, string key)
        {
            object result;
            return map != null && map.TryGetValue(key, out result) ? result as string : null;
        }

        public static Dictionary<string, object> Object(Dictionary<string, object> map, string key)
        {
            object result;
            return map != null && map.TryGetValue(key, out result) ? result as Dictionary<string, object> : null;
        }

        public string Signal
        {
            get
            {
                return Text(Output, "state") ?? Text(Output, "code") ?? Text(Error, "code") ?? "unavailable";
            }
        }

        public string Diagnostic()
        {
            var facts = new List<string> { "退出码 " + ExitCode };
            foreach (string value in new[] { Text(Output, "state"), Text(Output, "code"), Text(Error, "code"), Text(Output, "error") })
                if (value != null && Regex.IsMatch(value, @"\A[a-zA-Z0-9_]{1,80}\z") && !facts.Contains(value))
                    facts.Add(value);
            return string.Join(" / ", facts);
        }
    }

    internal static class Native
    {
        private const int CaptureLimit = 128 * 1024;

        public static string Quote(string argument)
        {
            var text = new StringBuilder("\"");
            int slashes = 0;
            foreach (char value in argument)
            {
                if (value == '\\')
                {
                    slashes++;
                    continue;
                }
                if (value == '"')
                    text.Append('\\', 2 * slashes + 1);
                else
                    text.Append('\\', slashes);
                slashes = 0;
                text.Append(value);
            }
            return text.Append('\\', 2 * slashes).Append('"').ToString();
        }

        public static string Arguments(params string[] arguments)
        {
            return string.Join(" ", Array.ConvertAll(arguments, Quote));
        }

        public static Dictionary<string, object> Parse(string text)
        {
            try
            {
                return new JavaScriptSerializer { MaxJsonLength = CaptureLimit, RecursionLimit = 40 }
                    .DeserializeObject(text) as Dictionary<string, object>;
            }
            catch (ArgumentException) { return null; }
            catch (InvalidOperationException) { return null; }
        }

        internal static Dictionary<string, object> ReadDocument(string path)
        {
            Paths.OrdinaryFile(path);
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
            {
                if (stream.Length > CaptureLimit)
                    return null;
                using (var input = new StreamReader(stream, new UTF8Encoding(false, true)))
                    return Parse(Capture(input));
            }
        }

        private static string Capture(TextReader reader)
        {
            var buffer = new char[4096];
            var text = new StringBuilder();
            int read;
            while ((read = reader.Read(buffer, 0, buffer.Length)) > 0)
            {
                text.Append(buffer, 0, read);
                if (text.Length > CaptureLimit)
                    text.Remove(0, text.Length - CaptureLimit);
            }
            return text.ToString();
        }

        private static Task<string> Drain(TextReader reader)
        {
            return Task.Factory.StartNew(() => Capture(reader), CancellationToken.None,
                TaskCreationOptions.LongRunning, TaskScheduler.Default);
        }

        public static NativeResult Run(string executable, params string[] args)
        {
            Paths.OrdinaryFile(executable);
            var settings = new ProcessStartInfo(executable, Arguments(args))
            {
                WorkingDirectory = Path.GetDirectoryName(executable),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            using (var process = new Process { StartInfo = settings })
            {
                process.Start();
                var output = Drain(process.StandardOutput);
                var error = Drain(process.StandardError);
                process.WaitForExit();
                Task.WaitAll(output, error);
                return new NativeResult
                {
                    ExitCode = process.ExitCode,
                    Output = Parse(output.Result),
                    Error = Parse(error.Result)
                };
            }
        }
    }

    internal static class Paths
    {
        private static readonly Regex DrivePath = new Regex(@"\A[A-Za-z]:[\\/]", RegexOptions.CultureInvariant);
        private static readonly Regex Device = new Regex(@"\A(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$)\z",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        internal static string FullTarget(string input)
        {
            if (string.IsNullOrWhiteSpace(input) || !DrivePath.IsMatch(input))
                throw new ClientError("请填写本机磁盘上的完整路径，例如 D:\\LiveMonitor。");
            foreach (string part in input.Substring(3).Replace('/', '\\').TrimEnd('\\').Split('\\'))
            {
                if (part.Length == 0 || part == "." || part == ".." ||
                    part.EndsWith(" ", StringComparison.Ordinal) || part.EndsWith(".", StringComparison.Ordinal) ||
                    part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 ||
                    Device.IsMatch(part.Split('.')[0].TrimEnd(' ')))
                    throw new ClientError("路径包含 Windows 安装程序无法使用的名称。请换一个新目录。");
            }
            string path = Path.GetFullPath(input).TrimEnd('\\');
            if (path.Length > 140)
                throw new ClientError("安装位置太长。请改用较短路径，例如 D:\\LiveMonitor。");
            var drive = new DriveInfo(Path.GetPathRoot(path));
            if (!drive.IsReady || (drive.DriveType != DriveType.Fixed && drive.DriveType != DriveType.Removable))
                throw new ClientError("请选择当前可用的本地磁盘。");
            OrdinaryDirectory(Path.GetDirectoryName(path));
            if (Directory.Exists(path))
                OrdinaryDirectory(path);
            else if (File.Exists(path))
                throw new ClientError("此安装位置已经有同名文件。请选择另一个目录。");
            return path;
        }

        internal static bool Same(string left, string right)
        {
            return string.Equals(left, right, StringComparison.OrdinalIgnoreCase);
        }

        internal static bool Contains(string root, string path)
        {
            return Same(root, path) || path.StartsWith(root.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase);
        }

        internal static void Separate(string source, string target)
        {
            if (Contains(source, target) || Contains(target, source))
                throw new ClientError("请选择和解压目录分开的安装位置。");
        }

        internal static void OrdinaryDirectory(string path)
        {
            for (var current = new DirectoryInfo(path); current != null; current = current.Parent)
            {
                FileAttributes attributes = File.GetAttributes(current.FullName);
                if ((attributes & FileAttributes.Directory) == 0 || (attributes & FileAttributes.ReparsePoint) != 0)
                    throw new ClientError("需要通过普通本地目录访问安装文件和安装位置。");
            }
        }

        internal static void OrdinaryFile(string path)
        {
            OrdinaryDirectory(Path.GetDirectoryName(path));
            var attributes = File.GetAttributes(path);
            if ((attributes & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0)
                throw new ClientError("找不到可用的正式入口文件。");
        }

        internal static string Sha256(string path)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (var hash = SHA256.Create())
                return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }

        internal static string DefaultTarget()
        {
            var drive = new DriveInfo("D");
            if (drive.IsReady && drive.DriveType == DriveType.Fixed)
                return @"D:\LiveMonitor";
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "LiveMonitor");
        }
    }

    internal sealed class Install
    {
        public const string ManifestSha = "__BUNDLE_MANIFEST_SHA256__";
        public const string SetupSha = "__CUSTOMER_SETUP_SHA256__";
        public static readonly string HelperName = "直播监控.exe";
        public static readonly string HomeName = "LiveMonitorDesktop";

        public static string Owner(string target, string expectedId)
        {
            var owner = Native.ReadDocument(Path.Combine(target, "owner.json"));
            string id = NativeResult.Text(owner, "install_id");
            Guid parsed;
            if (NativeResult.Text(owner, "kind") != "customer_installation_owner" ||
                !Guid.TryParseExact(id, "D", out parsed) || !string.Equals(id, parsed.ToString("D"), StringComparison.Ordinal) ||
                !Paths.Same(NativeResult.Text(owner, "target"), target) ||
                (expectedId != null && id != expectedId))
                throw new ClientError("无法确认这个安装目录的身份。请通过原安装包检查或联系部署人员。");
            return id;
        }

        public static string ValidateResult(string target, NativeResult result)
        {
            string code = NativeResult.Text(result.Output, "code");
            if (result.ExitCode != 0 || !NativeResult.Flag(result.Output, "ok") ||
                (code != "installed" && code != "already_installed") ||
                !Paths.Same(NativeResult.Text(result.Output, "target"), target) ||
                NativeResult.Text(result.Output, "install_id") == null)
                throw new ClientError("安装未确认完成。保留现场并联系部署人员。\r\n" + result.Diagnostic());
            string id = Owner(target, NativeResult.Text(result.Output, "install_id"));
            string installedManifest = Path.Combine(target, "installed.manifest.json");
            Paths.OrdinaryFile(installedManifest);
            if (Paths.Sha256(installedManifest) != ManifestSha)
                throw new ClientError("安装后的清单与这份安装包不一致。");
            return id;
        }

        public static void SeparateShortcutLocations(string target, string localAppData, params string[] shortcutFolders)
        {
            Paths.Separate(Path.Combine(localAppData, HomeName), target);
            foreach (string folder in shortcutFolders)
                if (!string.IsNullOrEmpty(folder) && Paths.Contains(target, folder))
                    throw new ClientError("安装位置必须与当前用户的桌面、开始菜单和启动助手目录分开。");
        }

        public static string Run(string target, string assemblyPath)
        {
            SeparateShortcutLocations(target, Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                Environment.GetFolderPath(Environment.SpecialFolder.Programs));
            string outside = Path.GetDirectoryName(assemblyPath);
            Paths.Separate(outside, target);
            string setup = Path.Combine(outside, "安装文件", "CustomerSetup.exe");
            if (!File.Exists(setup))
                throw new ClientError("在同一位置找不到「安装文件」。请完整解压分发包，再双击安装图标。");
            Paths.OrdinaryFile(setup);
            if (Paths.Sha256(setup) != SetupSha)
                throw new ClientError("安装入口的完整性核对失败。请重新取得可信的完整分发包。");
            var result = Native.Run(setup, "install", "--target", target, "--manifest-sha256", ManifestSha);
            return ValidateResult(target, result);
        }
    }

    internal static class Shortcut
    {
        private static string ConflictName(string name, string id)
        {
            return name + " (" + id.Substring(0, 8) + ")";
        }

        internal static string CopyHelper(string actual, string localAppData, string id)
        {
            Paths.OrdinaryFile(actual);
            Paths.OrdinaryDirectory(localAppData);
            string home = Path.Combine(localAppData, Install.HomeName);
            Directory.CreateDirectory(home);
            Paths.OrdinaryDirectory(home);
            string directory = Path.Combine(home, id);
            Directory.CreateDirectory(directory);
            Paths.OrdinaryDirectory(directory);
            string output = Path.Combine(directory, Install.HelperName);
            if (!File.Exists(output))
            {
                string temp = Path.Combine(directory, Guid.NewGuid().ToString("N") + ".pending");
                try
                {
                    using (var input = File.OpenRead(actual))
                    using (var pending = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                    {
                        input.CopyTo(pending);
                        pending.Flush(true);
                    }
                    try { File.Move(temp, output); }
                    catch (IOException) { if (!File.Exists(output)) throw; }
                }
                finally { if (File.Exists(temp)) File.Delete(temp); }
            }
            Paths.OrdinaryFile(output);
            if (Paths.Sha256(output) != Paths.Sha256(actual))
                throw new ClientError("当前用户目录里已有不同的启动助手。请联系部署人员。");
            return output;
        }

        private static bool OwnedLink(string file, string helper, string args)
        {
            if (!File.Exists(file) || (File.GetAttributes(file) & FileAttributes.ReparsePoint) != 0)
                return false;
            try
            {
                var link = ShellShortcut.Read(file);
                return Paths.Same(link.Target, helper) && link.Arguments == args;
            }
            catch (COMException) { return false; }
        }

        private static bool TryWrite(string file, string helper, string args, string description)
        {
            if (File.Exists(file) || Directory.Exists(file))
                return OwnedLink(file, helper, args);
            string temp = Path.Combine(Path.GetDirectoryName(file), ".LiveMonitor-" + Guid.NewGuid().ToString("N") + ".lnk");
            try
            {
                using (var claim = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None)) { }
                ShellShortcut.Write(temp, helper, args, description);
                if (!OwnedLink(temp, helper, args))
                    throw new ClientError("Windows 快捷方式写入后未通过回读检查。");
                try { File.Move(temp, file); }
                catch (IOException)
                {
                    if (!File.Exists(file) && !Directory.Exists(file))
                        throw;
                    return OwnedLink(file, helper, args);
                }
                return true;
            }
            finally { if (File.Exists(temp)) File.Delete(temp); }
        }

        internal static string Publish(string folder, string name, string helper, string mode, string target, string id)
        {
            if (string.IsNullOrWhiteSpace(folder) || !Directory.Exists(folder))
                throw new ClientError("Windows 的快捷方式目录当前不可用。");
            Paths.OrdinaryDirectory(folder);
            string args = Native.Arguments(mode, "--target", target, "--install-id", id);
            foreach (string label in new[] { name, ConflictName(name, id) })
            {
                string file = Path.Combine(folder, label + ".lnk");
                if (TryWrite(file, helper, args, name))
                    return label;
            }
            throw new ClientError("已存在其他应用的同名快捷方式，无法在此目录创建「" + name + "」。");
        }
    }

    internal sealed class OperationGate : IDisposable
    {
        private readonly Mutex mutex;

        private OperationGate(Mutex mutex)
        {
            this.mutex = mutex;
        }

        internal static OperationGate Enter(string scope, string identity)
        {
            string key;
            using (var hash = SHA256.Create())
                key = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(identity.ToUpperInvariant()))).Replace("-", "");
            var gate = new Mutex(false, @"Local\LiveMonitorDesktop-" + scope + "-" + key);
            try
            {
                if (!gate.WaitOne(0))
                    throw new ClientError("另一个窗口正在执行此操作。请切换到原窗口查看。");
                return new OperationGate(gate);
            }
            catch (AbandonedMutexException)
            {
                gate.ReleaseMutex();
                gate.Dispose();
                throw new ClientError("上一次操作的窗口意外退出。请保留现场并先检查当前状态。");
            }
            catch
            {
                gate.Dispose();
                throw;
            }
        }

        public void Dispose()
        {
            mutex.ReleaseMutex();
            mutex.Dispose();
        }
    }

    internal static class ShellShortcut
    {
        [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
        private class ShellLink { }

        // IShellLinkW is the Shell's Unicode interface. WScript.Shell's
        // argument round-trip can lose characters outside the ANSI code page.
        [ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IShellLink
        {
            void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count, IntPtr findData, uint flags);
            void GetIDList(out IntPtr list);
            void SetIDList(IntPtr list);
            void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
            void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
            void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
            void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
            void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count);
            void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string text);
            void GetHotkey(out short key);
            void SetHotkey(short key);
            void GetShowCmd(out int command);
            void SetShowCmd(int command);
            void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int count, out int index);
            void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
            void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
            void Resolve(IntPtr window, uint flags);
            void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
        }

        internal sealed class Details
        {
            internal string Target;
            internal string Arguments;
            internal string WorkingDirectory;
        }

        internal static Details Read(string file)
        {
            var instance = new ShellLink();
            try
            {
                var link = (IShellLink)instance;
                ((System.Runtime.InteropServices.ComTypes.IPersistFile)instance).Load(file, 0);
                var path = new StringBuilder(1024);
                var args = new StringBuilder(2048);
                var working = new StringBuilder(1024);
                link.GetPath(path, path.Capacity, IntPtr.Zero, 4);
                link.GetArguments(args, args.Capacity);
                link.GetWorkingDirectory(working, working.Capacity);
                return new Details { Target = path.ToString(), Arguments = args.ToString(), WorkingDirectory = working.ToString() };
            }
            finally { Marshal.FinalReleaseComObject(instance); }
        }

        internal static void Write(string file, string helper, string args, string description)
        {
            var instance = new ShellLink();
            try
            {
                var link = (IShellLink)instance;
                link.SetPath(helper);
                link.SetWorkingDirectory(Path.GetDirectoryName(helper));
                link.SetArguments(args);
                link.SetDescription(description);
                link.SetIconLocation(helper, 0);
                ((System.Runtime.InteropServices.ComTypes.IPersistFile)instance).Save(file, true);
            }
            finally { Marshal.FinalReleaseComObject(instance); }
        }
    }

    internal static class DesktopAction
    {
        private static readonly HashSet<string> StartWaiting = new HashSet<string>
        {
            "startup_timeout", "starting", "running", "waiting", "starting_or_identity_unknown",
            "stopping", "board_unavailable", "board_identity_unavailable", "gate_busy", "stopped"
        };

        internal static NativeResult Observe(string action, Func<string, NativeResult> invoke, Action wait,
            Func<bool> more, out bool reachedLater)
        {
            NativeResult result = invoke(action);
            var ready = action == "open" ? new Func<NativeResult, bool>(value => value.Ready)
                : value => value.Stopped;
            reachedLater = false;
            while (!ready(result) && more())
            {
                bool unsettled = action == "open"
                    ? StartWaiting.Contains(result.Signal)
                    : result.Signal == "stopping" || result.Signal == "running" || result.Signal == "waiting" || result.Signal == "gate_busy";
                if (!unsettled)
                    break;
                wait();
                result = invoke("status");
                reachedLater = true;
            }
            return result;
        }

        private static int Port(string target, NativeResult result)
        {
            var layout = Native.ReadDocument(Path.Combine(target, "installation.json"));
            var ports = NativeResult.Object(layout, "ports");
            var context = NativeResult.Object(result.Output, "context");
            object board, reported;
            if (ports == null || !ports.TryGetValue("board", out board) || !(board is int) ||
                (int)board < 1 || (int)board > 65535 || context == null ||
                !context.TryGetValue("port", out reported) || !(reported is int) || (int)board != (int)reported)
                throw new ClientError("看板已就绪，但无法确认它的本机端口。请联系部署人员。");
            return (int)board;
        }

        internal static void Execute(string action, string target, string expectedId, Action<string> update)
        {
            Install.Owner(target, expectedId);
            string launcher = Path.Combine(target, "entry", "CustomerLauncher.exe");
            var timer = Stopwatch.StartNew();
            bool reachedLater;
            var result = Observe(action, operation =>
            {
                if (operation == "open")
                    return Native.Run(launcher, "start", "--target", target, "--open");
                return Native.Run(launcher, operation, "--target", target);
            }, () =>
            {
                update(action == "open" ? "后台正在启动，正在确认看板状态…" : "正在等待读写工作收尾…");
                Thread.Sleep(1800);
            }, () => timer.Elapsed < TimeSpan.FromMinutes(3), out reachedLater);
            if (action == "open" && result.Ready)
            {
                if (reachedLater)
                {
                    int port = Port(target, result);
                    string url = "http://127.0.0.1:" + port.ToString(CultureInfo.InvariantCulture) + "/";
                    try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
                    catch (System.ComponentModel.Win32Exception)
                    {
                        throw new ClientError("看板已就绪。请在浏览器打开 " + url);
                    }
                }
                return;
            }
            if (action == "stop" && result.Stopped)
                return;
            string message = action == "open"
                ? "此时还未确认看板已启动。后台可能仍在运行，请保留安装目录。"
                : "此时还未确认后台已停止。请保留安装目录。";
            throw new ClientError(message + "\r\n" + result.Diagnostic());
        }
    }
}

