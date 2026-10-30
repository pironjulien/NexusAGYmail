using System.Text.Json.Nodes;

namespace NexusAGYmail;

/// <summary>
/// `NexusAGYmail.exe auth [--client-secret FILE] [--no-browser]`. Without an OAuth client yet and in a console, a guided
/// first sign-in: it opens each page of the Google Cloud console at the right place, says what to click, finds the
/// downloaded client JSON by itself, signs in, then checks what can be checked afterwards (Gmail API on, app published
/// rather than left in Testing at 7 days) and opens the page that fixes it. No shared OAuth client: the user creates
/// their own in their own Google Cloud project. Nothing is clicked for them; the guide opens, waits and checks.
/// </summary>
public static class AuthCommand
{
    public const string Usage = "NexusAGYmail.exe auth [--client-secret FILE] [--no-browser]";

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
                Console.WriteLine("Sign in to Google in the browser and save the token locally (guided when no OAuth client exists yet).");
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

        var io = Guide.ForConsole(openBrowser);
        Console.CancelKeyPress += io.OnCancel;
        try
        {
            return new Guide(io).Run(clientSecret, openBrowser, Registration.Interactive());
        }
        catch (OperationCanceledException)
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
            Console.CancelKeyPress -= io.OnCancel;
        }
    }
}

/// <summary>Inputs and outputs of the guide (replaced by fakes in the tests).</summary>
public sealed class GuideIo
{
    public Action<string> Say { get; init; } = Console.Error.WriteLine;
    public Func<string, string> Ask { get; init; } = prompt => { Console.Error.Write(prompt); return Console.ReadLine() ?? ""; };
    public Action<string> Open { get; init; } = OAuthSignIn.OpenDefaultBrowser;
    public Action<TimeSpan> Sleep { get; init; } = Thread.Sleep;
    public Func<DateTime> UtcNow { get; init; } = () => DateTime.UtcNow;
    public string Downloads { get; init; } = Path.Combine(GmailAuth.HomeDir(), "Downloads");
    /// <summary>State folder (client and token); null = the default one.</summary>
    public string? StateDir { get; init; }
    /// <summary>Signs in and returns the result (the real browser flow by default).</summary>
    public Func<OAuthClientConfig, bool, string, CancellationToken, JsonObject>? SignIn { get; init; }

    private CancellationTokenSource cancel = new();
    /// <summary>Ctrl+C: cancels the current step (a new source is armed for the next one).</summary>
    public CancellationToken Token => cancel.Token;
    public void Rearm() { if (cancel.IsCancellationRequested) cancel = new(); }
    public void OnCancel(object? sender, ConsoleCancelEventArgs e) { e.Cancel = true; cancel.Cancel(); }
}

public sealed class Guide(GuideIo io)
{
    public const string ConsoleUrl = "https://console.cloud.google.com";
    public const string ProjectCreate = ConsoleUrl + "/projectcreate";
    public const string EnableGmail = ConsoleUrl + "/flows/enableapi?apiid=gmail.googleapis.com";
    public const string Branding = ConsoleUrl + "/auth/branding";
    public const string Audience = ConsoleUrl + "/auth/audience";
    public const string ClientCreate = ConsoleUrl + "/auth/clients/create";
    public const string Fallback = "https://developers.google.com/workspace/gmail/api/reference/mcp";
    // Public pages Google requires on the consent screen before the app can be published.
    public const string HomePage = "https://github.com/pironjulien/NexusAGYmail";
    public const string PrivacyPage = HomePage + "#privacy";
    public static readonly TimeSpan WaitForJson = TimeSpan.FromMinutes(15);

    public static GuideIo ForConsole(bool openBrowser) => new()
    {
        Open = openBrowser ? OAuthSignIn.OpenDefaultBrowser : _ => { },
    };

    private static string Param(string url, string name, string value) =>
        value.Length == 0 ? url : $"{url}{(url.Contains('?') ? "&" : "?")}{name}={Uri.EscapeDataString(value)}";

