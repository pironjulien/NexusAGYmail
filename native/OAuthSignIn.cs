using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace NexusAGYmail;

/// <summary>Client OAuth « Application de bureau » de l'utilisateur (section installed du JSON de Google).</summary>
public sealed record OAuthClientConfig(string ClientId, string ClientSecret, string AuthUri, string TokenUri, string ProjectId);

/// <summary>
/// Connexion initiale au compte Google (équivalent natif de auth.authenticate_interactive) : flux navigateur sur un
/// port local 127.0.0.1, PKCE S256 et state vérifié, échange du code, adresse du compte, jeton chiffré par le
/// TokenStore. Un ancien jeton aux portées en trop n'est révoqué qu'APRÈS l'enregistrement du nouveau : un flux
/// annulé ou en échec laisse l'ancien jeton intact et utilisable.
/// </summary>
public sealed class OAuthSignIn
{
    public const string SuccessMessage = "NexusAGYmail is signed in. You can close this tab.";
    public const string RevokeUrl = "https://oauth2.googleapis.com/revoke";
    public const string DefaultAuthUri = "https://accounts.google.com/o/oauth2/auth";
    public const string DefaultTokenUri = "https://oauth2.googleapis.com/token";
    public const string DefaultApiBase = "https://gmail.googleapis.com/gmail/v1/users/me";
    public const string UniverseDomain = "googleapis.com";
    public const int LongLivedSeconds = 30 * 24 * 3600; // au-delà, le jeton n'est pas celui du mode Test (7 jours)
    public static readonly string[] ApiDisabledMarkers =
        ["accessNotConfigured", "SERVICE_DISABLED", "has not been used in project", "is disabled"];
    public static readonly TimeSpan DefaultTimeout = TimeSpan.FromMinutes(10);

    private const string VerifierChars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
    private const int MaxRequestHead = 16 * 1024;

    private static readonly HttpClient DefaultHttp =
        new(new SocketsHttpHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(20) };

    public static readonly JsonSerializerOptions OutputJson = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    // Points d'injection (les tests hors ligne les remplacent ; rien d'autre ne sort de la machine que vers Google).
    public HttpClient Http { get; init; } = DefaultHttp;
    public Action<string> OpenBrowser { get; init; } = OpenDefaultBrowser;
    public TextWriter Log { get; init; } = Console.Error;
    public TimeSpan Timeout { get; init; } = DefaultTimeout;
    public string RevokeUri { get; init; } = RevokeUrl;
    public string ApiBase { get; init; } =
        (Environment.GetEnvironmentVariable("NEXUS_AGYMAIL_API_BASE") ?? DefaultApiBase).TrimEnd('/');
    public TimeSpan ProfileRetryDelay { get; init; } = TimeSpan.FromSeconds(1);

    // --- client OAuth

    /// <summary>Où `auth --client-secret` copie le client OAuth.</summary>
    public static string ClientSecretTarget(string? stateDir = null) =>
        Path.Combine(stateDir ?? GmailAuth.GetNexusDir(), "client_secret.json");

    /// <summary>Localise le client OAuth Desktop (NEXUS_AGYMAIL_CLIENT_SECRET, sinon le dossier d'état) sans le lire.</summary>
    public static string GetClientSecretPath(string? stateDir = null)
    {
        string? chosen = Environment.GetEnvironmentVariable(GmailAuth.ClientSecretEnv);
        if (!string.IsNullOrEmpty(chosen))
        {
            string path = Path.GetFullPath(ExpandHome(chosen));
            if (File.Exists(path))
                return path;
            throw new FileNotFoundException($"{GmailAuth.ClientSecretEnv} points to a missing file: {path}");
        }

        string local = ClientSecretTarget(stateDir);
        if (File.Exists(local))
            return local;
        throw new FileNotFoundException(
            $"no OAuth client ({local}): create a Google OAuth client of type Desktop app (see the README), then run " +
            $"{GmailAuth.ReauthHint} --client-secret <downloaded client_secret_....json>");
    }

