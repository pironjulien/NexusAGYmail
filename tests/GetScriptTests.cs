using System.Diagnostics;
using System.IO.Compression;
using System.Text.Json.Nodes;

namespace NexusAGYmail.Tests;

/// <summary>The public installer get.ps1 on a local package (NEXUS_AGYMAIL_SOURCE) in a temporary home and install
/// folder, under PowerShell 7 and Windows PowerShell 5.1: install, Antigravity plugin, Nexus pack line, removal. Never
/// the network, the real home or the real Antigravity.</summary>
public sealed class GetScriptTests : IDisposable
{
    private readonly string root = Support.NewTemp("get");
    private string Home => Path.Join(root, "home");
    private string Apps => Path.Join(root, "app");
    private string Zip => Path.Join(root, "NexusAGYmail-win-x64.zip");

    public GetScriptTests()
    {
        Directory.CreateDirectory(Path.Join(Home, ".gemini", "config"));
        // The engine the tests run, with what it needs next to it (framework-dependent here, self-contained in a release).
        string engine = Path.GetDirectoryName(Path.GetFullPath(Support.Exe))!;
        using var zip = ZipFile.Open(Zip, ZipArchiveMode.Create);
        foreach (string file in Directory.GetFiles(engine).Where(f => !Path.GetFileName(f).StartsWith("NexusAGYmail.Tests", StringComparison.OrdinalIgnoreCase)))
            zip.CreateEntryFromFile(file, Path.GetFileName(file));
    }

    public void Dispose() => Support.Delete(root);

