using System.Reflection;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusAGYmail.Tests;

/// <summary>The executable as users and installers run it: contracts, version, `setup`, `status`, `auth` without client.</summary>
public sealed class CommandLineTests : IDisposable
{
    private static readonly string[] ExpectedTools =
    [
        "auth_status", "search_threads", "get_message", "get_thread", "list_new_messages", "download_attachment",
        "create_draft", "list_labels", "create_label", "list_drafts", "get_draft", "update_draft", "send_draft",
        "label_message", "unlabel_message", "label_thread", "unlabel_thread", "trash_message", "trash_thread",
        "untrash_message", "untrash_thread",
    ];

    private readonly string home = Support.NewTemp("cli");
    private readonly Dictionary<string, string?> environment;

    public CommandLineTests() => environment = Support.IsolatedEnvironment(home);
    public void Dispose() => Support.Delete(home);

    private string Gemini => Path.Combine(home, ".gemini", "config");
    private string PluginConfig => Path.Combine(Gemini, "plugins", "nexus-agymail", "mcp_config.json");

    [Fact]
    public void Tools_list_over_stdio_matches_the_embedded_contracts()
    {
        using var server = new NativeServer(environment);
        var init = server.Rpc("initialize", new JsonObject { ["protocolVersion"] = "2025-06-18", ["clientInfo"] = new JsonObject { ["name"] = "test" } });
        Assert.Equal("nexus-agymail", init["result"]!["serverInfo"]!["name"]!.GetValue<string>());
        Assert.Equal("2025-06-18", init["result"]!["protocolVersion"]!.GetValue<string>());
        var tools = server.Rpc("tools/list")["result"]!["tools"]!.AsArray();
        var contracts = JsonNode.Parse(File.ReadAllText(Path.Combine(Support.Repository, "native", "tool-contracts.json")))!;
        Assert.True(JsonNode.DeepEquals(contracts, tools), "tools/list differs from native/tool-contracts.json");
        Assert.Equal(ExpectedTools.Order(), tools.Select(t => t!["name"]!.GetValue<string>()).Order());
        foreach (var tool in tools)
        {
            Assert.NotNull(tool!["inputSchema"]);
            Assert.NotNull(tool["annotations"]);
            Assert.False(string.IsNullOrWhiteSpace(tool["description"]?.GetValue<string>()));
        }
    }

    [Fact]
    public void One_version_everywhere()
    {
        string root = Support.Repository;
        Assert.Equal($"NexusAGYmail {Program.Version}", Support.Run(environment, "--version").Out.Trim());
        var csproj = Regex.Match(File.ReadAllText(Path.Combine(root, "native", "NexusAGYmail.csproj")), "<Version>([^<]+)</Version>");
        Assert.Equal(Program.Version, csproj.Groups[1].Value);
        var plugin = JsonNode.Parse(File.ReadAllText(Path.Combine(root, "native", "plugin", "plugin.json")))!;
        Assert.Equal(Program.Version, plugin["version"]!.GetValue<string>());
        Assert.Contains($"(nexus-agymail {Program.Version})", File.ReadAllText(Path.Combine(root, "native", "plugin", "skills", "nexus-agymail", "SKILL.md")));
        var changelog = Regex.Match(File.ReadAllText(Path.Combine(root, "CHANGELOG.md")), @"^## \[(\d+\.\d+\.\d+)\]", RegexOptions.Multiline);
        Assert.Equal(Program.Version, changelog.Groups[1].Value);
        var server = JsonNode.Parse(File.ReadAllText(Path.Combine(root, "server.json")))!;
        Assert.Equal(Program.Version, server["version"]!.GetValue<string>());
        Assert.Contains($"/releases/download/v{Program.Version}/", server["packages"]![0]!["identifier"]!.GetValue<string>());
    }