    /// <summary>Contenu du client OAuth s'il est de type « Application de bureau » (installed.client_id), sinon null.</summary>
    public static JsonObject? ReadDesktopClient(string path)
    {
        try
        {
            string text = File.ReadAllText(path, Encoding.UTF8); // BOM UTF-8 accepté, comme utf-8-sig
            if (JsonNode.Parse(text) is JsonObject data &&
                data["installed"] is JsonObject installed &&
                installed["client_id"] is JsonValue id && id.TryGetValue<string>(out string? s) && !string.IsNullOrEmpty(s))
                return data;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or ArgumentException or NotSupportedException)
        {
            // Illisible ou pas du JSON : pas un client de bureau.
        }
        return null;
    }

    /// <summary>Copie le client dans le dossier d'état (droits réduits) ; rend (chemin, projet Google).</summary>
    public static (string Target, string Project) SaveClient(string source, string? stateDir = null)
    {
        string full = Path.GetFullPath(ExpandHome(source));
        var data = ReadDesktopClient(full) ?? throw new ArgumentException(
            $"{Path.GetFileName(full)} is not a Desktop app OAuth client (no 'installed' section): create a client of type Desktop app");
        string target = ClientSecretTarget(stateDir);
        if (!string.Equals(Path.GetFullPath(target), full, StringComparison.OrdinalIgnoreCase))
        {
            File.Copy(full, target, overwrite: true);
            try { Dpapi.RestrictToCurrentUser(target); }
            catch (Exception) { /* droits au mieux : le fichier reste dans le dossier d'état de l'utilisateur */ }
        }
        return (target, ProjectOf(data));
    }

    /// <summary>Lit le client OAuth comme google_auth_oauthlib (section installed, sinon web).</summary>
    public static OAuthClientConfig LoadClientConfig(string path)
    {
        JsonObject? data;
        try
        {
            data = JsonNode.Parse(File.ReadAllText(path, Encoding.UTF8)) as JsonObject;
        }
        catch (JsonException ex)
        {
            throw new GmailAuthError("client", $"unreadable OAuth client ({path}): {ex.Message}");
        }

        var section = data?["installed"] as JsonObject ?? data?["web"] as JsonObject
            ?? throw new GmailAuthError("client", $"Client secrets must be for a web or installed app ({path}).");
        string Str(string key, string fallback = "") =>
            section[key] is JsonValue v && v.TryGetValue<string>(out string? s) && !string.IsNullOrEmpty(s) ? s : fallback;

        string clientId = Str("client_id");
        if (string.IsNullOrEmpty(clientId))
            throw new GmailAuthError("client", $"OAuth client without client_id ({path}).");
        return new OAuthClientConfig(clientId, Str("client_secret"), Str("auth_uri", DefaultAuthUri), Str("token_uri", DefaultTokenUri), Str("project_id"));
    }

    private static string ProjectOf(JsonObject data) =>
        (data["installed"] as JsonObject)?["project_id"] is JsonValue v && v.TryGetValue<string>(out string? s) ? s : "";

    private static string ExpandHome(string path) =>
        path == "~" || path.StartsWith("~/", StringComparison.Ordinal) || path.StartsWith("~\\", StringComparison.Ordinal)
            ? GmailAuth.HomeDir() + path[1..]
            : path;

    // --- PKCE et URL d'autorisation

    /// <summary>Vérificateur PKCE de 128 caractères non réservés (comme google_auth_oauthlib).</summary>
    public static string NewCodeVerifier() => RandomNumberGenerator.GetString(VerifierChars, 128);

    /// <summary>Défi S256 : base64url sans remplissage du SHA-256 du vérificateur.</summary>
    public static string CodeChallenge(string verifier) => Base64Url(SHA256.HashData(Encoding.ASCII.GetBytes(verifier)));

    public static string NewState() => Base64Url(RandomNumberGenerator.GetBytes(24));

    private static string Base64Url(byte[] bytes) =>
        Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    public static string BuildAuthorizationUrl(OAuthClientConfig client, string redirectUri, string state, string challenge, string loginHint = "")
    {
        var query = new List<KeyValuePair<string, string>>
        {
            new("response_type", "code"),
            new("client_id", client.ClientId),
            new("redirect_uri", redirectUri),
            new("scope", string.Join(' ', GmailAuth.RequiredScopes)),
            new("state", state),
            new("code_challenge", challenge),
            new("code_challenge_method", "S256"),
            new("prompt", "consent"),
            new("access_type", "offline"),
        };
        if (!string.IsNullOrEmpty(loginHint))
            query.Add(new("login_hint", loginHint));
        string sep = client.AuthUri.Contains('?') ? "&" : "?";
        return client.AuthUri + sep + string.Join("&", query.Select(kv => $"{Uri.EscapeDataString(kv.Key)}={Uri.EscapeDataString(kv.Value)}"));
    }

