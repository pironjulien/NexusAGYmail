using System.Net;
using System.Text;
using System.Text.Json.Nodes;

namespace NexusAGYmail.Tests;

/// <summary>
/// Offline sign-in (`auth`): in-memory fake Google (token, revocation, Gmail profile), a fake browser that follows the
/// redirect on the loopback, the token in a temporary folder. Never the real browser, token or state folder.
/// </summary>
public sealed class AuthFlowTests : IDisposable
{
    private const string TokenUri = "https://oauth.fake.test/token";
    private const string RevokeUri = "https://oauth.fake.test/revoke";
    private const string ApiBase = "https://gmail.fake.test/gmail/v1/users/me";
    private const string AuthCode = "4/fake-auth-code";

    private static readonly OAuthClientConfig Client = new(
        "cid.apps.googleusercontent.com", "fake-client-secret", "https://accounts.fake.test/o/oauth2/auth", TokenUri, "fake-project");

    private readonly string root = Support.NewTemp("auth");
    public void Dispose() => Support.Delete(root);

    private sealed class FakeGoogle(Func<HttpRequestMessage, string, HttpResponseMessage> respond) : HttpMessageHandler
    {
        public readonly List<(string Url, string Body)> Calls = [];

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            string body = request.Content is null ? "" : await request.Content.ReadAsStringAsync(ct);
            lock (Calls)
                Calls.Add((request.RequestUri!.ToString(), body));
            return respond(request, body);
        }
    }

    private sealed class Scenario
    {
        public bool ForgeState;
        public bool BrowserIdle;
        public bool ProfileDisabled;
        public bool RefreshRevoked;
        public JsonObject? OldToken;
        public TimeSpan Timeout = TimeSpan.FromSeconds(30);
        public CancellationToken Cancel = CancellationToken.None;

        public string AuthUrl = "";
        public string Challenge = "";
        public bool PkceOk;
        public string FaviconStatus = "";
        public string PageBody = "";
        public string? RefreshAtRevoke;
        public JsonObject? Result;
        public Exception? Error;
        public FakeGoogle? Fake;
        public TokenStore? Store;
    }

    private static HttpResponseMessage Json(HttpStatusCode code, string json) =>
        new(code) { Content = new StringContent(json, Encoding.UTF8, "application/json") };

    private Scenario Play(Scenario s, string name)
    {
        var store = new TokenStore(Path.Combine(root, name));
        s.Store = store;
        if (s.OldToken is not null)
            store.Save(s.OldToken);

        s.Fake = new FakeGoogle((req, body) =>
        {
            string url = req.RequestUri!.ToString();
            var form = OAuthSignIn.ParseQuery("?" + body);
            if (url == TokenUri && form.GetValueOrDefault("grant_type") == "authorization_code")
            {
                s.PkceOk = form.GetValueOrDefault("code") == AuthCode &&
                           form.TryGetValue("code_verifier", out string? verifier) &&
                           OAuthSignIn.CodeChallenge(verifier) == s.Challenge &&
                           form.GetValueOrDefault("redirect_uri", "").StartsWith("http://127.0.0.1:", StringComparison.Ordinal) &&
                           form.GetValueOrDefault("client_id") == Client.ClientId;
                if (!s.PkceOk)
                    return Json(HttpStatusCode.BadRequest, "{\"error\":\"invalid_grant\"}");
                return Json(HttpStatusCode.OK, new JsonObject
                {
                    ["access_token"] = "at-new",
                    ["expires_in"] = 3599,
                    ["refresh_token"] = "rt-new",
                    ["scope"] = string.Join(' ', Enumerable.Reverse(GmailAuth.RequiredScopes)),
                    ["token_type"] = "Bearer",
                    ["refresh_token_expires_in"] = 604799,
                }.ToJsonString());
            }
            if (url == TokenUri && form.GetValueOrDefault("grant_type") == "refresh_token")
            {
                return s.RefreshRevoked
                    ? Json(HttpStatusCode.BadRequest, "{\"error\":\"invalid_grant\",\"error_description\":\"Token has been expired or revoked.\"}")
                    : Json(HttpStatusCode.OK, "{\"access_token\":\"at-refreshed\",\"expires_in\":3599}");
            }
            if (url == RevokeUri)
            {
                s.RefreshAtRevoke = store.Load()?["refresh_token"]?.GetValue<string>();
                return Json(HttpStatusCode.OK, "{}");
            }
            if (url == ApiBase + "/profile")
            {
                return s.ProfileDisabled
                    ? Json(HttpStatusCode.Forbidden, "{\"error\":{\"code\":403,\"message\":\"Gmail API has not been used in project 1 before or it is disabled.\",\"status\":\"PERMISSION_DENIED\",\"details\":[{\"reason\":\"SERVICE_DISABLED\"}]}}")
                    : Json(HttpStatusCode.OK, "{\"emailAddress\":\"alice@example.com\"}");
            }
            return Json(HttpStatusCode.NotFound, "{}");
        });

        Task browser = Task.CompletedTask;
        var flow = new OAuthSignIn
        {
            Http = new HttpClient(s.Fake),
            Log = TextWriter.Null,
            Timeout = s.Timeout,
            RevokeUri = RevokeUri,
            ApiBase = ApiBase,
            ProfileRetryDelay = TimeSpan.FromMilliseconds(1),
            OpenBrowser = url =>
            {
                s.AuthUrl = url;
                var q = OAuthSignIn.ParseQuery(url);
                s.Challenge = q.GetValueOrDefault("code_challenge", "");
                if (s.BrowserIdle)
                    return;
                browser = Task.Run(async () =>
                {
                    // Fake browser: a side request first (ignored), then Google's redirect.
                    using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                    string redirect = q["redirect_uri"];
                    using (var favicon = await http.GetAsync(redirect + "favicon.ico"))
                        s.FaviconStatus = ((int)favicon.StatusCode).ToString();
                    string state = s.ForgeState ? "forged-state" : q["state"];
                    string target = $"{redirect}?state={Uri.EscapeDataString(state)}&code={Uri.EscapeDataString(AuthCode)}" +
                                    $"&scope={Uri.EscapeDataString(string.Join(' ', GmailAuth.RequiredScopes))}";
                    using var resp = await http.GetAsync(target);
                    s.PageBody = await resp.Content.ReadAsStringAsync();
                });
            },
        };

        try { s.Result = flow.RunAsync(store, Client, openBrowser: true, loginHint: "", s.Cancel).GetAwaiter().GetResult(); }
        catch (Exception ex) { s.Error = ex; }
        try { browser.Wait(TimeSpan.FromSeconds(10)); }
        catch (AggregateException) { /* browser cut by a refusal: the refusal is checked by the test */ }
        return s;
    }

    private static JsonObject OldToken(params string[] scopes) => new()
    {
        ["token"] = "at-old",
        ["refresh_token"] = "rt-old",
        ["token_uri"] = TokenUri,
        ["client_id"] = Client.ClientId,
        ["client_secret"] = Client.ClientSecret,
        ["scopes"] = new JsonArray(scopes.Select(x => (JsonNode)JsonValue.Create(x)!).ToArray()),
        ["account"] = "alice@example.com",
    };

    [Fact]
    public void Pkce_matches_the_reference_and_verifiers_are_random()
    {
        Assert.Equal("RCm6MYn7-aEfdI1aNyLyZBNiv2d5LfMi_QtB1HIPqZ4",
            OAuthSignIn.CodeChallenge("nexus-pkce-test-verifier-0123456789-abcdefghijklmnopqrstuvwxyz"));
        string verifier = OAuthSignIn.NewCodeVerifier();
        Assert.Equal(128, verifier.Length);
        Assert.All(verifier, c => Assert.True(char.IsAsciiLetterOrDigit(c) || c is '-' or '.' or '_' or '~'));
        Assert.NotEqual(verifier, OAuthSignIn.NewCodeVerifier());
    }

    [Fact]
    public void Full_flow_saves_the_token_and_never_revokes_an_exact_one()
    {
        var ok = Play(new Scenario { OldToken = OldToken(GmailAuth.RequiredScopes) }, "ok");
        var q = OAuthSignIn.ParseQuery(ok.AuthUrl);
        Assert.StartsWith(Client.AuthUri + "?", ok.AuthUrl);
        Assert.Equal("consent", q.GetValueOrDefault("prompt"));
        Assert.Equal("offline", q.GetValueOrDefault("access_type"));
        Assert.Equal("S256", q.GetValueOrDefault("code_challenge_method"));
        Assert.Equal("code", q.GetValueOrDefault("response_type"));
        Assert.Equal(string.Join(' ', GmailAuth.RequiredScopes), q.GetValueOrDefault("scope"));
        Assert.StartsWith("http://127.0.0.1:", q.GetValueOrDefault("redirect_uri", ""));
        Assert.True(q.GetValueOrDefault("state", "").Length >= 32);
        Assert.Null(ok.Error);
        Assert.True(ok.PkceOk);
        Assert.Equal("404", ok.FaviconStatus);
        Assert.Equal(OAuthSignIn.SuccessMessage, ok.PageBody);

        var saved = ok.Store!.Load()!;
        Assert.Equal(["token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "universe_domain", "account", "expiry"],
            saved.Select(kv => kv.Key));
        Assert.Equal("at-new", saved["token"]?.GetValue<string>());
        Assert.Equal("rt-new", saved["refresh_token"]?.GetValue<string>());
        Assert.Equal(TokenUri, saved["token_uri"]?.GetValue<string>());
        Assert.Equal(Client.ClientId, saved["client_id"]?.GetValue<string>());
        Assert.Equal(Client.ClientSecret, saved["client_secret"]?.GetValue<string>());
        Assert.Equal(GmailAuth.RequiredScopes.Order(StringComparer.Ordinal), GmailAuth.GrantedScopes(saved));
        Assert.Equal("alice@example.com", saved["account"]?.GetValue<string>());
        Assert.True(saved["expiry"]?.GetValue<string>() is { } exp && exp.EndsWith('Z') && DateTimeOffset.TryParse(exp, out _));

        var r = ok.Result!;
        Assert.Equal("alice@example.com", r["account"]?.GetValue<string>());
        Assert.False(r["scopes"]?["reauth_required"]?.GetValue<bool>());
        Assert.Equal(ok.Store.TokenPath, r["path"]?.GetValue<string>());
        Assert.Equal(604799, r["refresh_token_expires_in"]?.GetValue<int>());
        Assert.True(r["testing_mode"]?.GetValue<bool>());
        Assert.Null(r["profile_error"]);
        Assert.Null(r["old_token_revoked"]);
        Assert.DoesNotContain(ok.Fake!.Calls, c => c.Url == RevokeUri);
    }

    [Fact]
    public void A_forged_state_is_refused_without_exchange()
    {
        var forged = Play(new Scenario { ForgeState = true, OldToken = OldToken(GmailAuth.RequiredScopes) }, "forged");
        Assert.True(forged.Error is GmailAuthError { Code: "state" }, forged.Error?.ToString());
        Assert.Empty(forged.Fake!.Calls);
        Assert.Equal("rt-old", forged.Store!.Load()?["refresh_token"]?.GetValue<string>());
    }

    [Fact]
    public void An_over_scoped_old_token_is_revoked_only_after_saving_the_new_one()
    {
        var excess = Play(new Scenario
        {
            OldToken = OldToken([.. GmailAuth.RequiredScopes, "https://mail.google.com/"]),
            ProfileDisabled = true,
            RefreshRevoked = true,
        }, "excess");
        var rx = excess.Result!;
        Assert.True(rx["old_token_revoked"]?.GetValue<bool>());
        Assert.Equal("rt-new", excess.RefreshAtRevoke);
        Assert.Contains(excess.Fake!.Calls, c => c.Url == RevokeUri && c.Body == "token=rt-old");
        Assert.Contains(GmailAuth.ReauthHint, rx["warning"]?.GetValue<string>() ?? "");
        Assert.Contains(excess.Fake.Calls, c => c.Url == TokenUri && c.Body.Contains("grant_type=refresh_token") && c.Body.Contains("refresh_token=rt-new"));
        Assert.False(string.IsNullOrEmpty(rx["profile_error"]?.GetValue<string>()));
        Assert.True(rx["gmail_api_disabled"]?.GetValue<bool>());
        Assert.Equal("", rx["account"]?.GetValue<string>());
        Assert.Equal("rt-new", excess.Store!.Load()?["refresh_token"]?.GetValue<string>());
    }

    [Fact]
    public void The_wait_is_bounded_and_ctrl_c_stops_it()
    {
        var idle = Play(new Scenario { BrowserIdle = true, Timeout = TimeSpan.FromMilliseconds(300) }, "idle");
        Assert.True(idle.Error is GmailAuthError { Code: "timeout" }, idle.Error?.ToString());
        Assert.Empty(idle.Fake!.Calls);
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        var cancelled = Play(new Scenario { BrowserIdle = true, Cancel = cts.Token }, "cancelled");
        Assert.IsAssignableFrom<OperationCanceledException>(cancelled.Error);
    }

    [Fact]
    public void Client_secret_copy_refusal_and_missing_file()
    {
        string state = Path.Combine(root, "state");
        Directory.CreateDirectory(state);
        string web = Path.Combine(root, "client_web.json");
        File.WriteAllText(web, "{\"web\":{\"client_id\":\"x\"}}");
        var refused = Assert.Throws<ArgumentException>(() => OAuthSignIn.SaveClient(web, state));
        Assert.Contains("is not a Desktop app OAuth client", refused.Message);
        Assert.False(File.Exists(OAuthSignIn.ClientSecretTarget(state)));

        string desktop = Path.Combine(root, "client_secret_123.json");
        File.WriteAllText(desktop, "\uFEFF{\"installed\":{\"client_id\":\"cid\",\"client_secret\":\"cs\",\"project_id\":\"p1\",\"token_uri\":\"" + TokenUri + "\"}}");
        var (target, project) = OAuthSignIn.SaveClient(desktop, state);
        var loaded = OAuthSignIn.LoadClientConfig(target);
        Assert.Equal("p1", project);
        Assert.Equal(OAuthSignIn.ClientSecretTarget(state), target);
        Assert.Equal("cid", loaded.ClientId);
        Assert.Equal(TokenUri, loaded.TokenUri);
        Assert.Equal(OAuthSignIn.DefaultAuthUri, loaded.AuthUri);
    }

    [Fact]
    public void The_reauth_hint_cites_the_native_command()
    {
        Assert.Equal("NexusAGYmail.exe auth", GmailAuth.NativeAuthCommand(null));
        Assert.Equal("& \"C:\\x y\\NexusAGYmail.exe\" auth", GmailAuth.NativeAuthCommand(@"C:\x y\NexusAGYmail.exe"));
        Assert.EndsWith(" auth", GmailAuth.ReauthHint);
        Assert.DoesNotContain("uvx", GmailAuth.ReauthHint);
    }
}
