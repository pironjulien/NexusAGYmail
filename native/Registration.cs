using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusAGYmail;

/// <summary>
/// `NexusAGYmail.exe setup`: declares the server to Antigravity as the plugin ~/.gemini/config/plugins/nexus-agymail
/// (skill and server, stdio entry = this executable with --mcp), never in the global mcp_config.json (an old entry of
/// ours there is removed: duplicate tools). An entry written by an earlier installer (uvx … nexus-agymail, or another
/// NexusAGYmail.exe) is replaced and its `env` kept; a plugin written otherwise by the user is kept. Claude and Codex have
/// their official Gmail connector: nothing is declared there. Every edited settings file is backed up first.
/// Nexus pack (NEXUS_PACK=1): no question, no wait; exit 0 unless a declaration failed, and a last line
/// `NEXUS_PACK_RESULT Antigravity=…; sign-in=…`. Signing in to Google stays the user's gesture.
/// </summary>
public static partial class Registration
{
    public const string Name = "nexus-agymail";
    private static readonly UTF8Encoding Utf8 = new(false);
    private static readonly JsonSerializerOptions Indented = new() { WriteIndented = true, Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static bool Interactive() =>
        Environment.GetEnvironmentVariable("NEXUS_PACK") != "1" && !Console.IsInputRedirected && !Console.IsOutputRedirected;

    public static int Setup(string[] args)
    {
        if (args.Length > 0)
        {
            Console.Error.WriteLine($"usage: NexusAGYmail.exe setup (unexpected argument: {args[0]})");
            return 2;
        }
        bool pack = Environment.GetEnvironmentVariable("NEXUS_PACK") == "1";
        string exe = Environment.ProcessPath ?? throw new InvalidOperationException("Executable path unknown.");
        var results = DeclareAll(exe);
        foreach (var (client, state) in results)
            Console.WriteLine($"{client}: {state}");
        if (results.Count == 0)
            Console.WriteLine("Antigravity not found: install it, open it once, then run this again (or configure your MCP client by hand, see the README).");

        var report = GmailAuth.InspectToken();
        bool signedIn = report["present"]?.GetValue<bool>() == true && report["error"] is null;
        if (signedIn)
        {
            string account = report["account"]?.GetValue<string>() ?? "";
            Console.WriteLine($"Gmail: signed in ({(account.Length > 0 ? account : "account recorded in the token")}).");
        }
        else
            Console.WriteLine($"Gmail: not signed in yet. Next step: {GmailAuth.ReauthHint} (guided: it opens each Google page, then checks).");

        bool failed = results.Values.Any(r => r.StartsWith("failed", StringComparison.Ordinal));
        if (!signedIn && Interactive())
        {
            Console.Write("Connect Gmail now (guided, about 5 minutes)? [Y/n] ");
            string answer = (Console.ReadLine() ?? "n").Trim().ToLowerInvariant();
            if (answer is "" or "y" or "yes" or "o" or "oui")
            {
                int code = AuthCommand.Run([]);
                return code != 0 ? code : failed ? 1 : 0;
            }
        }
        if (pack)
        {
            var fields = results.Select(r => $"{r.Key}={Short(r.Value)}").Append($"sign-in={(signedIn ? "done" : "to do")}");
            Console.WriteLine("NEXUS_PACK_RESULT " + string.Join("; ", fields));
        }
        return failed ? 1 : 0;
    }

    private static string Short(string result) =>
        result.StartsWith("failed", StringComparison.Ordinal) ? "failed"
        : result.StartsWith("kept", StringComparison.Ordinal) ? "kept"
        : result.StartsWith("plugin", StringComparison.Ordinal) ? "plugin" : result;

    /// <summary>Result per client found (absent clients are not listed).</summary>
    public static Dictionary<string, string> DeclareAll(string exe)
    {
        string? result;
        try { result = Antigravity(exe); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            result = $"failed: {ex.Message}";
        }
        return result is null ? new() : new() { ["Antigravity"] = result };
    }

    [GeneratedRegex(@"^nexus-agymail(==[\w.]+|@latest)?$")]
    private static partial Regex PackageArg();

    /// <summary>Entry written by one of our installers (uvx … nexus-agymail, or a NexusAGYmail.exe), or no entry.</summary>
    public static bool Ours(JsonNode? existing)
    {
        if (existing is null)
            return true;
        if (existing is not JsonObject entry)
            return false;
        string command = Path.GetFileName(entry["command"] is JsonValue v && v.TryGetValue(out string? c) ? c : "").ToLowerInvariant();
        if (command == "nexusagymail.exe")
            return true;
        var args = (entry["args"] as JsonArray ?? []).Select(a => a is JsonValue av && av.TryGetValue(out string? s) ? s : "");
        return command is "uvx" or "uvx.exe" && args.Any(a => PackageArg().IsMatch(a));
    }

    public static JsonObject Entry(string exe, JsonNode? previous)
    {
        var entry = new JsonObject { ["command"] = exe, ["args"] = new JsonArray("--mcp") };
        if (previous is JsonObject old && old["env"] is JsonObject env && env.Count > 0)
            entry["env"] = env.DeepClone();
        return entry;
    }

    /// <summary>Plugin files embedded in the executable (relative path, bytes).</summary>
    public static IEnumerable<(string Path, byte[] Bytes)> PluginFiles()
    {
        var assembly = Assembly.GetExecutingAssembly();
        foreach (string resource in assembly.GetManifestResourceNames().Order(StringComparer.Ordinal).Where(r => r.StartsWith("plugin/", StringComparison.Ordinal)))
        {
            using var stream = assembly.GetManifestResourceStream(resource)!;
            using var copy = new MemoryStream();
            stream.CopyTo(copy);
            yield return (resource["plugin/".Length..].Replace('\\', '/'), copy.ToArray());
        }
    }

    private static JsonObject ReadObject(string path)
    {
        if (!File.Exists(path) || new FileInfo(path).Length == 0)
            return new JsonObject();
        string text = File.ReadAllText(path, Encoding.UTF8).TrimStart('﻿');
        return JsonNode.Parse(text) as JsonObject ?? throw new InvalidOperationException($"{path}: not a JSON object");
    }

    private static void WriteAtomic(string path, string text)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        string temporary = path + ".nexus-tmp";
        File.WriteAllText(temporary, text.ReplaceLineEndings("\n"), Utf8);
        File.Move(temporary, path, overwrite: true);
    }