    public static void OpenDefaultBrowser(string url)
    {
        using var _ = Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
    }

    // --- flux complet

    public JsonObject Run(TokenStore store, OAuthClientConfig client, bool openBrowser = true, string loginHint = "", CancellationToken ct = default) =>
        RunAsync(store, client, openBrowser, loginHint, ct).GetAwaiter().GetResult();

    public async Task<JsonObject> RunAsync(TokenStore store, OAuthClientConfig client, bool openBrowser = true, string loginHint = "", CancellationToken ct = default)
    {
        JsonObject? old;
        try
        {
            old = store.Load();
        }
        catch (TokenError)
        {
            old = null;
        }

        string verifier = NewCodeVerifier();
        string state = NewState();
        string code;
        string redirectUri;

        var listener = new TcpListener(IPAddress.Loopback, 0) { ExclusiveAddressUse = true };
        listener.Start();
        try
        {
            int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            redirectUri = $"http://127.0.0.1:{port}/";
            string url = BuildAuthorizationUrl(client, redirectUri, state, CodeChallenge(verifier), loginHint);
            if (openBrowser)
            {
                try { OpenBrowser(url); }
                catch (Exception ex) { Log.WriteLine($"Browser not opened ({ex.Message}): use the URL below."); }
            }
            Log.WriteLine($"Sign in to Google in your browser (if it did not open, visit this URL):\n{url}");
            Log.Flush();
            code = await WaitForCodeAsync(listener, state, ct).ConfigureAwait(false);
        }
        finally
        {
            listener.Stop();
        }

        var tokenResponse = await ExchangeCodeAsync(client, code, redirectUri, verifier, ct).ConfigureAwait(false);
        var info = BuildTokenInfo(tokenResponse, client);

        string profileError = "";
        try
        {
            info["account"] = await ReadProfileAddressAsync(info["token"]!.GetValue<string>(), ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex) // l'adresse est un confort : le jeton reste bon sans elle
        {
            profileError = ex.Message;
            Log.WriteLine($"Account address not read: {profileError}");
        }

        store.Save(info);
        var result = new JsonObject
        {
            ["account"] = info["account"]?.GetValue<string>() ?? "",
            ["scopes"] = GmailAuth.ScopeReport(info),
            ["path"] = store.TokenPath,
        };
        if (tokenResponse["refresh_token_expires_in"] is JsonNode expiresIn)
        {
            result["refresh_token_expires_in"] = expiresIn.DeepClone();
            result["testing_mode"] = ShortLived(expiresIn);
        }
        if (profileError.Length > 0)
        {
            result["profile_error"] = profileError;
            result["gmail_api_disabled"] = ApiDisabledMarkers.Any(m => profileError.Contains(m, StringComparison.Ordinal));
        }

        string oldRefresh = old?["refresh_token"]?.GetValue<string>() ?? "";
        string newRefresh = info["refresh_token"]!.GetValue<string>();
        if (old is not null && GmailAuth.ScopeReport(old)["excess"] is JsonArray { Count: > 0 } && oldRefresh != newRefresh)
        {
            // Sans révocation, l'ancienne autorisation (portées en trop) resterait valable chez Google.
            bool revoked = await RevokeAsync(oldRefresh, ct).ConfigureAwait(false);
            result["old_token_revoked"] = revoked;
            Log.WriteLine($"Old token with extra scopes: {(revoked ? "revoked" : "revocation not confirmed")}.");
            // Google peut retirer toute l'autorisation de l'application avec l'ancien jeton : on vérifie le nouveau.
            if (!await RefreshWorksAsync(info, ct).ConfigureAwait(false))
            {
                result["warning"] =
                    $"revoking the old token also cut the new one: run {GmailAuth.ReauthHint} again " +
                    "(the old one is gone, the second sign-in gives a clean token)";
            }
        }
        return result;
    }

    // --- serveur de redirection local

    private async Task<string> WaitForCodeAsync(TcpListener listener, string expectedState, CancellationToken ct)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(Timeout);
        try
        {
            while (true)
            {
                using TcpClient conn = await listener.AcceptTcpClientAsync(deadline.Token).ConfigureAwait(false);
                string? code = await HandleRedirectAsync(conn, expectedState, deadline.Token).ConfigureAwait(false);
                if (code is not null)
                    return code;
            }
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            throw new GmailAuthError(
                "timeout",
                $"timed out waiting for the sign-in in the browser ({Timeout.TotalSeconds:0} s): run {GmailAuth.ReauthHint} again");
        }
    }

