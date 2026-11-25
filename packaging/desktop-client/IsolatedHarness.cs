using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace LiveMonitorDesktop
{
    internal static class Harness
    {
        private const string Id = "7d24c997-e72d-40c3-b0d2-b83fa672bf2a";
        private const string RawSecret = "PRIVATE stderr field deliberately excluded";
        private static readonly List<string> passed = new List<string>();
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        [STAThread]
        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args[0].StartsWith("fake-"))
                return Fake(args);
            try
            {
                Run(args[0], args[1]);
                Console.WriteLine(Json.Serialize(new { ok = true, cases = passed, genuine_install = false }));
                return 0;
            }
            catch (Exception error)
            {
                Console.WriteLine(Json.Serialize(new { ok = false, cases = passed, failure = error.ToString() }));
                return 1;
            }
        }

        private static int Fake(string[] args)
        {
            if (args[0] == "fake-deny")
            {
                Console.Error.WriteLine(Json.Serialize(new { ok = false, code = "holders_unconfirmed", secret = RawSecret }));
                return 75;
            }
            if (args[0] == "fake-pipes")
            {
                Console.Error.Write(new string('x', 512 * 1024));
                Console.WriteLine("{\"ok\":true,\"state\":\"running\",\"board_ready\":true}");
                return 0;
            }
            string code = args[0] == "fake-already" ? "already_installed" : "installed";
            string id = args[0] == "fake-wrongid" ? Guid.Empty.ToString("D") : Id;
            Console.WriteLine(Json.Serialize(new { ok = true, code = code, install_id = id, target = args[1] }));
            return args[0] == "fake-nonzero" ? 78 : 0;
        }

        private static void Check(bool condition, string operation)
        {
            if (!condition)
                throw new InvalidOperationException(operation);
        }

        private static void Reject(Action action, string errorMessage = null)
        {
            try { action(); }
            catch (ClientError error)
            {
                Check(errorMessage == null || error.Message.Contains(errorMessage), "wrong rejection: " + error.Message);
                return;
            }
            throw new InvalidOperationException("unsafe admission");
        }

        private static void Run(string scratch, string bundle)
        {
            string self = Assembly.GetExecutingAssembly().Location;
            Directory.CreateDirectory(scratch);
            Check(Path.GetFileName(scratch).StartsWith("proof-"), "only fresh proof roots");
            Check(Directory.GetFileSystemEntries(scratch).Length == 0, "proof root must be empty");
            string parent = Directory.CreateDirectory(Path.Combine(scratch, "本地 $ 路径")).FullName;
            string target = Path.Combine(parent, "客户 🐟 测试");
            string local = Directory.CreateDirectory(Path.Combine(scratch, "AppData")).FullName;
            string desktop = Directory.CreateDirectory(Path.Combine(scratch, "重定向桌面")).FullName;
            string start = Directory.CreateDirectory(Path.Combine(scratch, "开始菜单")).FullName;
            Check(Paths.FullTarget(target) == target, "Unicode path and spaces");
            Reject(() => Paths.FullTarget(@"C:\x\..\z"));
            Reject(() => Paths.FullTarget(@"C:\NUL.txt"));
            Reject(() => Paths.FullTarget(@"C:\invalid. "));
            Reject(() => Paths.FullTarget(@"\\unknown\shared\test"));
            Reject(() => Paths.FullTarget(Path.Combine(parent, new string('x', 120))));
            Reject(() => Install.SeparateShortcutLocations(Path.Combine(local, Install.HomeName, "overlay"), local));
            Reject(() => Install.SeparateShortcutLocations(local, local));
            Reject(() => Install.SeparateShortcutLocations(desktop, local, desktop, start));
            Install.SeparateShortcutLocations(Path.Combine(desktop, "独立安装目录"), local, desktop, start);
            passed.Add("short_local_unicode_paths_and_disjoint_scope");

            Directory.CreateDirectory(target);
            File.WriteAllText(Path.Combine(target, "owner.json"),
                Json.Serialize(new { kind = "customer_installation_owner", schema = 1, target = target, install_id = Id }));
            using (var archive = ZipFile.OpenRead(bundle))
            using (var input = archive.GetEntry("bundle.manifest.json").Open())
            using (var output = File.Create(Path.Combine(target, "installed.manifest.json")))
                input.CopyTo(output);
            Check(Install.ValidateResult(target, Native.Run(self, "fake-success", target)) == Id, "installed result");
            Check(Install.ValidateResult(target, Native.Run(self, "fake-already", target)) == Id, "idempotent result");
            Reject(() => Install.ValidateResult(target, Native.Run(self, "fake-wrongid", target)), "无法确认");
            Reject(() => Install.ValidateResult(target, Native.Run(self, "fake-nonzero", target)), "退出码 78");
            Reject(() => Install.ValidateResult(target, Native.Run(self, "fake-success", target + "错误")), "安装未确认");
            var denied = Native.Run(self, "fake-deny");
            Check(denied.ExitCode == 75 && denied.Signal == "holders_unconfirmed", "stderr denial recognized");
            Check(denied.Diagnostic().Contains("holders_unconfirmed") && !denied.Diagnostic().Contains(RawSecret),
                "display only bounded status codes, excludes sensitive fields");
            Check(Native.Run(self, "fake-pipes").Ready, "drain both redirected pipes concurrently");
            passed.Add("native_exit_receipt_owner_manifest_and_bounded_pipes");

            string helper1 = null, helper2 = null;
            Parallel.Invoke(() => helper1 = Shortcut.CopyHelper(self, local, Id),
                            () => helper2 = Shortcut.CopyHelper(self, local, Id));
            Check(Paths.Same(helper1, helper2) && Paths.Sha256(helper1) == Paths.Sha256(self), "same-id helper copy");
            string other = Path.Combine(scratch, "different.exe");
            File.Copy(self, other);
            File.AppendAllText(other, "different");
            Reject(() => Shortcut.CopyHelper(other, local, Id), "已有不同的");
            passed.Add("concurrent_helper_publish_and_preserve_other_content");

            string original = Path.Combine(desktop, "直播监控.lnk");
            File.WriteAllText(original, "KEEP FOREIGN SHORTCUT", new UTF8Encoding(false));
            string sentinel = Paths.Sha256(original);
            string link1 = null, link2 = null;
            Parallel.Invoke(() => link1 = Shortcut.Publish(desktop, "直播监控", helper1, "open", target, Id),
                            () => link2 = Shortcut.Publish(desktop, "直播监控", helper1, "open", target, Id));
            Check(link1 == link2 && link1 == "直播监控 (7d24c997)", "collision fallback idempotent under concurrency");
            Check(Paths.Sha256(original) == sentinel, "leave foreign shortcut untouched");
            Check(Shortcut.Publish(start, "直播监控", helper1, "open", target, Id) == "直播监控", "start menu creation");
            Check(Shortcut.Publish(start, "停止直播监控", helper1, "stop", target, Id) == "停止直播监控", "stop shortcut");
            VerifyLink(Path.Combine(desktop, link1 + ".lnk"), helper1, "open", target);
            VerifyLink(Path.Combine(start, "停止直播监控.lnk"), helper1, "stop", target);
            Check(Directory.GetFiles(desktop, ".LiveMonitor-*").Length + Directory.GetFiles(start, ".LiveMonitor-*").Length == 0,
                "temporary shortcut cleanup");
            passed.Add("real_lnk_redirected_paths_unicode_conflict_reuse_and_parallel_publication");

            using (OperationGate.Enter("desktop", Id))
                Task.Run(() => Reject(() => { using (OperationGate.Enter("desktop", Id.ToUpperInvariant())) { } }, "另一个窗口")).Wait();
            using (OperationGate.Enter("desktop", Id)) { }
            using (OperationGate.Enter("install", target))
                Task.Run(() => Reject(() => { using (OperationGate.Enter("install", target.ToUpperInvariant())) { } }, "另一个窗口")).Wait();
            passed.Add("concurrent_gui_operations_do_not_duplicate_native_requests");

            var calls = new List<string>();
            bool late;
            var opened = DesktopAction.Observe("open", action =>
            {
                calls.Add(action);
                return calls.Count == 1 ? State(false, "startup_timeout", false, 1) :
                    calls.Count == 2 ? State(true, "waiting", false, 0) : State(true, "running", true, 0);
            }, () => { }, () => calls.Count < 4, out late);
            Check(opened.Ready && late && string.Join(",", calls) == "open,status,status", "do not start twice");
            calls.Clear();
            var stopped = DesktopAction.Observe("stop", action =>
            {
                calls.Add(action);
                return calls.Count == 1 ? State(false, "stopping", false, 1) : State(true, "stopped", false, 0);
            }, () => { }, () => calls.Count < 3, out late);
            Check(stopped.Stopped && late && string.Join(",", calls) == "stop,status", "do not stop twice");
            var conflict = DesktopAction.Observe("open", action =>
                State(false, "port_conflict", false, 1), () => Check(false, "port conflict should not poll"),
                () => true, out late);
            Check(!conflict.Ready && !late && conflict.Signal == "port_conflict", "port conflict preserved");
            passed.Add("cold_start_and_graceful_stop_only_poll_status");

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var form = new ClientForm(self))
            {
                Draw(form, Path.Combine(scratch, "install-ui.png"));
                form.ShowWaiting("正在核验并安装直播监控。机械硬盘上可能需要较长时间。");
                Draw(form, Path.Combine(scratch, "waiting-ui.png"));
            }
            passed.Add("form_layout_snapshots_without_show_or_customer_operation");
        }

        private static NativeResult State(bool ok, string state, bool ready, int exit)
        {
            return new NativeResult { ExitCode = exit, Output = new Dictionary<string, object>
                { { "ok", ok }, { "state", state }, { "board_ready", ready } } };
        }

        private static void VerifyLink(string path, string helper, string mode, string target)
        {
            var link = ShellShortcut.Read(path);
            Check(Paths.Same(link.Target, helper), "lnk points to durable helper");
            Check(link.Arguments == Native.Arguments(mode, "--target", target, "--install-id", Id), "lnk Unicode argument quoting");
            Check(Paths.Same(link.WorkingDirectory, Path.GetDirectoryName(helper)), "lnk independent of extraction location");
        }

        private static void Draw(Form form, string file)
        {
            var layout = (TableLayoutPanel)form.Controls[0];
            form.Controls.Remove(layout);
            try
            {
                layout.Size = form.ClientSize;
                layout.CreateControl();
                layout.PerformLayout();
                foreach (Control child in layout.Controls)
                {
                    Check(child.Bottom <= layout.Height && child.Height > 0, "row exceeds visible layout area");
                    child.CreateControl();
                    child.PerformLayout();
                }
                using (var bitmap = new Bitmap(layout.Width, layout.Height))
                {
                    layout.DrawToBitmap(bitmap, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
                    bitmap.Save(file, ImageFormat.Png);
                }
            }
            finally { form.Controls.Add(layout); }
        }
    }
}