    /// <summary>Console page on the right project and Google account (otherwise the browser's default account).</summary>
    public static string WithProject(string url, string project, string account = "") =>
        Param(Param(url, "project", project), "authuser", account);

    public int Run(string clientSecret, bool openBrowser, bool interactive)
    {
        string project = "";
        string account = "";
        if (clientSecret.Length > 0)
        {
            try { project = OAuthSignIn.SaveClient(clientSecret, io.StateDir).Project; }
            catch (ArgumentException ex) { io.Say(ex.Message); return 1; }
            io.Say($"OAuth client saved: {OAuthSignIn.ClientSecretTarget(io.StateDir)}");
        }
        else
        {
            string existing;
            try { existing = OAuthSignIn.GetClientSecretPath(io.StateDir); }
            catch (FileNotFoundException ex)
            {
                if (!interactive)
                {
                    io.Say(ex.Message);
                    return 1;
                }
                account = AskAccount();
                var created = CreateClient(account);
                if (created is null)
                    return 1;
                project = created.Value.Project;
                existing = created.Value.Target;
            }
            if (project.Length == 0)
                project = OAuthSignIn.LoadClientConfig(existing).ProjectId;
        }
        return SignIn(project, openBrowser, account, interactive);
    }

    public string AskAccount()
    {
        string answer = io.Ask("   Gmail address to connect (Enter = the account your browser opens by default): ").Trim();
        return answer.Contains('@') && !answer.Contains(' ') ? answer : "";
    }

    private void Step(int number, string title, string url, string account, params string[] lines)
    {
        url = WithProject(url, "", account);
        io.Say($"\n{number}/5  {title}");
        io.Open(url);
        io.Say($"   Opened in your browser: {url}");
        foreach (string line in lines)
            io.Say($"   - {line}");
    }

    private void Pause() => io.Ask("   Press Enter when it is done... ");

    /// <summary>Steps 1 to 4: project, Gmail API, published consent screen, downloaded Desktop client.</summary>
    public (string Target, string Project)? CreateClient(string account = "")
    {
        DateTime started = io.UtcNow();
        io.Say("Gmail needs your own Google OAuth client (free, about 5 minutes, once): your mail never goes through anyone " +
               "else's app. Each page opens in your browser; do what it says, then come back here.");
        Step(1, "Create a Google Cloud project", ProjectCreate, account,
            "Project name: NexusAGYmail", "Click Create, then select the new project (bell or project menu at the top).");
        Pause();
        Step(2, "Turn on the Gmail API", EnableGmail, account,
            "Check that the project at the top is NexusAGYmail (switch to it if needed)", "Click Next, then Enable.");
        Pause();
        Step(3, "Consent screen", Branding, account,
            "Click Get started. App name: NexusAGYmail; support email: yours. Next.",
            "Audience: External. Next. Contact email: yours. Next. Accept, then Create.",
            "Google asks for public pages before publishing: open Branding again and fill in",
            $"  App home page: {HomePage}",
            $"  Privacy policy link: {PrivacyPage}",
            "  Authorized domain: github.com (Add domain), then Save.");
        Pause();
        string audience = WithProject(Audience, "", account);
        io.Open(audience);
        io.Say($"   Now on {audience}: click Publish app, then Confirm. Without it Google refuses the sign-in (Error 403: " +
               "access_denied); in Testing it would sign you out every 7 days.");
        Pause();
        Step(4, "Create the Desktop client", ClientCreate, account,
            "Application type: Desktop app; name: NexusAGYmail. Leave 'Use this client for an AI-powered agent' unchecked. Click Create.",
            "In the window that opens: Download JSON.");
        string? found = WaitForClient(started.AddSeconds(-5), WaitForJson);
        if (found is null)
        {
            io.Say($"   No client JSON found. Run `{GmailAuth.ReauthHint} --client-secret <file>` once you have it.");
            return null;
        }
        var saved = OAuthSignIn.SaveClient(found, io.StateDir);
        io.Say($"   Found {Path.GetFileName(found)}: saved as {saved.Target}.");
        return saved;
    }