    /// <summary>Une requête du navigateur : le code si c'est la bonne redirection, null pour une requête annexe.</summary>
    private static async Task<string?> HandleRedirectAsync(TcpClient conn, string expectedState, CancellationToken ct)
    {
        NetworkStream stream = conn.GetStream();
        using var readTimeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        readTimeout.CancelAfter(TimeSpan.FromSeconds(10));
        string head;
        try
        {
            head = await ReadRequestHeadAsync(stream, readTimeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            return null; // connexion muette (préchargement du navigateur) : on attend la suivante
        }
        catch (IOException)
        {
            return null;
        }

        string requestLine = head.Split("\r\n", 2)[0];
        string[] parts = requestLine.Split(' ');
        if (parts.Length < 2 || parts[0] != "GET")
        {
            await RespondAsync(stream, 405, "Method Not Allowed", "Method not allowed.", ct).ConfigureAwait(false);
            return null;
        }

        var query = ParseQuery(parts[1]);
        if (!query.ContainsKey("code") && !query.ContainsKey("error") && !query.ContainsKey("state"))
        {
            await RespondAsync(stream, 404, "Not Found", "Not found.", ct).ConfigureAwait(false);
            return null; // favicon.ico et autres requêtes annexes
        }

        if (!query.TryGetValue("state", out string? state) ||
            !CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(state), Encoding.UTF8.GetBytes(expectedState)))
        {
            await RespondAsync(stream, 400, "Bad Request", "NexusAGYmail: this sign-in answer does not match the request. Run auth again.", ct).ConfigureAwait(false);
            throw new GmailAuthError("state", "(mismatching_state) the sign-in answer does not match this request (state differs): nothing saved, run auth again");
        }

        if (query.TryGetValue("error", out string? error))
        {
            await RespondAsync(stream, 400, "Bad Request", $"NexusAGYmail: Google refused the sign-in ({error}).", ct).ConfigureAwait(false);
            query.TryGetValue("error_description", out string? description);
            throw new GmailAuthError("refused", $"({error}) Google refused the sign-in{(string.IsNullOrEmpty(description) ? "" : ": " + description)}");
        }

        if (!query.TryGetValue("code", out string? code) || string.IsNullOrEmpty(code))
        {
            await RespondAsync(stream, 400, "Bad Request", "NexusAGYmail: no authorization code in Google's answer.", ct).ConfigureAwait(false);
            throw new GmailAuthError("refused", "no authorization code in Google's answer: run auth again");
        }

        await RespondAsync(stream, 200, "OK", SuccessMessage, ct).ConfigureAwait(false);
        return code;
    }

    private static async Task<string> ReadRequestHeadAsync(NetworkStream stream, CancellationToken ct)
    {
        var buffer = new byte[4096];
        using var head = new MemoryStream();
        while (head.Length < MaxRequestHead)
        {
            int n = await stream.ReadAsync(buffer, ct).ConfigureAwait(false);
            if (n == 0)
                break;
            head.Write(buffer, 0, n);
            string text = Encoding.ASCII.GetString(head.GetBuffer(), 0, (int)head.Length);
            if (text.Contains("\r\n\r\n", StringComparison.Ordinal))
                return text;
        }
        return Encoding.ASCII.GetString(head.GetBuffer(), 0, (int)head.Length);
    }

    private static async Task RespondAsync(NetworkStream stream, int status, string reason, string body, CancellationToken ct)
    {
        byte[] payload = Encoding.UTF8.GetBytes(body);
        string headers =
            $"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {payload.Length}\r\n" +
            "Cache-Control: no-store\r\nConnection: close\r\n\r\n";
        try
        {
            await stream.WriteAsync(Encoding.ASCII.GetBytes(headers), ct).ConfigureAwait(false);
            await stream.WriteAsync(payload, ct).ConfigureAwait(false);
            await stream.FlushAsync(ct).ConfigureAwait(false);
        }
        catch (IOException)
        {
            // Navigateur parti : la réponse de la page n'est qu'un confort.
        }
    }

