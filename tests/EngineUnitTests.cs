using System.Text;
using System.Text.Json.Nodes;

namespace NexusAGYmail.Tests;

/// <summary>In-process checks of the engine's building blocks (formerly `NexusAGYmail.exe --selftest`).</summary>
public class EngineUnitTests
{
    [Fact]
    public void Dpapi_round_trip()
    {
        byte[] cipher = Dpapi.Protect(Encoding.UTF8.GetBytes("SuperSecretToken12345"));
        Assert.Equal("SuperSecretToken12345", Encoding.UTF8.GetString(Dpapi.Unprotect(cipher)));
        Assert.DoesNotContain("SuperSecretToken12345", Encoding.UTF8.GetString(cipher));
    }

    [Theory]
    [InlineData(".credentials.json", true)]
    [InlineData("token.json", true)]
    [InlineData(".env", true)]
    [InlineData(".env.local", true)]
    [InlineData(".env.example", false)]
    [InlineData("report.pdf", false)]
    public void Secret_file_names_are_detected(string name, bool secret) =>
        Assert.Equal(secret, AttachmentSecurity.IsSecretName(name));

    [Fact]
    public void Safe_names_for_saved_attachments()
    {
        Assert.Equal("normal.txt", AttachmentSecurity.SafeName("normal.txt"));
        string cleaned = AttachmentSecurity.SafeName("bad:name/test?.pdf");
        Assert.True(!cleaned.Contains(':') && !cleaned.Contains('?') && cleaned.EndsWith(".pdf", StringComparison.Ordinal), cleaned);
        Assert.StartsWith("_CON", AttachmentSecurity.SafeName("CON.txt"));
    }

    [Fact]
    public void Addresses_are_normalized_and_header_injection_refused()
    {
        string addresses = MimeHelper.NormalizeAddresses("alice@example.com, Bob <bob@example.com>", "To", true);
        Assert.Contains("alice@example.com", addresses);
        Assert.Contains("bob@example.com", addresses);
        Assert.Throws<ArgumentException>(() => MimeHelper.NormalizeAddresses("injected\r\nSubject: attack", "To", false));
    }

    [Fact]
    public void Mime_builder_writes_recipient_subject_and_body()
    {
        string eml = Encoding.UTF8.GetString(MimeHelper.BuildRfc2822("dest@example.com", "Test Subject", "Hello World body"));
        Assert.Contains("To: dest@example.com", eml);
        Assert.Contains("Subject: Test Subject", eml);
        Assert.Contains("Hello World body", eml);
    }

    [Fact]
    public void Accord_is_bound_to_the_exact_draft_bytes()
    {
        string sha = DraftAccord.ComputeFingerprint(Encoding.UTF8.GetBytes("Exact RFC Draft Body"));
        string token = DraftAccord.IssueAccord(sha);
        Assert.True(DraftAccord.VerifyAccord(token, sha));
        Assert.False(DraftAccord.VerifyAccord(token, "altered-sha256"));
    }

    [Fact]
    public void Send_attempts_are_recorded_then_resolved()
    {
        string dir = Support.NewTemp("envois");
        try
        {
            var tracker = new EnvoisTracker(dir);
            string attempt = tracker.MarkAttempt("test-sha-12345", "draft-999", DateTimeOffset.UtcNow.ToUnixTimeSeconds());
            Assert.Equal(1, tracker.Unknown("test-sha-12345")?["attempts"]?.GetValue<int>());
            tracker.Resolve("test-sha-12345", attempt);
            Assert.Null(tracker.Unknown("test-sha-12345"));
        }
        finally { Support.Delete(dir); }
    }

    [Fact]
    public void Mcp_initialize_and_tools_list_in_process()
    {
        var server = new McpServer();
        var init = server.HandleRequest(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = 1, ["method"] = "initialize" });
        Assert.Equal("nexus-agymail", init?["result"]?["serverInfo"]?["name"]?.GetValue<string>());
        Assert.Equal(Program.Version, init?["result"]?["serverInfo"]?["version"]?.GetValue<string>());
        var list = server.HandleRequest(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = 2, ["method"] = "tools/list" });
        Assert.Equal(21, (list?["result"]?["tools"] as JsonArray)?.Count);
        Assert.Null(server.HandleRequest(new JsonObject { ["jsonrpc"] = "2.0", ["method"] = "notifications/initialized" }));
        Assert.Equal(-32601, server.HandleRequest(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = 3, ["method"] = "nope" })?["error"]?["code"]?.GetValue<int>());
    }
}
