using System.Text;
using System.Text.Json.Nodes;

namespace NexusAGYmail;

internal static class Diagnostics
{
    public static void Started()
    {
        try
        {
            string directory = Path.Combine(GmailAuth.HomeDir(), ".nexus", "status");
            Directory.CreateDirectory(directory); string path = Path.Combine(directory, "agymail.json");
            string temporary = path + "." + Environment.ProcessId + ".tmp";
            var value = new JsonObject { ["product"] = "agymail", ["version"] = Program.Version, ["latest"] = null,
                ["state"] = "idle", ["runtime"] = "native", ["pid"] = Environment.ProcessId,
                ["started_at"] = DateTimeOffset.UtcNow.ToString("O"), ["checked_at"] = null,
                ["message"] = "Native engine running. Installed build; published update status has not been checked." };
            File.WriteAllText(temporary, value.ToJsonString(), new UTF8Encoding(false)); File.Move(temporary, path, true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { /* Diagnostic failure never changes an operation outcome. */ }
    }
    public static void Journal(string? client, string tool, JsonObject result, long milliseconds)
    {
        try
        {
            if (new[] { "0", "off", "false", "no" }.Contains(Environment.GetEnvironmentVariable("NEXUS_JOURNAL")?.ToLowerInvariant())) return;
            string name = (client ?? Environment.GetEnvironmentVariable("NEXUS_AGENT") ?? "").ToLowerInvariant();
            string? agent = name.Contains("codex") ? "codex" : name.Contains("claude") ? "claude" : name.Contains("antigravity") || name.Contains("gemini") || name.Contains("jetski") ? "antigravity" : null;
            if (agent is null) return;
            string root = Environment.GetEnvironmentVariable("NEXUS_JOURNAL_HOME") ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".nexus", "journal");
            string status = result["status"]?.GetValue<string>() ?? "error";
            bool success = status is "success" or "authenticated";
            var now = DateTimeOffset.UtcNow;
            var line = new JsonObject { ["v"] = 1, ["ts"] = now.ToString("O"), ["agent"] = agent, ["outil"] = Program.Version,
                ["appel"] = null, ["app"] = "gmail", ["cadre"] = null, ["action"] = tool, ["technique"] = "api",
                ["issue"] = success ? "✓" : status == "confirmation_required" ? "refus" : "✗", ["ms"] = milliseconds,
                ["premier_plan"] = false, ["erreur"] = success ? null : result["code"]?.DeepClone() ?? JsonValue.Create("error") };
            string directory = Path.Combine(root, "agymail", agent); Directory.CreateDirectory(directory);
            File.AppendAllText(Path.Combine(directory, now.ToString("yyyy-MM") + ".jsonl"), line.ToJsonString() + "\n", new UTF8Encoding(false));
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { /* Journal is metadata-only and non-blocking. */ }
    }
}