    public static Dictionary<string, string> ParseQuery(string target)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        int q = target.IndexOf('?');
        if (q < 0)
            return result;
        foreach (string pair in target[(q + 1)..].Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            int eq = pair.IndexOf('=');
            string key = Uri.UnescapeDataString((eq < 0 ? pair : pair[..eq]).Replace('+', ' '));
            string value = eq < 0 ? "" : Uri.UnescapeDataString(pair[(eq + 1)..].Replace('+', ' '));
            result.TryAdd(key, value);
        }
        return result;
    }

    // --- échanges avec Google

    private async Task<JsonObject> ExchangeCodeAsync(OAuthClientConfig client, string code, string redirectUri, string verifier, CancellationToken ct)
    {
        var form = new Dictionary<string, string>
        {
            ["grant_type"] = "authorization_code",
            ["code"] = code,
            ["redirect_uri"] = redirectUri,
            ["client_id"] = client.ClientId,
            ["client_secret"] = client.ClientSecret,
            ["code_verifier"] = verifier,
        };

        string body;
        HttpStatusCode status;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, client.TokenUri) { Content = new FormUrlEncodedContent(form) };
            using var resp = await Http.SendAsync(req, ct).ConfigureAwait(false);
            status = resp.StatusCode;
            body = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            throw new GmailAuthError("network", $"network: the sign-in code could not be exchanged ({ex.Message})");
        }

        JsonObject? json = null;
        try { json = JsonNode.Parse(body) as JsonObject; }
        catch (JsonException) { /* corps non JSON : traité ci-dessous */ }

        if ((int)status is < 200 or > 299)
        {
            string error = json?["error"]?.ToString() ?? "";
            string description = json?["error_description"]?.ToString() ?? "";
            throw new GmailAuthError(
                "refused",
                $"Google refused the sign-in code (HTTP {(int)status}{(error.Length > 0 ? ", " + error : "")}{(description.Length > 0 ? ": " + description : "")})");
        }
        if (json is null || json["access_token"] is not JsonValue at || !at.TryGetValue<string>(out string? access) || string.IsNullOrEmpty(access))
            throw new GmailAuthError("refused", "invalid token response from Google (no access_token)");
        if (json["refresh_token"] is not JsonValue rt || !rt.TryGetValue<string>(out string? refresh) || string.IsNullOrEmpty(refresh))
            throw new GmailAuthError("refused", "Google returned no refresh_token: nothing saved, run auth again");
        return json;
    }

    /// <summary>Jeton au format de google.oauth2.credentials.Credentials.to_json(), portées réellement accordées.</summary>
    public static JsonObject BuildTokenInfo(JsonObject response, OAuthClientConfig client, DateTime? nowUtc = null)
    {
        // Les portées réellement accordées (consentement granulaire), pas celles demandées.
        var granted = (response["scope"]?.GetValue<string>() ?? "")
            .Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)
            .ToList();
        if (granted.Count == 0)
            granted = GmailAuth.RequiredScopes.ToList();
        granted.Sort(StringComparer.Ordinal);

        var info = new JsonObject
        {
            ["token"] = response["access_token"]!.GetValue<string>(),
            ["refresh_token"] = response["refresh_token"]!.GetValue<string>(),
            ["token_uri"] = client.TokenUri,
            ["client_id"] = client.ClientId,
            ["client_secret"] = client.ClientSecret,
            ["scopes"] = new JsonArray(granted.Select(s => (JsonNode)JsonValue.Create(s)!).ToArray()),
            ["universe_domain"] = UniverseDomain,
            ["account"] = "",
        };
        if (response["expires_in"] is JsonValue ev && TryInt(ev, out long seconds))
        {
            DateTime expiry = (nowUtc ?? DateTime.UtcNow).AddSeconds(seconds);
            info["expiry"] = expiry.ToString("yyyy-MM-ddTHH:mm:ss.ffffff", CultureInfo.InvariantCulture) + "Z";
        }
        return info;
    }

    private async Task<string> ReadProfileAddressAsync(string accessToken, CancellationToken ct)
    {
        string url = ApiBase + "/profile";
        for (int attempt = 0; ; attempt++)
        {
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", accessToken);
            req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            using var resp = await Http.SendAsync(req, ct).ConfigureAwait(false);
            string body = await resp.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            int status = (int)resp.StatusCode;
            if (resp.IsSuccessStatusCode)
                return (JsonNode.Parse(body) as JsonObject)?["emailAddress"]?.GetValue<string>() ?? "";
            // Comme execute(num_retries=3) : 429 et 5xx retentés avec un délai croissant.
            if ((status == 429 || status >= 500) && attempt < 3)
            {
                await Task.Delay(ProfileRetryDelay * (1 << attempt), ct).ConfigureAwait(false);
                continue;
            }
            string compact = string.Join(' ', body.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
            if (compact.Length > 2000)
                compact = compact[..2000] + "...";
            throw new IOException($"HTTP {status} when requesting {url}: {compact}");
        }
    }

    /// <summary>Révoque un jeton chez Google ; true si révoqué ou déjà invalide (200 ou 400).</summary>
    public async Task<bool> RevokeAsync(string refreshToken, CancellationToken ct = default)
    {
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, RevokeUri)
            {
                Content = new FormUrlEncodedContent(new Dictionary<string, string> { ["token"] = refreshToken }),
            };
            using var resp = await Http.SendAsync(req, ct).ConfigureAwait(false);
            return resp.StatusCode is HttpStatusCode.OK or HttpStatusCode.BadRequest;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception)
        {
            return false; // réseau : révocation non confirmée, le nouveau jeton est déjà enregistré
        }
    }

    /// <summary>True si le jeton se renouvelle encore chez Google (false seulement sur invalid_grant).</summary>
    public async Task<bool> RefreshWorksAsync(JsonObject info, CancellationToken ct = default)
    {
        try
        {
            await GmailAuth.RequestRefreshAsync(
                Http,
                GmailAuth.TokenUriOf(info),
                info["refresh_token"]?.GetValue<string>() ?? "",
                info["client_id"]?.GetValue<string>() ?? "",
                info["client_secret"]?.GetValue<string>() ?? "",
                ct).ConfigureAwait(false);
            return true;
        }
        catch (GmailAuthError ex)
        {
            return ex.Code != "revoked"; // réseau ou autre refus : rien de prouvé, le jeton reste en place
        }
    }

    public static bool ShortLived(JsonNode? expiresIn) =>
        expiresIn is JsonValue v && TryInt(v, out long seconds) && seconds > 0 && seconds < LongLivedSeconds;

    private static bool TryInt(JsonValue value, out long result)
    {
        if (value.TryGetValue(out long l)) { result = l; return true; }
        if (value.TryGetValue(out double d) && double.IsFinite(d)) { result = (long)d; return true; }
        if (value.TryGetValue(out string? s) && long.TryParse(s, NumberStyles.Integer, CultureInfo.InvariantCulture, out l)) { result = l; return true; }
        result = 0;
        return false;
    }
}

