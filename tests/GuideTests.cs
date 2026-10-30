using System.Text.Json.Nodes;

namespace NexusAGYmail.Tests;

/// <summary>The guided first sign-in with fake inputs, browser and clock: pages opened in order, the downloaded client
/// found by itself, the checks after sign-in. Its own temporary state folder, never the real one.</summary>
public sealed class GuideTests : IDisposable
{
    private readonly string root = Support.NewTemp("guide");
    private string Downloads => Path.Combine(root, "Downloads");
    private string State => Path.Combine(root, "state");
    private readonly List<string> said = [];
    private readonly List<string> opened = [];
    private readonly Queue<string> answers = new();

    public GuideTests()
    {
        Directory.CreateDirectory(Downloads);
        Directory.CreateDirectory(State);
    }

    public void Dispose() => Support.Delete(root);

    private static string Desktop(string project) =>
        "{\"installed\":{\"client_id\":\"cid-" + project + "\",\"client_secret\":\"cs\",\"project_id\":\"" + project + "\"}}";

    private GuideIo Io(Func<OAuthClientConfig, bool, string, CancellationToken, JsonObject>? signIn = null, Action? onSleep = null) => new()
    {
        Say = said.Add,
        Ask = prompt => { said.Add(prompt); return answers.Count > 0 ? answers.Dequeue() : ""; },
        Open = opened.Add,
        Sleep = _ => onSleep?.Invoke(),
        Downloads = Downloads,
        StateDir = State,
        SignIn = signIn,
    };

    private static JsonObject Result(bool apiDisabled = false, bool testing = false) => new()
    {
        ["account"] = "alice@example.com",
        ["scopes"] = new JsonObject { ["reauth_required"] = false },
        ["gmail_api_disabled"] = apiDisabled,
        ["testing_mode"] = testing,
    };

    [Fact]
    public void Newest_desktop_client_only_since_the_start()
    {
        DateTime since = DateTime.UtcNow.AddMinutes(-1);
        string old = Path.Combine(Downloads, "client_secret_old.json");
        File.WriteAllText(old, Desktop("old"));
        File.SetLastWriteTimeUtc(old, since.AddMinutes(-10));
        File.WriteAllText(Path.Combine(Downloads, "client_secret_web.json"), "{\"web\":{\"client_id\":\"w\"}}");
        Assert.Null(Guide.NewestClient(Downloads, since));
        string fresh = Path.Combine(Downloads, "client_secret_new.json");
        File.WriteAllText(fresh, Desktop("p1"));
        Assert.Equal(fresh, Guide.NewestClient(Downloads, since));
    }

    [Fact]
    public void Creates_the_client_page_by_page_then_signs_in()
    {
        answers.Enqueue("alice@example.com");  // account; then Enter at every pause
        string? signedWith = null;
        var guide = new Guide(Io(
            signIn: (client, _, account, _) => { signedWith = client.ClientId + "|" + account; return Result(); },
            onSleep: () => File.WriteAllText(Path.Combine(Downloads, "client_secret_123.json"), Desktop("p1"))));
        Assert.Equal(0, guide.Run("", openBrowser: true, interactive: true));
        Assert.Equal([Guide.ProjectCreate, Guide.EnableGmail, Guide.Branding, Guide.Audience, Guide.ClientCreate],
            opened.Select(u => u.Split("?authuser=")[0].Split("&authuser=")[0]));
        Assert.All(opened, u => Assert.Contains("authuser=alice%40example.com", u));
        Assert.True(File.Exists(OAuthSignIn.ClientSecretTarget(State)));
        Assert.Equal("cid-p1|alice@example.com", signedWith);
        Assert.Contains(said, s => s.Contains("Done: signed in as alice@example.com"));
        Assert.Contains(said, s => s.Contains(Guide.PrivacyPage));
    }

    [Fact]
    public void After_sign_in_opens_the_page_that_fixes_the_project()
    {
        File.WriteAllText(OAuthSignIn.ClientSecretTarget(State), Desktop("p9"));
        var results = new Queue<JsonObject>([Result(apiDisabled: true), Result(testing: true), Result()]);
        var guide = new Guide(Io(signIn: (_, _, _, _) => results.Dequeue()));
        Assert.Equal(0, guide.Run("", openBrowser: true, interactive: true));
        Assert.Equal([Guide.EnableGmail + "&project=p9", Guide.Audience + "?project=p9"], opened);
    }

    [Fact]
    public void Without_a_console_no_client_means_a_clear_message()
    {
        var guide = new Guide(Io());
        Assert.Equal(1, guide.Run("", openBrowser: false, interactive: false));
        Assert.Empty(opened);
        Assert.Contains(said, s => s.Contains("--client-secret"));
    }

    [Fact]
    public void A_given_non_desktop_client_is_refused()
    {
        string web = Path.Combine(root, "web.json");
        File.WriteAllText(web, "{\"web\":{\"client_id\":\"w\"}}");
        Assert.Equal(1, new Guide(Io()).Run(web, openBrowser: false, interactive: false));
        Assert.False(File.Exists(OAuthSignIn.ClientSecretTarget(State)));
    }
}
