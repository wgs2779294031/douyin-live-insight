using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace LiveMonitorDesktop
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string self = Assembly.GetExecutingAssembly().Location;
            try
            {
                if (args.Length == 0)
                {
                    Application.Run(new ClientForm(self));
                    return;
                }
                Guid id;
                if (args.Length != 5 || (args[0] != "open" && args[0] != "stop") ||
                    args[1] != "--target" || args[3] != "--install-id" || !Guid.TryParseExact(args[4], "D", out id) ||
                    args[4] != id.ToString("D"))
                    throw new ClientError("请从安装后创建的桌面或开始菜单图标启动。");
                string expected = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    Install.HomeName, id.ToString("D"), Install.HelperName);
                if (!Paths.Same(self, expected))
                    throw new ClientError("这是分发包里的安装助手。请从已创建的图标启动。");
                Application.Run(new ClientForm(self, args[0], Paths.FullTarget(args[2]), args[4]));
            }
            catch (ClientError error)
            {
                MessageBox.Show(error.Message, "直播监控", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            catch (Exception)
            {
                MessageBox.Show("无法打开启动助手。请确认安装目录仍可访问，或联系部署人员。",
                    "直播监控", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }
    }

    internal sealed class ClientForm : Form
    {
        private readonly string self;
        private readonly string mode;
        private readonly string launchTarget;
        private readonly string launchId;
        private readonly TextBox targetText;
        private readonly Button browse;
        private readonly Button action;
        private readonly Label progressText;
        private readonly Label hint;
        private readonly ProgressBar progress;
        private readonly Stopwatch elapsed = new Stopwatch();
        private readonly Timer clock = new Timer();
        private readonly Label elapsedText;
        private bool busy;
        private bool completed;

        internal ClientForm(string self, string mode = "install", string target = null, string id = null)
        {
            this.self = self;
            this.mode = mode;
            launchTarget = target;
            launchId = id;
            bool installing = mode == "install";
            Text = installing ? "安装直播监控" : mode == "open" ? "启动直播监控" : "停止直播监控";
            ClientSize = new Size(660, installing ? 482 : 397);
            MinimumSize = new Size(600, installing ? 485 : 420);
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            MaximizeBox = false;
            Icon = SystemIcons.Application;
            BackColor = Color.White;
            Font = new Font("Microsoft YaHei UI", 9F);
            ForeColor = Color.FromArgb(27, 46, 67);

            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(24, 16, 24, 18), ColumnCount = 1 };
            Controls.Add(layout);
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            Add(layout, new Label { Text = Text, Dock = DockStyle.Fill,
                Font = new Font(Font.FontFamily, 17F, FontStyle.Bold), TextAlign = ContentAlignment.MiddleLeft }, 39);
            Add(layout, new Label { Text = installing ? "选一个本地目录，安装后会给当前 Windows 用户创建快捷方式。"
                : "安装位置 · " + launchTarget, Dock = DockStyle.Fill, AutoEllipsis = true,
                TextAlign = ContentAlignment.MiddleLeft }, 38);

            if (installing)
            {
                Add(layout, new Label { Text = "安装位置", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft }, 24);
                var pathRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Margin = new Padding(0) };
                pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
                pathRow.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 94));
                targetText = new TextBox { Name = "installTarget", Text = Paths.DefaultTarget(), Dock = DockStyle.Fill,
                    Margin = new Padding(3, 7, 10, 3), AccessibleName = "安装位置" };
                browse = new Button { Text = "浏览…", Dock = DockStyle.Fill, Margin = new Padding(0, 1, 0, 0) };
                browse.Click += (sender, args) => BrowseTarget();
                pathRow.Controls.Add(targetText, 0, 0);
                pathRow.Controls.Add(browse, 1, 0);
                Add(layout, pathRow, 40);
            }

            elapsedText = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleRight,
                ForeColor = Color.FromArgb(80, 100, 123) };
            Add(layout, elapsedText, 25);
            progress = new ProgressBar { Dock = DockStyle.Fill, MarqueeAnimationSpeed = 0,
                Style = ProgressBarStyle.Continuous, AccessibleName = "当前操作进度" };
            Add(layout, progress, 14);
            progressText = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft,
                Text = installing ? "请选择安装位置。" : "正在准备…", AutoEllipsis = false };
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            layout.Controls.Add(progressText);

            hint = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft,
                ForeColor = Color.FromArgb(74, 92, 115), Text = installing
                    ? "解压后运行此程序。需为安装和运行数据保留磁盘空间。"
                    : "看板和监控程序运行在这台 Windows 电脑上。" };
            Add(layout, hint, 42);
            action = new Button { Text = installing ? "安装并创建图标" : "关闭窗口", Width = 156,
                Height = 36, Anchor = AnchorStyles.Right, Margin = new Padding(3, 3, 0, 0) };
            Add(layout, action, 41);
            if (installing)
                AcceptButton = action;
            action.Click += (sender, args) =>
            {
                if (mode == "install" && !completed)
                    BeginInstall();
                else
                    Close();
            };
            clock.Interval = 1000;
            clock.Tick += (sender, args) => RefreshTime();
            FormClosing += ConfirmClose;
            FormClosed += (sender, args) => clock.Dispose();
            if (!installing)
                Shown += (sender, args) => BeginDesktopAction();
        }

        private static void Add(TableLayoutPanel parent, Control child, int height)
        {
            parent.RowStyles.Add(new RowStyle(SizeType.Absolute, height));
            parent.Controls.Add(child);
        }

        private void BrowseTarget()
        {
            using (var choose = new FolderBrowserDialog
            {
                Description = "选择一个新安装目录，或选择已安装这版直播监控的目录。",
                ShowNewFolderButton = true
            })
            {
                try
                {
                    string preferred = targetText.Text;
                    choose.SelectedPath = Directory.Exists(preferred) ? preferred : Path.GetDirectoryName(preferred);
                }
                catch (ArgumentException) { }
                if (choose.ShowDialog(this) == DialogResult.OK)
                    targetText.Text = choose.SelectedPath;
            }
        }

        private void ChangeStatus(string message)
        {
            if (IsDisposed || !IsHandleCreated)
                return;
            try
            {
                BeginInvoke((Action)(() => { if (!IsDisposed) progressText.Text = message; }));
            }
            catch (InvalidOperationException) { }
        }

        private void RefreshTime()
        {
            if (busy)
                elapsedText.Text = "已等待 " + elapsed.Elapsed.ToString(@"mm\:ss");
        }

        internal void ShowWaiting(string message)
        {
            busy = true;
            progress.Style = ProgressBarStyle.Marquee;
            progress.MarqueeAnimationSpeed = 35;
            progressText.Text = message;
            hint.Text = "可以最小化此窗口。操作完成前，关闭按钮会将窗口最小化。";
            elapsed.Restart();
            RefreshTime();
            clock.Start();
            if (mode == "install")
            {
                targetText.ReadOnly = true;
                browse.Enabled = false;
                action.Enabled = false;
            }
        }

        private void Complete(string message, string nextStep, bool installationFinished)
        {
            busy = false;
            elapsed.Stop();
            clock.Stop();
            progress.Style = ProgressBarStyle.Continuous;
            progress.Value = installationFinished ? 100 : 0;
            progressText.Text = message;
            hint.Text = nextStep;
            completed = installationFinished;
            action.Enabled = true;
            action.Text = completed || mode != "install" ? "关闭窗口" : "安装并创建图标";
            if (mode == "install" && !completed)
            {
                targetText.ReadOnly = false;
                browse.Enabled = true;
            }
        }

        private async void BeginInstall()
        {
            if (busy)
                return;
            string target;
            try { target = Paths.FullTarget(targetText.Text.Trim()); }
            catch (ClientError error)
            {
                Complete(error.Message, "修改安装位置后再试。", false);
                return;
            }
            catch (Exception)
            {
                Complete("无法访问安装位置。请选择本机磁盘上可访问的短路径。", "修改安装位置后再试。", false);
                return;
            }
            targetText.Text = target;
            ShowWaiting("正在核验并安装直播监控。机械硬盘上可能需要较长时间。");
            try
            {
                var outcome = await Task.Run(() => InstallAndRegister(target));
                if (IsDisposed)
                    return;
                if (outcome.Error == null)
                    Complete("已安装到 " + target + "。\r\n" + outcome.Labels, "使用桌面或开始菜单图标开关程序。", true);
                else
                    Complete("程序已安装，但快捷方式未全部创建。\r\n" + outcome.Error,
                        "可重新打开安装助手选择相同目录补建图标，或按指南进阶命令运行。", true);
            }
            catch (ClientError error)
            {
                if (!IsDisposed)
                    Complete(error.Message, "保留当前安装文件和目标目录供排查。", false);
            }
            catch (Exception)
            {
                if (!IsDisposed)
                    Complete("安装没有返回可确认的结果。请检查文件、目录权限或联系部署人员。",
                        "保留当前安装文件和目标目录供排查。", false);
            }
        }

        private sealed class ShortcutOutcome
        {
            internal string Labels;
            internal string Error;
        }

        private ShortcutOutcome InstallAndRegister(string target)
        {
            using (OperationGate.Enter("install", target))
            {
                string id = Install.Run(target, self);
                ChangeStatus("安装完成，正在为当前用户创建快捷方式…");
                try { return new ShortcutOutcome { Labels = PublishShortcuts(target, id) }; }
                catch (ClientError error) { return new ShortcutOutcome { Error = error.Message }; }
                catch (Exception) { return new ShortcutOutcome { Error = "Windows 未能创建全部快捷方式。" }; }
            }
        }

        private string PublishShortcuts(string target, string id)
        {
            var failures = new List<string>();
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
            string startMenu = Environment.GetFolderPath(Environment.SpecialFolder.Programs);
            Install.SeparateShortcutLocations(target, localAppData, desktop, startMenu);
            string helper = Shortcut.CopyHelper(self, localAppData, id);
            string desktopLabels = null;
            foreach (var knownFolder in new[] { Environment.SpecialFolder.DesktopDirectory, Environment.SpecialFolder.Programs })
            {
                string label = knownFolder == Environment.SpecialFolder.Programs ? "开始菜单" : "桌面";
                try
                {
                    string folder = Environment.GetFolderPath(knownFolder);
                    string open = Shortcut.Publish(folder, "直播监控", helper, "open", target, id);
                    string stop = Shortcut.Publish(folder, "停止直播监控", helper, "stop", target, id);
                    if (knownFolder == Environment.SpecialFolder.DesktopDirectory)
                        desktopLabels = "桌面图标 · " + open + " / " + stop;
                }
                catch (Exception)
                {
                    failures.Add(label);
                }
            }
            if (failures.Count != 0)
                throw new ClientError(string.Join("、", failures) + "有图标未创建。已存在的其他图标保持原样。");
            return desktopLabels;
        }

        private async void BeginDesktopAction()
        {
            ShowWaiting(mode == "open" ? "正在启动本地看板…" : "正在停止监控，等待读写工作收尾…");
            try
            {
                await Task.Run(() =>
                {
                    using (OperationGate.Enter("desktop", launchId))
                        DesktopAction.Execute(mode, launchTarget, launchId, ChangeStatus);
                });
                if (IsDisposed)
                    return;
                busy = false;
                if (mode == "open")
                    Close();
                else
                    Complete("直播监控已停止。", "以后可从桌面或开始菜单重新启动。", true);
            }
            catch (ClientError error)
            {
                if (!IsDisposed)
                    Complete(error.Message, "请保留安装目录。需要时按安装指南联系部署人员。", false);
            }
            catch (Exception)
            {
                if (!IsDisposed)
                    Complete("无法访问正式启动入口或取得运行结果。", "请确认安装目录未移动，再按指南检查。", false);
            }
        }

        private void ConfirmClose(object sender, FormClosingEventArgs args)
        {
            if (!busy || args.CloseReason == CloseReason.WindowsShutDown)
                return;
            args.Cancel = true;
            WindowState = FormWindowState.Minimized;
        }
    }
}