/// <summary>Commande `NexusAGYmail.exe auth [--client-secret FICHIER] [--no-browser]` (équivalent de cmd_auth).</summary>
public static class AuthCommand
{
    public const string Usage = "NexusAGYmail.exe auth [--client-secret FILE] [--no-browser]";
    private const string ConsoleUrl = "https://console.cloud.google.com";
    private const string EnableGmail = ConsoleUrl + "/flows/enableapi?apiid=gmail.googleapis.com";
    private const string Audience = ConsoleUrl + "/auth/audience";

    public static int Run(string[] args)
    {
        string clientSecret = "";
        bool openBrowser = true;
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            if (a is "-h" or "--help")
            {
                Console.WriteLine($"usage: {Usage}");
                Console.WriteLine("Sign in to Google in the browser and save the token locally.");
                Console.WriteLine("  --client-secret FILE  Desktop app OAuth client JSON downloaded from Google");
                Console.WriteLine("  --no-browser          print the sign-in URL instead of opening a browser");
                return 0;
            }
            if (a == "--no-browser")
                openBrowser = false;
            else if (a == "--client-secret" && i + 1 < args.Length)
                clientSecret = args[++i];
            else if (a.StartsWith("--client-secret=", StringComparison.Ordinal))
                clientSecret = a["--client-secret=".Length..];
            else
            {
                Console.Error.WriteLine($"usage: {Usage}");
                Console.Error.WriteLine(a == "--client-secret" ? "error: --client-secret expects a FILE" : $"error: unrecognized argument: {a}");
                return 2;
            }
        }

