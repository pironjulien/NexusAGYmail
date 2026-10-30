using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusAGYmail.Tests;

/// <summary>Paths, temporary homes and engine processes shared by the tests. Nothing touches the real home or token.</summary>
public static class Support
{
    public static string Repository { get; } = typeof(Support).Assembly.GetCustomAttributes<AssemblyMetadataAttribute>()
        .Single(a => a.Key == "NexusAGYmail.Repository").Value!;

    /// <summary>The engine under test: NEXUS_AGYMAIL_TEST_EXE (the exact build an installer qualifies), else the build
    /// referenced by this project.</summary>
    public static string Exe
    {
        get
        {
            string? chosen = Environment.GetEnvironmentVariable("NEXUS_AGYMAIL_TEST_EXE");
            if (!string.IsNullOrEmpty(chosen))
                return chosen;
            string local = Path.Combine(AppContext.BaseDirectory, "NexusAGYmail.exe");
            Assert.True(File.Exists(local), $"NexusAGYmail.exe missing next to the tests: {local}");
            return local;
        }
    }

    public static string NewTemp(string name)
    {
        string path = Path.Combine(Path.GetTempPath(), "nexusagymail-tests", $"{name}-{Guid.NewGuid():N}");
        Directory.CreateDirectory(path);
        return path;
    }

    public static void Delete(string path)
    {
        try { Directory.Delete(path, true); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { }
    }

    /// <summary>Environment of an isolated engine: temporary home, state, journal; no account or agent inherited.</summary>
    public static Dictionary<string, string?> IsolatedEnvironment(string home) => new()
    {
        ["USERPROFILE"] = home,
        ["HOME"] = home,
        ["NEXUS_AGYMAIL_HOME"] = Path.Combine(home, ".nexus", "nexus-agymail"),
        ["NEXUS_JOURNAL_HOME"] = Path.Combine(home, "journal"),
        ["NEXUS_AGYMAIL_ACCOUNT"] = null,
        ["NEXUS_AGYMAIL_CLIENT_SECRET"] = null,
        ["NEXUS_AGYMAIL_PROTECTED"] = null,
        ["NEXUS_AGENT"] = null,
        ["NEXUS_PACK"] = null,
        ["NEXUS_AGYMAIL_UPDATE_CHECK"] = "0",
        // Never Google: any request the test did not route to its fake fails on this closed port.
        ["NEXUS_AGYMAIL_API_BASE"] = "http://127.0.0.1:9/gmail/v1/users/me",
        ["NEXUS_AGYMAIL_TOKEN_URI"] = "http://127.0.0.1:9/never-refresh",
    };

    public static ProcessStartInfo StartInfo(string exe, IEnumerable<string> args, IDictionary<string, string?> environment)
    {
        var info = new ProcessStartInfo(exe)
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
        };
        foreach (string a in args)
            info.ArgumentList.Add(a);
        foreach (var (key, value) in environment)
        {
            if (value is null) info.Environment.Remove(key);
            else info.Environment[key] = value;
        }
        return info;
    }

    /// <summary>Runs the engine to completion: (exit code, stdout, stderr).</summary>
    public static (int Code, string Out, string Err) Run(IDictionary<string, string?> environment, params string[] args)
    {
        using var process = Process.Start(StartInfo(Exe, args, environment))!;
        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(60_000))
        {
            process.Kill(true);
            Assert.Fail($"NexusAGYmail {string.Join(' ', args)} did not finish");
        }
        return (process.ExitCode, stdout.Result, stderr.Result);
    }

    public static JsonObject TestToken(params string[] scopes) => new()
    {
        ["token"] = "fake-access-only-for-local-test",
        ["refresh_token"] = "fake-refresh",
        ["client_id"] = "fake-client",
        ["client_secret"] = "fake-secret",
        ["expiry"] = "2099-01-01T00:00:00Z",
        ["scopes"] = new JsonArray(scopes.Select(s => (JsonNode)JsonValue.Create(s)!).ToArray()),
        ["account"] = "test@example.com",
    };
}

/// <summary>The engine over JSON-RPC (stdio), one process per test.</summary>
public sealed class NativeServer : IDisposable
{
    private readonly Process process;
    private readonly BlockingCollection<string> lines = [];
    private int serial;

    public int Pid => process.Id;

    public NativeServer(IDictionary<string, string?> environment)
    {
        process = Process.Start(Support.StartInfo(Support.Exe, ["--mcp"], environment))!;
        var reader = process.StandardOutput;
        _ = Task.Run(async () =>
        {
            while (await reader.ReadLineAsync() is { } line)
                lines.Add(line);
            lines.CompleteAdding();
        });
        _ = process.StandardError.ReadToEndAsync();
    }

    /// <summary>Writes a request (returns its id) without waiting for the answer.</summary>
    public int Send(string method, JsonObject? parameters = null)
    {
        int id = ++serial;
        Write(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = id, ["method"] = method, ["params"] = parameters ?? new JsonObject() });
        return id;
    }

    public void Write(JsonObject message)
    {
        process.StandardInput.WriteLine(message.ToJsonString());
        process.StandardInput.Flush();
    }

    /// <summary>Next message from the engine (an answer, or a request such as elicitation/create).</summary>
    public JsonObject Next()
    {
        if (!lines.TryTake(out string? line, TimeSpan.FromSeconds(60)))
        {
            process.Kill(true);
            Assert.Fail("No JSON-RPC message from the engine");
        }
        return JsonNode.Parse(line!)!.AsObject();
    }

    public JsonObject Rpc(string method, JsonObject? parameters = null)
    {
        int id = Send(method, parameters);
        var response = Next();
        Assert.True(response["id"] is JsonValue v && v.TryGetValue(out int got) && got == id, $"expected the answer to {method}, got {response.ToJsonString()}");
        return response;
    }

    public JsonObject Call(string name, JsonObject? arguments = null)
    {
        var result = Rpc("tools/call", new JsonObject { ["name"] = name, ["arguments"] = arguments ?? new JsonObject() })["result"]!.AsObject();
        var value = JsonNode.Parse(result["content"]![0]!["text"]!.GetValue<string>())!.AsObject();
        Assert.Equal(value["status"]?.GetValue<string>() == "error", result["isError"]!.GetValue<bool>());
        if (result["structuredContent"] is JsonNode structured)
            Assert.True(JsonNode.DeepEquals(structured, value), "structuredContent differs from the text content");
        return value;
    }

    public void Dispose()
    {
        try
        {
            process.StandardInput.Close();
            if (!process.WaitForExit(3000))
                process.Kill(true);
        }
        catch (InvalidOperationException) { }
        process.Dispose();
    }
}