    /// <summary>Newest Desktop client JSON (client_secret*.json) downloaded since <paramref name="sinceUtc"/>.</summary>
    public static string? NewestClient(string folder, DateTime sinceUtc)
    {
        try
        {
            return new DirectoryInfo(folder).EnumerateFiles("client_secret*.json")
                .Where(f => f.LastWriteTimeUtc >= sinceUtc)
                .OrderByDescending(f => f.LastWriteTimeUtc)
                .Select(f => f.FullName)
                .FirstOrDefault(p => OAuthSignIn.ReadDesktopClient(p) is not null);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { return null; }
    }

    /// <summary>Waits for client_secret_….json in Downloads; Ctrl+C to type its path instead.</summary>
    public string? WaitForClient(DateTime sinceUtc, TimeSpan timeout)
    {
        io.Say($"   Waiting for client_secret_….json in {io.Downloads} (Ctrl+C to type its path instead)...");
        DateTime deadline = io.UtcNow() + timeout;
        while (io.UtcNow() < deadline)
        {
            if (NewestClient(io.Downloads, sinceUtc) is { } found)
                return found;
            if (io.Token.IsCancellationRequested)
            {
                io.Rearm();
                string text = io.Ask("   Path of the downloaded client JSON: ").Trim().Trim('"');
                if (text.Length > 0 && OAuthSignIn.ReadDesktopClient(text) is not null)
                    return text;
                io.Say("   That is not a Desktop app client JSON.");
                return null;
            }
            io.Sleep(TimeSpan.FromSeconds(1));
        }
        return null;
    }

    /// <summary>Step 5: sign-in, then the checks (Gmail API, Testing mode) with the gesture that fixes them.</summary>
    public int SignIn(string project, bool openBrowser, string account, bool interactive)
    {
        var client = OAuthSignIn.LoadClientConfig(OAuthSignIn.GetClientSecretPath(io.StateDir));
        int attempts = interactive ? 3 : 1;
        for (int attempt = 0; attempt < attempts; attempt++)
        {
            io.Say("\n5/5  Sign in to Gmail");
            io.Say("   Google shows \"Google hasn't verified this app\": it is your own app. Click Advanced, then Go to " +
                   "NexusAGYmail (unsafe), and allow. 'Access blocked' (Error 403: access_denied) means the app is not " +
                   "published yet: publish it (step 3), then press Ctrl+C and run auth again.");
            io.Rearm();
            var result = io.SignIn is { } fake
                ? fake(client, openBrowser, account, io.Token)
                : new OAuthSignIn().Run(GmailAuth.GetTokenStore(), client, openBrowser, account, io.Token);
            Console.WriteLine(result.ToJsonString(OAuthSignIn.OutputJson));
            if (result["gmail_api_disabled"]?.GetValue<bool>() == true)
            {
                string url = WithProject(EnableGmail, project, account);
                io.Open(url);
                io.Say($"   The Gmail API is not on for this project yet. Click Enable on {url}, wait a minute{(interactive ? "." : ", then run auth again.")}");
                if (interactive) Pause();
                continue;
            }
            if (result["testing_mode"]?.GetValue<bool>() == true)
            {
                string url = WithProject(Audience, project, account);
                io.Open(url);
                io.Say($"   The app is still in Testing (sign-in valid 7 days only). Click Publish app on {url}{(interactive ? "." : ", then run auth again.")}");
                if (interactive) Pause();
                continue;
            }
            if (result["scopes"]?["reauth_required"]?.GetValue<bool>() == true)
            {
                io.Say($"   Scopes: {result["scopes"]?["message"]}. Allow every Gmail permission and sign in again.");
                continue;
            }
            if (result["warning"] is JsonNode warning)
            {
                io.Say(warning.GetValue<string>());
                continue;
            }
            string signedIn = result["account"]?.GetValue<string>() ?? "";
            io.Say($"\nDone: signed in as {(signedIn.Length > 0 ? signedIn : "your account")}. Restart Antigravity to use Gmail.");
            return 0;
        }
        if (interactive)
            io.Say($"\nNot finished. If you would rather not create a client, see Google's official Gmail MCP server: {Fallback}");
        return 1;
    }
}