        using var cancel = new CancellationTokenSource();
        ConsoleCancelEventHandler onCancel = (_, e) =>
        {
            e.Cancel = true; // sortie propre, sans pile : l'attente se termine sur l'annulation
            cancel.Cancel();
        };
        Console.CancelKeyPress += onCancel;
        try
        {
            string project = "";
            if (!string.IsNullOrEmpty(clientSecret))
            {
                try
                {
                    var (target, proj) = OAuthSignIn.SaveClient(clientSecret);
                    project = proj;
                    Console.Error.WriteLine($"OAuth client saved: {target}");
                }
                catch (ArgumentException ex)
                {
                    Console.Error.WriteLine(ex.Message);
                    return 1;
                }
            }

            OAuthClientConfig client;
            try
            {
                client = OAuthSignIn.LoadClientConfig(OAuthSignIn.GetClientSecretPath());
            }
            catch (FileNotFoundException ex)
            {
                Console.Error.WriteLine(ex.Message);
                return 1;
            }
            if (project.Length == 0)
                project = client.ProjectId;

            Console.Error.WriteLine(
                "Google shows \"Google hasn't verified this app\": it is your own app. Click Advanced, then Go to " +
                "NexusAGYmail (unsafe), and allow. 'Access blocked' (Error 403: access_denied) means the app is not " +
                "published yet: publish it, then press Ctrl+C and run auth again.");

            var result = new OAuthSignIn().Run(GmailAuth.GetTokenStore(), client, openBrowser, "", cancel.Token);
            Console.WriteLine(result.ToJsonString(OAuthSignIn.OutputJson));
            return Followup(result, project, openBrowser);
        }
        catch (OperationCanceledException) when (cancel.IsCancellationRequested)
        {
            Console.Error.WriteLine();
            Console.Error.WriteLine("Cancelled.");
            return 130;
        }
        catch (GmailAuthError ex)
        {
            Console.Error.WriteLine($"Sign-in failed ({ex.Code}): {ex.Message}");
            return 1;
        }
        catch (TokenError ex)
        {
            Console.Error.WriteLine($"Token not saved ({ex.Code}): {ex.Message}");
            return 1;
        }
        finally
        {
            Console.CancelKeyPress -= onCancel;
        }
    }

    /// <summary>Vérifications après connexion (API Gmail, mode Test, portées), avec le geste qui corrige. 0 si tout va.</summary>
    private static int Followup(JsonObject result, string project, bool openBrowser)
    {
        void Open(string url)
        {
            if (!openBrowser)
                return;
            try { OAuthSignIn.OpenDefaultBrowser(url); }
            catch (Exception) { /* l'adresse est affichée de toute façon */ }
        }

        string WithProject(string url) =>
            string.IsNullOrEmpty(project) ? url : $"{url}{(url.Contains('?') ? "&" : "?")}project={project}";

        if (result["gmail_api_disabled"]?.GetValue<bool>() == true)
        {
            string url = WithProject(EnableGmail);
            Open(url);
            Console.Error.WriteLine($"The Gmail API is not on for this project yet. Click Enable on {url}, wait a minute, then run auth again.");
            return 1;
        }
        if (result["testing_mode"]?.GetValue<bool>() == true)
        {
            string url = WithProject(Audience);
            Open(url);
            Console.Error.WriteLine($"The app is still in Testing (sign-in valid 7 days only). Click Publish app on {url}, then run auth again.");
            return 1;
        }
        if (result["scopes"]?["reauth_required"]?.GetValue<bool>() == true)
        {
            Console.Error.WriteLine($"Scopes: {result["scopes"]?["message"]}. Allow every Gmail permission and sign in again.");
            return 1;
        }
        if (result["warning"] is JsonNode warning)
        {
            Console.Error.WriteLine(warning.GetValue<string>());
            return 1;
        }
        string account = result["account"]?.GetValue<string>() ?? "";
        Console.Error.WriteLine($"Done: signed in as {(account.Length > 0 ? account : "your account")}. Restart Antigravity to use Gmail.");
        return 0;
    }
}
