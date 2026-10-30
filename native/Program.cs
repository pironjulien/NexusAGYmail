using System;
using System.Text.Json.Nodes;

namespace NexusAGYmail;

public static class Program
{
    // Same version as NexusAGYmail.csproj, plugin/plugin.json and the top section of CHANGELOG.md (checked by the tests).
    public const string Version = "0.15.2";

    public static int Main(string[] args)
    {
        string command = args.Length > 0 ? args[0] : "";
        switch (command)
        {
            case "--mcp":
            case "serve":
                new McpServer().RunStdio();
                return 0;
            case "auth":
                return AuthCommand.Run(args[1..]);
            case "setup":
                return Registration.Setup(args[1..]);
            case "status":
                return RunStatus();
            case "--version":
            case "-v":
                Console.WriteLine($"NexusAGYmail {Version}");
                return 0;
            case "":
            case "--help":
            case "-h":
                PrintHelp();
                return command.Length == 0 ? 2 : 0;
            default:
                Console.Error.WriteLine($"Unknown command: {command}");
                PrintHelp();
                return 2;
        }
    }

    private static void PrintHelp()
    {
        Console.WriteLine($"NexusAGYmail {Version} - Gmail MCP server for Google Antigravity");
        Console.WriteLine("Usage:");
        Console.WriteLine("  NexusAGYmail.exe --mcp      MCP server over stdio (what Antigravity runs)");
        Console.WriteLine("  NexusAGYmail.exe setup      declare the server to Antigravity (plugin), then offer the guided sign-in");
        Console.WriteLine("  NexusAGYmail.exe auth [--client-secret FILE] [--no-browser]");
        Console.WriteLine("                              guided Google sign-in; the token stays on this PC");
        Console.WriteLine("  NexusAGYmail.exe status     token status as JSON (no network, no secret shown)");
        Console.WriteLine("  NexusAGYmail.exe --version  version");
    }

    private static int RunStatus()
    {
        JsonObject report = GmailAuth.InspectToken();
        Console.WriteLine(report.ToJsonString(OAuthSignIn.OutputJson));
        return report["present"]?.GetValue<bool>() == true && report["error"] is null ? 0 : 1;
    }
}
