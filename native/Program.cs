using System;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NexusAGYmail;

public static class Program
{
    public const string Version = "1.0.1";

    public static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "auth")
        {
            return AuthCommand.Run(args[1..]);
        }

        if (args.Length == 0 || args.Contains("--help") || args.Contains("-h"))
        {
            PrintHelp();
            return 0;
        }

        if (args.Contains("--version") || args.Contains("-v"))
        {
            Console.WriteLine($"NexusAGYmail v{Version} (Native C# .NET 10)");
            return 0;
        }

        if (args.Contains("--selftest"))
        {
            return RunSelftest();
        }

        if (args.Contains("status"))
        {
            return RunStatus();
        }

        if (args.Contains("--mcp"))
        {
            var server = new McpServer();
            server.RunStdio();
            return 0;
        }

        PrintHelp();
        return 1;
    }

    private static void PrintHelp()
    {
        Console.WriteLine($"NexusAGYmail v{Version} - Unified Standalone C# .NET 10 Engine");
        Console.WriteLine("Usage:");
        Console.WriteLine("  NexusAGYmail.exe --mcp       Start stdio MCP server (21 tools)");
        Console.WriteLine("  NexusAGYmail.exe auth [--client-secret FILE] [--no-browser]");
        Console.WriteLine("                              Sign in to Google in the browser and save the token locally");
        Console.WriteLine("  NexusAGYmail.exe --selftest  Run native unit test suite");
        Console.WriteLine("  NexusAGYmail.exe status      Display Gmail authentication status");
        Console.WriteLine("  NexusAGYmail.exe --version   Print version info");
    }

    private static int RunStatus()
    {
        try
        {
            var creds = GmailAuth.GetCredentials();
            Console.WriteLine($"Account: {creds.Account}");
            Console.WriteLine($"Token expiry: {creds.ExpiryIso}");
            Console.WriteLine($"Scopes: {string.Join(", ", creds.Scopes)}");
            Console.WriteLine("Status: Authenticated and valid.");
            return 0;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"Authentication required or error: {ex.Message}");
            return 1;
        }
    }

    private static int RunSelftest()
    {
        int passed = 0;
        int failed = 0;

        void Assert(string name, bool condition)
        {
            if (condition)
            {
                passed++;
                Console.WriteLine($"[PASS] {name}");
            }
            else
            {
                failed++;
                Console.WriteLine($"[FAIL] {name}");
            }
        }

        Console.WriteLine("Running NexusAGYmail native self-tests...");

        // 1. DPAPI roundtrip test on Windows
        try
        {
            if (OperatingSystem.IsWindows())
            {
                byte[] secret = Encoding.UTF8.GetBytes("SuperSecretToken12345");
                byte[] cipher = Dpapi.Protect(secret);
                byte[] plain = Dpapi.Unprotect(cipher);
                Assert("DPAPI encryption and decryption roundtrip", Encoding.UTF8.GetString(plain) == "SuperSecretToken12345");
            }
            else
            {
                Assert("DPAPI skipped on non-Windows", true);
            }
        }
        catch (Exception ex)
        {
            Assert($"DPAPI test failed with exception: {ex.Message}", false);
        }

        // 2. Secret path detection
        try
        {
            Assert("Secret name detection .credentials.json", AttachmentSecurity.IsSecretName(".credentials.json"));
            Assert("Secret name detection token.json", AttachmentSecurity.IsSecretName("token.json"));
            Assert("Secret name detection .env", AttachmentSecurity.IsSecretName(".env"));
            Assert("Secret name detection .env.local", AttachmentSecurity.IsSecretName(".env.local"));
            Assert("Non-secret name detection .env.example", !AttachmentSecurity.IsSecretName(".env.example"));
            Assert("Non-secret name detection report.pdf", !AttachmentSecurity.IsSecretName("report.pdf"));
        }
        catch (Exception ex)
        {
            Assert($"Secret detection failed: {ex.Message}", false);
        }

        // 3. Safe name cleaning
        try
        {
            string safe1 = AttachmentSecurity.SafeName("normal.txt");
            Assert("SafeName normal.txt", safe1 == "normal.txt");

            string safe2 = AttachmentSecurity.SafeName("bad:name/test?.pdf");
            Assert("SafeName cleans forbidden chars", !safe2.Contains(':') && !safe2.Contains('?') && safe2.EndsWith(".pdf"));

            string safe3 = AttachmentSecurity.SafeName("CON.txt");
            Assert("SafeName escapes Windows reserved CON", safe3.StartsWith("_CON"));
        }
        catch (Exception ex)
        {
            Assert($"SafeName test failed: {ex.Message}", false);
        }

        // 4. Address normalization
        try
        {
            string addrs = MimeHelper.NormalizeAddresses("alice@example.com, Bob <bob@example.com>", "To", true);
            Assert("NormalizeAddresses parses valid emails", addrs.Contains("alice@example.com") && addrs.Contains("bob@example.com"));

            bool threw = false;
            try
            {
                MimeHelper.NormalizeAddresses("injected\r\nSubject: attack", "To", false);
            }
            catch (ArgumentException)
            {
                threw = true;
            }
            Assert("NormalizeAddresses blocks CRLF injection", threw);
        }
        catch (Exception ex)
        {
            Assert($"NormalizeAddresses failed: {ex.Message}", false);
        }

        // 5. MIME builder & RFC 2822
        try
        {
            byte[] eml = MimeHelper.BuildRfc2822("dest@example.com", "Test Subject", "Hello World body");
            string emlStr = Encoding.UTF8.GetString(eml);
            Assert("BuildRfc2822 contains To", emlStr.Contains("To: dest@example.com"));
            Assert("BuildRfc2822 contains Subject", emlStr.Contains("Subject: Test Subject"));
            Assert("BuildRfc2822 contains body", emlStr.Contains("Hello World body"));
        }
        catch (Exception ex)
        {
            Assert($"MIME builder failed: {ex.Message}", false);
        }

        // 6. Draft Accord token & fingerprinting
        try
        {
            byte[] draftBytes = Encoding.UTF8.GetBytes("Exact RFC Draft Body");
            string sha = DraftAccord.ComputeFingerprint(draftBytes);
            string token = DraftAccord.IssueAccord(sha);

            Assert("VerifyAccord matches exact fingerprint", DraftAccord.VerifyAccord(token, sha));
            Assert("VerifyAccord rejects altered fingerprint", !DraftAccord.VerifyAccord(token, "altered-sha256"));
        }
        catch (Exception ex)
        {
            Assert($"DraftAccord test failed: {ex.Message}", false);
        }

        // 7. Envois tracker
        try
        {
            string tempDir = Path.Combine(Path.GetTempPath(), "nexus_selftest_" + Guid.NewGuid().ToString("N"));
            var tracker = new EnvoisTracker(tempDir);
            string sha = "test-sha-12345";
            string draftId = "draft-999";
            long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

            string attId = tracker.MarkAttempt(sha, draftId, now);
            var unk = tracker.Unknown(sha);
            Assert("EnvoisTracker detects unknown attempt", unk != null && unk["attempts"]?.GetValue<int>() == 1);

            tracker.Resolve(sha, attId);
            var unkResolved = tracker.Unknown(sha);
            Assert("EnvoisTracker resolves attempt", unkResolved == null);

            Directory.Delete(tempDir, true);
        }
        catch (Exception ex)
        {
            Assert($"EnvoisTracker failed: {ex.Message}", false);
        }

        // 8. MCP server initialize & tools list
        try
        {
            var server = new McpServer();
            var initReq = new JsonObject
            {
                ["jsonrpc"] = "2.0",
                ["id"] = 1,
                ["method"] = "initialize"
            };
            var initResp = server.HandleRequest(initReq);
            Assert("MCP server initialize returns 2.0 result", initResp?["result"]?["serverInfo"]?["name"]?.GetValue<string>() == "nexus-agymail");

            var listReq = new JsonObject
            {
                ["jsonrpc"] = "2.0",
                ["id"] = 2,
                ["method"] = "tools/list"
            };
            var listResp = server.HandleRequest(listReq);
            var tools = listResp?["result"]?["tools"] as JsonArray;
            Assert("MCP server tools/list contains exactly 21 tools", tools != null && tools.Count == 21);
        }
        catch (Exception ex)
        {
            Assert($"MCP server test failed: {ex.Message}", false);
        }

        // 9. Connexion `auth` hors ligne (faux Google, faux navigateur, jeton temporaire)
        try
        {
            AuthSelfTests.Run(Assert);
        }
        catch (Exception ex)
        {
            Assert($"Auth self-tests failed: {ex.GetType().Name}: {ex.Message}", false);
        }

        Console.WriteLine($"Self-tests finished: {passed} passed, {failed} failed.");
        return failed == 0 ? 0 : 1;
    }
}