    private static void Backup(string path)
    {
        if (!File.Exists(path))
            return;
        string folder = Path.Join(GmailAuth.GetNexusDir(), "backups");
        Directory.CreateDirectory(folder);
        string stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss", System.Globalization.CultureInfo.InvariantCulture);
        File.Copy(path, Path.Join(folder, $"{Path.GetFileName(Path.GetDirectoryName(path))}-{Path.GetFileName(path)}-{stamp}"), overwrite: true);
    }

    /// <summary>State of the Antigravity declaration; null when Antigravity is absent.</summary>
    public static string? Antigravity(string exe)
    {
        string gemini = Path.Join(GmailAuth.HomeDir(), ".gemini");
        if (!Directory.Exists(Path.Join(gemini, "antigravity")) && !Directory.Exists(Path.Join(gemini, "config")))
            return null;
        string root = Path.Join(gemini, "config");
        string target = Path.Join(root, "plugins", Name);
        string mcpFile = Path.Join(target, "mcp_config.json");
        JsonNode? current = File.Exists(mcpFile) ? ReadObject(mcpFile)["mcpServers"]?[Name] : null;
        if (File.Exists(mcpFile) && !Ours(current))
            return "kept (a nexus-agymail plugin of yours already exists)";

        bool changed = false;
        foreach (var (relative, bytes) in PluginFiles())
        {
            string destination = Path.Join(target, relative.Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(destination) && File.ReadAllBytes(destination).AsSpan().SequenceEqual(bytes))
                continue;
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            File.WriteAllBytes(destination, bytes);
            changed = true;
        }
        var entry = Entry(exe, current);
        if (!JsonNode.DeepEquals(current, entry))
        {
            var config = new JsonObject { ["mcpServers"] = new JsonObject { [Name] = entry } };
            WriteAtomic(mcpFile, config.ToJsonString(Indented) + "\n");
            changed = true;
        }

        string globalMcp = Path.Join(root, "mcp_config.json");  // old global entry of ours: duplicate tools
        if (File.Exists(globalMcp))
        {
            var data = ReadObject(globalMcp);
            if (data["mcpServers"] is JsonObject servers && servers.TryGetPropertyValue(Name, out var ours) && Ours(ours))
            {
                Backup(globalMcp);
                servers.Remove(Name);
                WriteAtomic(globalMcp, data.ToJsonString(Indented) + "\n");
            }
        }

        bool disabled = false;  // a discovered plugin is on by default; an explicit choice of the user is only reported
        try { disabled = ReadObject(Path.Join(root, "config.json"))["plugins"]?[Name]?["enabled"] is JsonValue e && e.TryGetValue(out bool on) && !on; }
        catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException) { /* unreadable settings: nothing to report */ }
        string state = changed ? "plugin" : "plugin (already installed)";
        return state + (disabled ? ", turned off in Antigravity's settings" : "");
    }
}