    private (int Code, string Out) Get(string shell, params string[] args)
    {
        var info = new ProcessStartInfo(shell)
        {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
            RedirectStandardInput = true,
        };
        foreach (string a in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", Path.Join(Support.Repository, "get.ps1") }.Concat(args))
            info.ArgumentList.Add(a);
        foreach (var (key, value) in Support.IsolatedEnvironment(Home))
        {
            if (value is null) info.Environment.Remove(key);
            else info.Environment[key] = value;
        }
        info.Environment["NEXUS_AGYMAIL_SOURCE"] = Zip;
        info.Environment["NEXUS_AGYMAIL_ROOT"] = Apps;
        info.Environment["NEXUS_PACK"] = "1";
        using var process = Process.Start(info)!;
        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        Assert.True(process.WaitForExit(120_000), "get.ps1 did not finish");
        return (process.ExitCode, stdout.Result + stderr.Result);
    }

    [Theory]
    [InlineData("pwsh")]
    [InlineData("powershell")]
    public void Installs_declares_and_removes(string shell)
    {
        var (code, output) = Get(shell);
        Assert.True(code == 0, output);
        Assert.Equal("NEXUS_PACK_RESULT Antigravity=plugin; sign-in=to do", output.Trim().Split('\n')[^1].Trim());
        string installed = Path.Join(Apps, Program.Version, "NexusAGYmail.exe");
        Assert.True(File.Exists(installed), output);
        string config = Path.Join(Home, ".gemini", "config", "plugins", "nexus-agymail", "mcp_config.json");
        Assert.Equal(installed, JsonNode.Parse(File.ReadAllText(config))!["mcpServers"]!["nexus-agymail"]!["command"]!.GetValue<string>(), ignoreCase: true);

        Assert.True(Get(shell).Code == 0, "a second run (update) must succeed");
        Assert.Single(Directory.GetDirectories(Apps));

        (code, output) = Get(shell, "-Remove");
        Assert.True(code == 0, output);
        Assert.False(File.Exists(config));
        Assert.False(Directory.Exists(Apps));
    }

    [Theory]
    [InlineData("pwsh")]
    [InlineData("powershell")]
    public void A_scanner_briefly_holding_the_fresh_executable_does_not_fail_the_installation(string shell)
    {
        // What an antivirus does to a freshly written executable: opens it for a moment (every sharing mode allowed),
        // which blocks renaming its folder. get.ps1 waits that hold out instead of failing ("being used by another
        // process", seen once in NexusSystem's pack job on a fresh Windows runner).
        string? held = null;
        bool stagedWhileHeld = false;
        using var released = new ManualResetEventSlim();
        using var watcher = new FileSystemWatcher(root, "NexusAGYmail.exe") { IncludeSubdirectories = true };
        var scanner = new Thread(() =>
        {
            string path = watcher.WaitForChanged(WatcherChangeTypes.Created, 60_000) is { TimedOut: false } change ? Path.Join(root, change.Name) : "";
            for (int i = 0; i < 400 && held is null && path.Length > 0; i++)
            {
                try
                {
                    using var scan = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                    held = path;
                    Thread.Sleep(2_000);
                    stagedWhileHeld = File.Exists(path);
                }
                catch (IOException) { Thread.Sleep(5); }  // still being extracted
            }
            released.Set();
        }) { IsBackground = true };
        watcher.EnableRaisingEvents = true;
        scanner.Start();

        var (code, output) = Get(shell);
        Assert.True(released.Wait(60_000, TestContext.Current.CancellationToken));
        Assert.True(code == 0, output);
        Assert.NotNull(held);
        Assert.Contains("staging-", held, StringComparison.OrdinalIgnoreCase);
        Assert.True(stagedWhileHeld, "the hold must have overlapped the staged folder");
        Assert.True(File.Exists(Path.Join(Apps, Program.Version, "NexusAGYmail.exe")), output);
        Assert.Empty(Directory.GetDirectories(root, "staging-*"));
    }

    /// <summary>Every file of a folder with its SHA-256, by relative name.</summary>
    private static string Snapshot(string folder) => string.Join('\n', Directory.GetFiles(folder, "*", SearchOption.AllDirectories)
        .Order(StringComparer.OrdinalIgnoreCase)
        .Select(f => Path.GetRelativePath(folder, f) + "|" + Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(f)))));

    [Theory]
    [InlineData("pwsh")]
    [InlineData("powershell")]
    public async Task Servers_started_before_an_update_keep_working_after_it(string shell)
    {
        // What broke NexusFree 1.3.3's open sessions: an engine loads code from its own files after it starts (lazily
        // loaded assemblies, or a single-file bundle reading itself), so an update must never change, remove or replace
        // a file of a folder a server runs from. Two servers run while get.ps1 updates: one from the installed version
        // (the same build reinstalled, then another build of that version), one from an older version folder.
        var (code, output) = Get(shell);
        Assert.True(code == 0, output);
        string current = Path.Join(Apps, Program.Version);
        string older = Path.Join(Apps, "0.0.1");
        Directory.CreateDirectory(older);
        foreach (string file in Directory.GetFiles(current))
            File.Copy(file, Path.Join(older, Path.GetFileName(file)));

        await using var fake = new FakeGmail();
        fake.Received("m1", "Un texte accentué é", "Premier");
        NativeServer Start(string folder)
        {
            string home = Path.Join(root, "server-" + Path.GetFileName(folder));
            string state = Path.Join(home, ".nexus", "nexus-agymail");
            Directory.CreateDirectory(state);
            File.WriteAllText(Path.Join(state, "token.json"), Support.TestToken(FakeGmail.NewScopes).ToJsonString());
            var environment = Support.IsolatedEnvironment(home);
            environment["NEXUS_AGYMAIL_API_BASE"] = fake.ApiBase;
            environment["NEXUS_AGYMAIL_TOKEN_URI"] = fake.BaseUrl + "/never-refresh";
            var server = new NativeServer(environment, Path.Join(folder, "NexusAGYmail.exe"));
            server.Rpc("initialize", new JsonObject { ["protocolVersion"] = "2025-06-18", ["clientInfo"] = new JsonObject { ["name"] = "Antigravity" }, ["capabilities"] = new JsonObject() });
            return server;
        }

        string currentBefore = Snapshot(current), olderBefore = Snapshot(older);
        using (var onCurrent = Start(current))
        using (var onOlder = Start(older))
        {
            (code, output) = Get(shell);
            Assert.True(code == 0, "reinstalling the same build must succeed: " + output);
            // Another build of the same version (a development release): an extra file makes it differ.
            using (var zip = ZipFile.Open(Zip, ZipArchiveMode.Update))
                using (var writer = new StreamWriter(zip.CreateEntry("build.txt").Open())) writer.Write("another build");
            (code, output) = Get(shell);
            Assert.True(code == 0, output);

            Assert.Equal(currentBefore, Snapshot(current));
            Assert.Equal(olderBefore, Snapshot(older));
            foreach (var server in new[] { onCurrent, onOlder })
            {
                Assert.Equal("success", server.Call("search_threads")["status"]?.GetValue<string>());
                Assert.Contains("Premier", server.Call("get_message", new JsonObject { ["messageId"] = "m1" })["message"]!["subject"]!.GetValue<string>());
            }
        }

        // Once those servers stopped, the next run installs the other build and removes the older version.
        (code, output) = Get(shell);
        Assert.True(code == 0, output);
        Assert.True(File.Exists(Path.Join(current, "build.txt")), output);
        Assert.False(Directory.Exists(older));
        Assert.Empty(Directory.GetDirectories(root, "retired-*"));
    }

    [Fact]
    public void Docs_and_scripts_never_run_downloaded_code_in_memory()
    {
        // Microsoft Defender flags these forms (Trojan:Win32/Commando.A!ml): install by files and -File only.
        var forbidden = new System.Text.RegularExpressions.Regex(@"\birm\b|\biex\b|Invoke-Expression|scriptblock\]::Create|EncodedCommand", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        var files = new[] { "README.md", "get.ps1", "install.ps1", "test.ps1", Path.Join("tools", "package.ps1") }
            .Select(f => Path.Join(Support.Repository, f)).Where(File.Exists)
            .Concat(Directory.GetFiles(Path.Join(Support.Repository, ".github", "workflows")));
        foreach (string file in files)
        {
            var lines = File.ReadAllLines(file).Select((l, i) => (l, i)).Where(x => forbidden.IsMatch(x.l)).ToList();
            Assert.True(lines.Count == 0, $"{file}:{(lines.Count > 0 ? lines[0].i + 1 : 0)}: {(lines.Count > 0 ? lines[0].l : "")}");
        }
    }

    [Fact]
    public void A_test_package_never_installs_for_real()
    {
        var info = new ProcessStartInfo("pwsh") { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (string a in new[] { "-NoProfile", "-File", Path.Join(Support.Repository, "get.ps1") })
            info.ArgumentList.Add(a);
        info.Environment["USERPROFILE"] = Home;
        info.Environment["NEXUS_AGYMAIL_SOURCE"] = Zip;
        info.Environment.Remove("NEXUS_AGYMAIL_ROOT");
        using var process = Process.Start(info)!;
        string output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
        process.WaitForExit();
        Assert.Equal(1, process.ExitCode);
        Assert.Contains("refusing to install for real", output);
        Assert.False(Directory.Exists(Path.Join(Home, ".nexus", "nexus-agymail", "app")));
    }
}
