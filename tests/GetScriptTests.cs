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
