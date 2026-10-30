using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace NexusAGYmail;

/// <summary>
/// Tests hors ligne de `auth` (--selftest) : faux Google en mémoire (jeton, révocation, profil Gmail), faux navigateur
/// qui suit la redirection sur la boucle locale, jeton dans un dossier temporaire. Jamais le vrai navigateur, jamais
/// le vrai jeton ni le vrai dossier d'état.
/// </summary>
internal static class AuthSelfTests
{
    private const string TokenUri = "https://oauth.fake.test/token";
    private const string RevokeUri = "https://oauth.fake.test/revoke";
    private const string ApiBase = "https://gmail.fake.test/gmail/v1/users/me";
    private const string AuthCode = "4/fake-auth-code";

    private static readonly OAuthClientConfig Client = new(
        "cid.apps.googleusercontent.com", "fake-client-secret", "https://accounts.fake.test/o/oauth2/auth", TokenUri, "fake-project");

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

    private static void Play(Scenario s, string dir)
    {
        var store = new TokenStore(dir);
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
                    // Faux navigateur : requête annexe d'abord (ignorée), puis la redirection de Google.
                    using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                    string redirect = q["redirect_uri"];
                    using (var fav = await http.GetAsync(redirect + "favicon.ico"))
                        s.FaviconStatus = ((int)fav.StatusCode).ToString();
                    string state = s.ForgeState ? "forged-state" : q["state"];
                    string target = $"{redirect}?state={Uri.EscapeDataString(state)}&code={Uri.EscapeDataString(AuthCode)}" +
                                    $"&scope={Uri.EscapeDataString(string.Join(' ', GmailAuth.RequiredScopes))}";
                    using var resp = await http.GetAsync(target);
                    s.PageBody = await resp.Content.ReadAsStringAsync();
                });
            },
        };

        try
        {
            s.Result = flow.RunAsync(store, Client, openBrowser: true, loginHint: "", s.Cancel).GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            s.Error = ex;
        }
        try { browser.Wait(TimeSpan.FromSeconds(10)); }
        catch (AggregateException) { /* navigateur coupé par un refus : le refus est vérifié ailleurs */ }
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

    public static void Run(Action<string, bool> assert)
    {
        string root = Path.Combine(Path.GetTempPath(), "nexus_selftest_auth_" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            RunAll(assert, root);
        }
        finally
        {
            try { Directory.Delete(root, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }

    private static void RunAll(Action<string, bool> assert, string root)
    {
        // PKCE : même calcul que google_auth_oauthlib (valeur de référence calculée par Python).
        assert("OAuth PKCE S256 challenge matches the Python reference",
            OAuthSignIn.CodeChallenge("nexus-pkce-test-verifier-0123456789-abcdefghijklmnopqrstuvwxyz") ==
            "RCm6MYn7-aEfdI1aNyLyZBNiv2d5LfMi_QtB1HIPqZ4");
        string verifier = OAuthSignIn.NewCodeVerifier();
        assert("OAuth PKCE verifier is 128 unreserved characters",
            verifier.Length == 128 && verifier.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '.' or '_' or '~') &&
            verifier != OAuthSignIn.NewCodeVerifier());

        // 1. Flux complet, ancien jeton aux portées exactes : enregistré, jamais révoqué.
        var ok = new Scenario { OldToken = OldToken(GmailAuth.RequiredScopes) };
        Play(ok, Path.Combine(root, "ok"));
        var q = OAuthSignIn.ParseQuery(ok.AuthUrl);
        assert("OAuth URL asks consent, offline access, S256, the 3 scopes and a 127.0.0.1 redirect",
            ok.AuthUrl.StartsWith(Client.AuthUri + "?", StringComparison.Ordinal) &&
            q.GetValueOrDefault("prompt") == "consent" && q.GetValueOrDefault("access_type") == "offline" &&
            q.GetValueOrDefault("code_challenge_method") == "S256" && q.GetValueOrDefault("response_type") == "code" &&
            q.GetValueOrDefault("scope") == string.Join(' ', GmailAuth.RequiredScopes) &&
            q.GetValueOrDefault("redirect_uri", "").StartsWith("http://127.0.0.1:", StringComparison.Ordinal) &&
            q.GetValueOrDefault("state", "").Length >= 32);
        assert("OAuth flow exchanges the code with the matching PKCE verifier", ok.Error is null && ok.PkceOk);
        assert("OAuth flow ignores side requests and shows the success page",
            ok.FaviconStatus == "404" && ok.PageBody == OAuthSignIn.SuccessMessage);

        JsonObject? saved = null;
        try { saved = ok.Store!.Load(); } catch (TokenError) { }
        var expectedKeys = new[] { "token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "universe_domain", "account", "expiry" };
        assert("OAuth token saved (DPAPI) in the Python to_json format and read back by GetCredentials' fields",
            saved is not null && saved.Select(kv => kv.Key).SequenceEqual(expectedKeys) &&
            saved["token"]?.GetValue<string>() == "at-new" && saved["refresh_token"]?.GetValue<string>() == "rt-new" &&
            saved["token_uri"]?.GetValue<string>() == TokenUri && saved["client_id"]?.GetValue<string>() == Client.ClientId &&
            saved["client_secret"]?.GetValue<string>() == Client.ClientSecret &&
            GmailAuth.GrantedScopes(saved).SequenceEqual(GmailAuth.RequiredScopes.OrderBy(x => x, StringComparer.Ordinal)) &&
            saved["account"]?.GetValue<string>() == "alice@example.com" &&
            saved["expiry"]?.GetValue<string>() is { } exp && exp.EndsWith('Z') && DateTimeOffset.TryParse(exp, out _));
        var r = ok.Result;
        assert("OAuth result reports account, scopes, path and testing_mode like Python",
            r is not null && r["account"]?.GetValue<string>() == "alice@example.com" &&
            r["scopes"]?["reauth_required"]?.GetValue<bool>() == false && r["path"]?.GetValue<string>() == ok.Store!.TokenPath &&
            r["refresh_token_expires_in"]?.GetValue<int>() == 604799 && r["testing_mode"]?.GetValue<bool>() == true &&
            r["profile_error"] is null);
        assert("OAuth flow never revokes an old token with exact scopes",
            r is not null && r["old_token_revoked"] is null && !ok.Fake!.Calls.Any(c => c.Url == RevokeUri));

        // 2. state falsifié : refus, aucun échange, ancien jeton intact.
        var forged = new Scenario { ForgeState = true, OldToken = OldToken(GmailAuth.RequiredScopes) };
        Play(forged, Path.Combine(root, "forged"));
        JsonObject? kept = null;
        try { kept = forged.Store!.Load(); } catch (TokenError) { }
        assert("OAuth flow refuses a forged state without exchanging or saving",
            forged.Error is GmailAuthError { Code: "state" } && forged.Fake!.Calls.Count == 0 &&
            kept?["refresh_token"]?.GetValue<string>() == "rt-old");

        // 3. Ancien jeton aux portées en trop : révoqué APRÈS l'enregistrement, nouveau jeton vérifié ; profil en échec.
        var excess = new Scenario
        {
            OldToken = OldToken([.. GmailAuth.RequiredScopes, "https://mail.google.com/"]),
            ProfileDisabled = true,
            RefreshRevoked = true,
        };
        Play(excess, Path.Combine(root, "excess"));
        var rx = excess.Result;
        assert("OAuth flow revokes the old over-scoped token only after saving the new one",
            rx is not null && rx["old_token_revoked"]?.GetValue<bool>() == true && excess.RefreshAtRevoke == "rt-new" &&
            excess.Fake!.Calls.Any(c => c.Url == RevokeUri && c.Body == "token=rt-old"));
        assert("OAuth flow warns when the new token no longer refreshes after the revocation",
            rx?["warning"]?.GetValue<string>() is { } w && w.Contains(GmailAuth.ReauthHint, StringComparison.Ordinal) &&
            excess.Fake!.Calls.Any(c => c.Url == TokenUri && c.Body.Contains("grant_type=refresh_token") && c.Body.Contains("refresh_token=rt-new")));
        JsonObject? afterProfile = null;
        try { afterProfile = excess.Store!.Load(); } catch (TokenError) { }
        assert("OAuth flow keeps the token when the Gmail profile fails (profile_error, gmail_api_disabled)",
            rx?["profile_error"]?.GetValue<string>() is { Length: > 0 } && rx["gmail_api_disabled"]?.GetValue<bool>() == true &&
            rx["account"]?.GetValue<string>() == "" && afterProfile?["refresh_token"]?.GetValue<string>() == "rt-new");

        // 4. Délai d'attente borné et Ctrl+C.
        var idle = new Scenario { BrowserIdle = true, Timeout = TimeSpan.FromMilliseconds(300) };
        Play(idle, Path.Combine(root, "idle"));
        assert("OAuth flow gives up after its bounded timeout", idle.Error is GmailAuthError { Code: "timeout" } && idle.Fake!.Calls.Count == 0);
        using (var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(200)))
        {
            var cancelled = new Scenario { BrowserIdle = true, Cancel = cts.Token };
            Play(cancelled, Path.Combine(root, "cancelled"));
            assert("OAuth flow stops on Ctrl+C (cancellation, exit 130)", cancelled.Error is OperationCanceledException);
        }

        // 5. Client OAuth : copie, refus d'un client non Desktop, variable vers un fichier absent.
        string state = Path.Combine(root, "state");
        Directory.CreateDirectory(state);
        string web = Path.Combine(root, "client_web.json");
        File.WriteAllText(web, "{\"web\":{\"client_id\":\"x\"}}");
        bool refused = false;
        try { OAuthSignIn.SaveClient(web, state); }
        catch (ArgumentException ex) { refused = ex.Message.Contains("is not a Desktop app OAuth client", StringComparison.Ordinal); }
        assert("Client secret: a non-Desktop JSON is refused", refused && !File.Exists(OAuthSignIn.ClientSecretTarget(state)));

        string desktop = Path.Combine(root, "client_secret_123.json");
        File.WriteAllText(desktop, "\uFEFF{\"installed\":{\"client_id\":\"cid\",\"client_secret\":\"cs\",\"project_id\":\"p1\",\"token_uri\":\"" + TokenUri + "\"}}");
        var (target, project) = OAuthSignIn.SaveClient(desktop, state);
        var loaded = OAuthSignIn.LoadClientConfig(target);
        assert("Client secret: a Desktop JSON is copied to the state folder and read back",
            project == "p1" && target == OAuthSignIn.ClientSecretTarget(state) && File.Exists(target) &&
            loaded.ClientId == "cid" && loaded.TokenUri == TokenUri && loaded.AuthUri == OAuthSignIn.DefaultAuthUri);

        string? previous = Environment.GetEnvironmentVariable(GmailAuth.ClientSecretEnv);
        bool missingReported = false;
        try
        {
            Environment.SetEnvironmentVariable(GmailAuth.ClientSecretEnv, Path.Combine(root, "absent.json"));
            OAuthSignIn.GetClientSecretPath(state);
        }
        catch (FileNotFoundException ex)
        {
            missingReported = ex.Message.StartsWith(GmailAuth.ClientSecretEnv + " points to a missing file", StringComparison.Ordinal);
        }
        finally
        {
            Environment.SetEnvironmentVariable(GmailAuth.ClientSecretEnv, previous);
        }
        assert("Client secret: NEXUS_AGYMAIL_CLIENT_SECRET to a missing file is reported", missingReported);

        assert("Reauth hint cites the native auth command",
            GmailAuth.NativeAuthCommand(null) == "NexusAGYmail.exe auth" &&
            GmailAuth.NativeAuthCommand(@"C:\x y\NexusAGYmail.exe") == "& \"C:\\x y\\NexusAGYmail.exe\" auth" &&
            GmailAuth.ReauthHint.EndsWith(" auth", StringComparison.Ordinal) && !GmailAuth.ReauthHint.Contains("uvx"));
    }
}