    [Fact]
    public void The_shared_guardrail_policy_is_embedded_unchanged()
    {
        string root = Support.Repository;
        var manifest = File.ReadAllLines(Path.Combine(root, "vendored.sha256")).Where(l => l.Trim().Length > 0)
            .Select(l => l.Split("  ", 2)).ToDictionary(p => p[1], p => p[0]);
        Assert.Equal(["native/garde-fous.json"], manifest.Keys);
        byte[] bytes = File.ReadAllBytes(Path.Combine(root, "native", "garde-fous.json"));
        string lf = Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(System.Text.Encoding.UTF8.GetString(bytes).ReplaceLineEndings("\n"))));
        Assert.Equal(manifest["native/garde-fous.json"], lf);
        using var embedded = typeof(Program).Assembly.GetManifestResourceStream("garde-fous.json")!;
        using var copy = new MemoryStream();
        embedded.CopyTo(copy);
        Assert.Equal(bytes, copy.ToArray());
    }

    [Fact]
    public void Status_without_token_is_json_and_fails()
    {
        var (code, output, _) = Support.Run(environment, "status");
        Assert.Equal(1, code);
        var report = JsonNode.Parse(output)!;
        Assert.False(report["present"]!.GetValue<bool>());
        Assert.Contains("auth", report["message"]!.GetValue<string>());
    }

    [Fact]
    public void Auth_without_client_outside_a_console_explains_and_fails()
    {
        var (code, _, error) = Support.Run(environment, "auth", "--no-browser");
        Assert.Equal(1, code);
        Assert.Contains("--client-secret", error);
        environment["NEXUS_AGYMAIL_CLIENT_SECRET"] = Path.Combine(home, "absent.json");
        (code, _, error) = Support.Run(environment, "auth", "--no-browser");
        Assert.Equal(1, code);
        Assert.Contains("NEXUS_AGYMAIL_CLIENT_SECRET points to a missing file", error);
        Assert.Equal(2, Support.Run(environment, "auth", "--bogus").Code);
        Assert.Equal(2, Support.Run(environment, "nonsense").Code);
    }

    [Fact]
    public void Setup_without_antigravity_declares_nothing()
    {
        environment["NEXUS_PACK"] = "1";
        var (code, output, _) = Support.Run(environment, "setup");
        Assert.Equal(0, code);
        Assert.Contains("Antigravity not found", output);
        Assert.Equal("NEXUS_PACK_RESULT sign-in=to do", output.Trim().Split('\n')[^1].Trim());
        Assert.False(Directory.Exists(Path.Combine(home, ".gemini")));
    }

    [Fact]
    public void Setup_writes_the_plugin_replaces_old_entries_and_keeps_env()
    {
        Directory.CreateDirectory(Path.Combine(Gemini, "plugins", "nexus-agymail"));
        // An earlier public install (uvx) in the plugin, and an old global entry of ours next to someone else's server.
        File.WriteAllText(PluginConfig, """{"mcpServers":{"nexus-agymail":{"command":"C:\\u\\uvx.exe","args":["--python","3.14","nexus-agymail@latest"],"env":{"NEXUS_AGYMAIL_PROTECTED":"D:\\vault"}}}}""");
        File.WriteAllText(Path.Combine(Gemini, "mcp_config.json"), """{"mcpServers":{"nexus-agymail":{"command":"uvx","args":["nexus-agymail"]},"other":{"command":"x"}}}""");
        environment["NEXUS_PACK"] = "1";
        var (code, output, _) = Support.Run(environment, "setup");
        Assert.Equal(0, code);
        Assert.Contains("Antigravity: plugin", output);
        Assert.Equal("NEXUS_PACK_RESULT Antigravity=plugin; sign-in=to do", output.Trim().Split('\n')[^1].Trim());

        var entry = JsonNode.Parse(File.ReadAllText(PluginConfig))!["mcpServers"]!["nexus-agymail"]!;
        Assert.Equal(Path.GetFullPath(Support.Exe), entry["command"]!.GetValue<string>(), ignoreCase: true);
        Assert.Equal(["--mcp"], entry["args"]!.AsArray().Select(a => a!.GetValue<string>()));
        Assert.Equal("D:\\vault", entry["env"]!["NEXUS_AGYMAIL_PROTECTED"]!.GetValue<string>());
        foreach (var (relative, bytes) in Registration.PluginFiles())
            Assert.Equal(bytes, File.ReadAllBytes(Path.Combine(Gemini, "plugins", "nexus-agymail", relative)));
        Assert.Contains(Registration.PluginFiles(), f => f.Path == "skills/nexus-agymail/SKILL.md");
        Assert.Contains(Registration.PluginFiles(), f => f.Path == "plugin.json");
        var global = JsonNode.Parse(File.ReadAllText(Path.Combine(Gemini, "mcp_config.json")))!["mcpServers"]!.AsObject();
        Assert.Equal(["other"], global.Select(kv => kv.Key));
        Assert.NotEmpty(Directory.GetFiles(Path.Combine(home, ".nexus", "nexus-agymail", "backups")));

        output = Support.Run(environment, "setup").Out;
        Assert.Contains("Antigravity: plugin (already installed)", output);
    }

    [Fact]
    public void Setup_keeps_a_plugin_written_by_the_user()
    {
        Directory.CreateDirectory(Path.GetDirectoryName(PluginConfig)!);
        const string mine = """{"mcpServers":{"nexus-agymail":{"command":"node","args":["my-gmail.js"]}}}""";
        File.WriteAllText(PluginConfig, mine);
        File.WriteAllText(Path.Combine(Gemini, "config.json"), """{"plugins":{"nexus-agymail":{"enabled":false}}}""");
        environment["NEXUS_PACK"] = "1";
        var (code, output, _) = Support.Run(environment, "setup");
        Assert.Equal(0, code);
        Assert.Contains("kept", output);
        Assert.Equal(mine, File.ReadAllText(PluginConfig));
        Assert.Equal("NEXUS_PACK_RESULT Antigravity=kept; sign-in=to do", output.Trim().Split('\n')[^1].Trim());
    }

    [Theory]
    [InlineData("""{"command":"C:\\a\\NexusAGYmail.exe","args":["--mcp"]}""", true)]
    [InlineData("""{"command":"uvx","args":["nexus-agymail==0.12.0"]}""", true)]
    [InlineData("""{"command":"uvx","args":["other-server"]}""", false)]
    [InlineData("""{"command":"node","args":["nexus-agymail"]}""", false)]
    [InlineData("\"text\"", false)]
    public void Only_our_own_entries_are_replaced(string entry, bool ours) =>
        Assert.Equal(ours, Registration.Ours(JsonNode.Parse(entry)));
}
