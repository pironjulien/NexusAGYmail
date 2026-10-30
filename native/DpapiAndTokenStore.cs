using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace NexusAGYmail;

public sealed class DpapiError : IOException
{
    public int NativeErrorCode { get; }

    public DpapiError(int code, string message) : base($"{message} (Windows error {code})")
    {
        NativeErrorCode = code;
    }
}

public static class Dpapi
{
    // Entropie figée à l'ancien nom (NexusGmail) : compatibilité totale avec dpapi.py et les jetons existants.
    public static readonly byte[] DefaultEntropy = Encoding.ASCII.GetBytes("nexus-gmail/token/v1");
    public static readonly byte[] Dpapi1Prefix = Encoding.ASCII.GetBytes("DPAPI1:");

    private const uint CRYPTPROTECT_UI_FORBIDDEN = 0x1;
    private const uint TOKEN_QUERY = 0x0008;
    private const int TokenUser = 1;
    private const uint DACL_SECURITY_INFORMATION = 0x00000004;
    private const uint PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000;
    private const uint SDDL_REVISION_1 = 1;

    public static bool Encrypted => OperatingSystem.IsWindows();

    [StructLayout(LayoutKind.Sequential)]
    private struct DATA_BLOB
    {
        public uint cbData;
        public IntPtr pbData;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SID_AND_ATTRIBUTES
    {
        public IntPtr Sid;
        public uint Attributes;
    }

    [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CryptProtectData(
        ref DATA_BLOB pDataIn,
        string? szDataDescr,
        ref DATA_BLOB pOptionalEntropy,
        IntPtr pvReserved,
        IntPtr pPromptStruct,
        uint dwFlags,
        out DATA_BLOB pDataOut);

    [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CryptProtectData(
        ref DATA_BLOB pDataIn,
        string? szDataDescr,
        IntPtr pOptionalEntropy,
        IntPtr pvReserved,
        IntPtr pPromptStruct,
        uint dwFlags,
        out DATA_BLOB pDataOut);

    [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CryptUnprotectData(
        ref DATA_BLOB pDataIn,
        IntPtr ppszDataDescr,
        ref DATA_BLOB pOptionalEntropy,
        IntPtr pvReserved,
        IntPtr pPromptStruct,
        uint dwFlags,
        out DATA_BLOB pDataOut);

    [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CryptUnprotectData(
        ref DATA_BLOB pDataIn,
        IntPtr ppszDataDescr,
        IntPtr pOptionalEntropy,
        IntPtr pvReserved,
        IntPtr pPromptStruct,
        uint dwFlags,
        out DATA_BLOB pDataOut);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr LocalFree(IntPtr hMem);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(IntPtr ProcessHandle, uint DesiredAccess, out IntPtr TokenHandle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(
        IntPtr TokenHandle,
        int TokenInformationClass,
        IntPtr TokenInformation,
        uint TokenInformationLength,
        out uint ReturnLength);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool ConvertSidToStringSidW(IntPtr Sid, out IntPtr StringSid);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(
        string StringSecurityDescriptor,
        uint StringSDRevision,
        out IntPtr SecurityDescriptor,
        IntPtr SecurityDescriptorSize);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetFileSecurityW(
        string lpFileName,
        uint SecurityInformation,
        IntPtr pSecurityDescriptor);

    public static unsafe byte[] Protect(ReadOnlySpan<byte> data, ReadOnlySpan<byte> entropy = default, bool useDefaultEntropy = true)
    {
        if (!Encrypted)
            return data.ToArray();

        ReadOnlySpan<byte> actualEntropy = entropy.IsEmpty && useDefaultEntropy ? DefaultEntropy : entropy;
        byte dummyData = 0;
        byte dummyEnt = 0;

        fixed (byte* pData = data.IsEmpty ? new ReadOnlySpan<byte>(&dummyData, 1) : data)
        fixed (byte* pEnt = actualEntropy.IsEmpty ? new ReadOnlySpan<byte>(&dummyEnt, 1) : actualEntropy)
        {
            var inBlob = new DATA_BLOB
            {
                cbData = (uint)data.Length,
                pbData = (IntPtr)pData,
            };

            bool ok;
            DATA_BLOB outBlob;
            if (!actualEntropy.IsEmpty)
            {
                var entBlob = new DATA_BLOB
                {
                    cbData = (uint)actualEntropy.Length,
                    pbData = (IntPtr)pEnt,
                };
                ok = CryptProtectData(ref inBlob, "nexus-agymail", ref entBlob, IntPtr.Zero, IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, out outBlob);
            }
            else
            {
                ok = CryptProtectData(ref inBlob, "nexus-agymail", IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, out outBlob);
            }

            if (!ok)
                throw new DpapiError(Marshal.GetLastWin32Error(), "DPAPI encryption refused");

            try
            {
                byte[] result = new byte[outBlob.cbData];
                if (outBlob.cbData > 0)
                    Marshal.Copy(outBlob.pbData, result, 0, (int)outBlob.cbData);
                return result;
            }
            finally
            {
                if (outBlob.pbData != IntPtr.Zero)
                    LocalFree(outBlob.pbData);
            }
        }
    }

    public static byte[] ProtectWithPrefix(ReadOnlySpan<byte> data, ReadOnlySpan<byte> entropy = default, bool useDefaultEntropy = true)
    {
        byte[] raw = Protect(data, entropy, useDefaultEntropy);
        byte[] outBytes = new byte[Dpapi1Prefix.Length + raw.Length];
        Buffer.BlockCopy(Dpapi1Prefix, 0, outBytes, 0, Dpapi1Prefix.Length);
        Buffer.BlockCopy(raw, 0, outBytes, Dpapi1Prefix.Length, raw.Length);
        return outBytes;
    }

    public static byte[] Unprotect(ReadOnlySpan<byte> data, ReadOnlySpan<byte> entropy = default, bool useDefaultEntropy = true)
    {
        if (!Encrypted)
            return data.ToArray();

        if (data.StartsWith(Dpapi1Prefix))
        {
            var stripped = data[Dpapi1Prefix.Length..];
            if (TryUnprotectCore(stripped, entropy, useDefaultEntropy, out byte[]? plain, out _))
                return plain!;
            // Support base64-encoded payload after DPAPI1: if used
            try
            {
                var b64Text = Encoding.ASCII.GetString(stripped).Trim();
                byte[] decoded = Convert.FromBase64String(b64Text);
                if (TryUnprotectCore(decoded, entropy, useDefaultEntropy, out plain, out _))
                    return plain!;
            }
            catch
            {
                // Fall through to raw attempt
            }
        }

        if (TryUnprotectCore(data, entropy, useDefaultEntropy, out byte[]? result, out int err))
            return result!;

        throw new DpapiError(err, "DPAPI decryption refused (another user, another PC or a damaged file)");
    }

    private static unsafe bool TryUnprotectCore(
        ReadOnlySpan<byte> data,
        ReadOnlySpan<byte> entropy,
        bool useDefaultEntropy,
        out byte[]? result,
        out int errorCode)
    {
        ReadOnlySpan<byte> actualEntropy = entropy.IsEmpty && useDefaultEntropy ? DefaultEntropy : entropy;
        byte dummyData = 0;
        byte dummyEnt = 0;

        fixed (byte* pData = data.IsEmpty ? new ReadOnlySpan<byte>(&dummyData, 1) : data)
        fixed (byte* pEnt = actualEntropy.IsEmpty ? new ReadOnlySpan<byte>(&dummyEnt, 1) : actualEntropy)
        {
            var inBlob = new DATA_BLOB
            {
                cbData = (uint)data.Length,
                pbData = (IntPtr)pData,
            };

            bool ok;
            DATA_BLOB outBlob;
            if (!actualEntropy.IsEmpty)
            {
                var entBlob = new DATA_BLOB
                {
                    cbData = (uint)actualEntropy.Length,
                    pbData = (IntPtr)pEnt,
                };
                ok = CryptUnprotectData(ref inBlob, IntPtr.Zero, ref entBlob, IntPtr.Zero, IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, out outBlob);
            }
            else
            {
                ok = CryptUnprotectData(ref inBlob, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, CRYPTPROTECT_UI_FORBIDDEN, out outBlob);
            }

            if (!ok)
            {
                errorCode = Marshal.GetLastWin32Error();
                result = null;
                return false;
            }

            try
            {
                result = new byte[outBlob.cbData];
                if (outBlob.cbData > 0)
                    Marshal.Copy(outBlob.pbData, result, 0, (int)outBlob.cbData);
                errorCode = 0;
                return true;
            }
            finally
            {
                if (outBlob.pbData != IntPtr.Zero)
                    LocalFree(outBlob.pbData);
            }
        }
    }

    public static string CurrentUserSid()
    {
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, out IntPtr token))
            throw new DpapiError(Marshal.GetLastWin32Error(), "OpenProcessToken refused");
        try
        {
            GetTokenInformation(token, TokenUser, IntPtr.Zero, 0, out uint size);
            IntPtr buffer = Marshal.AllocHGlobal((int)size);
            try
            {
                if (!GetTokenInformation(token, TokenUser, buffer, size, out _))
                    throw new DpapiError(Marshal.GetLastWin32Error(), "GetTokenInformation refused");
                var sidAttr = Marshal.PtrToStructure<SID_AND_ATTRIBUTES>(buffer);
                if (!ConvertSidToStringSidW(sidAttr.Sid, out IntPtr strPtr))
                    throw new DpapiError(Marshal.GetLastWin32Error(), "ConvertSidToStringSidW refused");
                try
                {
                    return Marshal.PtrToStringUni(strPtr) ?? "";
                }
                finally
                {
                    LocalFree(strPtr);
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }
        finally
        {
            CloseHandle(token);
        }
    }

    public static void RestrictToCurrentUser(string path)
    {
        if (!Encrypted)
            return;

        string sid = CurrentUserSid();
        string sddl = $"D:P(A;;FA;;;{sid})";
        if (ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1, out IntPtr descriptor, IntPtr.Zero))
        {
            try
            {
                uint flags = DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION;
                if (SetFileSecurityW(path, flags, descriptor))
                    return;
            }
            finally
            {
                LocalFree(descriptor);
            }
        }

        // Fallback via icacls if SetFileSecurityW fails
        try
        {
            var psi = new ProcessStartInfo("icacls")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            psi.ArgumentList.Add(path);
            psi.ArgumentList.Add("/inheritance:r");
            psi.ArgumentList.Add("/grant:r");
            psi.ArgumentList.Add($"*{sid}:(F)");
            using var proc = Process.Start(psi);
            proc?.WaitForExit(5000);
        }
        catch (Exception ex)
        {
            throw new DpapiError(Marshal.GetLastWin32Error(), $"permissions not applied to {path}: {ex.Message}");
        }
    }
}

public sealed class TokenError : Exception
{
    public string Code { get; }

    public TokenError(string code, string message) : base(message)
    {
        Code = code;
    }
}

public sealed class TokenStore
{
    public const double DefaultLockTimeoutSeconds = 30.0;

    public string DirectoryPath { get; }
    public string TokenPath { get; }
    public string LegacyPath { get; }
    public string LockPath { get; }
    public double LockTimeoutSeconds { get; }

    private static readonly ThreadLocal<Dictionary<string, int>> LockDepth =
        new(() => new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase));

    private static readonly JsonSerializerOptions CompactJsonOptions = new()
    {
        WriteIndented = false,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public TokenStore(string directory, double lockTimeout = DefaultLockTimeoutSeconds)
    {
        DirectoryPath = Path.GetFullPath(directory);
        TokenPath = Path.Combine(DirectoryPath, "token.bin");
        LegacyPath = Path.Combine(DirectoryPath, "token.json");
        LockPath = Path.Combine(DirectoryPath, "token.lock");
        LockTimeoutSeconds = lockTimeout;
    }

    public IDisposable Lock()
    {
        var depths = LockDepth.Value!;
        if (depths.TryGetValue(LockPath, out int depth) && depth > 0)
        {
            depths[LockPath] = depth + 1;
            return new ReentrantHandle(LockPath, null);
        }

        Directory.CreateDirectory(DirectoryPath);
        bool created = !File.Exists(LockPath);
        var fs = new FileStream(LockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite);
        try
        {
            if (created)
            {
                try { Dpapi.RestrictToCurrentUser(LockPath); } catch { }
            }

            var sw = Stopwatch.StartNew();
            while (true)
            {
                try
                {
                    fs.Lock(0, 1);
                    break;
                }
                catch (IOException)
                {
                    if (sw.Elapsed.TotalSeconds >= LockTimeoutSeconds)
                    {
                        throw new TokenError(
                            "locked",
                            $"token held by another nexus-agymail server for {LockTimeoutSeconds:0} s ({LockPath}): try again");
                    }
                    Thread.Sleep(50);
                }
            }

            depths[LockPath] = 1;
            return new ReentrantHandle(LockPath, fs);
        }
        catch
        {
            fs.Dispose();
            throw;
        }
    }

    private sealed class ReentrantHandle(string lockPath, FileStream? stream) : IDisposable
    {
        public void Dispose()
        {
            var depths = LockDepth.Value!;
            if (depths.TryGetValue(lockPath, out int d))
            {
                if (d <= 1)
                    depths.Remove(lockPath);
                else
                    depths[lockPath] = d - 1;
            }

            if (stream is not null)
            {
                try { stream.Unlock(0, 1); } catch { }
                stream.Dispose();
            }
        }
    }

    public string? Describe()
    {
        if (File.Exists(TokenPath))
            return Dpapi.Encrypted ? "token.bin (DPAPI-encrypted)" : "token.bin (readable by your account only)";
        if (File.Exists(LegacyPath))
            return "token.json (plain text, migrated on first load)";
        return null;
    }

    public JsonObject? Load()
    {
        using (Lock())
        {
            if (File.Exists(TokenPath))
                return ReadEncrypted();
            if (File.Exists(LegacyPath))
                return MigrateLegacy();
            return null;
        }
    }

    public void Save(JsonObject info)
    {
        using (Lock())
        {
            WriteEncrypted(info);
        }
    }

    private JsonObject ReadEncrypted()
    {
        byte[] plain;
        try
        {
            plain = Dpapi.Unprotect(File.ReadAllBytes(TokenPath));
        }
        catch (DpapiError ex)
        {
            throw new TokenError("unreadable", $"unreadable token ({ex.Message}): sign in again");
        }
        return ParseTokenJson(Encoding.UTF8.GetString(plain), Path.GetFileName(TokenPath));
    }

    private void WriteEncrypted(JsonObject info)
    {
        Directory.CreateDirectory(DirectoryPath);
        string json = info.ToJsonString(CompactJsonOptions);
        byte[] blob = Dpapi.Protect(Encoding.UTF8.GetBytes(json));
        string rand = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(4));
        string tmp = Path.Combine(DirectoryPath, $"token.bin.{Environment.ProcessId}.{rand}.tmp");
        try
        {
            using (var fs = new FileStream(tmp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                Dpapi.RestrictToCurrentUser(tmp);
                fs.Write(blob, 0, blob.Length);
                fs.Flush(flushToDisk: true);
            }
            File.Move(tmp, TokenPath, overwrite: true);
        }
        finally
        {
            try
            {
                if (File.Exists(tmp))
                    File.Delete(tmp);
            }
            catch
            {
                // Ignore cleanup failure
            }
        }
    }

    private JsonObject MigrateLegacy()
    {
        string rawText = File.ReadAllText(LegacyPath, Encoding.UTF8);
        JsonObject info = ParseTokenJson(rawText, Path.GetFileName(LegacyPath));
        WriteEncrypted(info);
        JsonObject again;
        try
        {
            again = ReadEncrypted();
        }
        catch (TokenError ex)
        {
            throw new TokenError("migration", $"migration to token.bin not read back ({ex.Message}): token.json kept");
        }

        if (again.ToJsonString(CompactJsonOptions) != info.ToJsonString(CompactJsonOptions))
            throw new TokenError("migration", "migration to token.bin read back different: token.json kept");

        File.Delete(LegacyPath);
        return info;
    }

    public static JsonObject ParseTokenJson(string text, string sourceName)
    {
        JsonNode? node;
        try
        {
            node = JsonNode.Parse(text);
        }
        catch (JsonException)
        {
            throw new TokenError("corrupt", $"corrupt token ({sourceName}): sign in again");
        }

        if (node is not JsonObject obj ||
            obj["refresh_token"] is not JsonValue rt ||
            !rt.TryGetValue<string>(out string? refreshToken) ||
            string.IsNullOrWhiteSpace(refreshToken))
        {
            throw new TokenError("corrupt", $"incomplete token ({sourceName}, no refresh_token): sign in again");
        }

        return obj;
    }
}

public sealed class GmailAuthError : Exception
{
    public string Code { get; }

    public GmailAuthError(string code, string message) : base(message)
    {
        Code = code;
    }
}

public sealed record AuthCredentials(
    string AccessToken,
    string RefreshToken,
    bool Valid,
    string? ExpiryIso,
    string Account,
    List<string> Scopes);

public static class GmailAuth
{
    public const string ModifyScope = "https://www.googleapis.com/auth/gmail.modify";
    public static readonly string[] RequiredScopes =
    [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        ModifyScope,
    ];

    public const string ModifyTools =
        "label_message, unlabel_message, label_thread, unlabel_thread, trash_message, trash_thread, untrash_message, untrash_thread";
    // Commande native de connexion : le chemin exact de cet exécutable quand il est connu (copie versionnée de
    // l'installeur), sinon le nom seul.
    public static readonly string ReauthHint = NativeAuthCommand(Environment.ProcessPath);

    public static string NativeAuthCommand(string? processPath)
    {
        if (string.IsNullOrEmpty(processPath) ||
            !string.Equals(Path.GetFileName(processPath), "NexusAGYmail.exe", StringComparison.OrdinalIgnoreCase))
            return "NexusAGYmail.exe auth";
        return processPath.Contains(' ') ? $"& \"{processPath}\" auth" : $"{processPath} auth";
    }
    public const string StateEnv = "NEXUS_AGYMAIL_HOME";
    public const string AccountEnv = "NEXUS_AGYMAIL_ACCOUNT";
    public const string ClientSecretEnv = "NEXUS_AGYMAIL_CLIENT_SECRET";

    private static readonly HttpClient SharedHttp = new(new SocketsHttpHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(20) };

    public static string HomeDir()
    {
        return Environment.GetEnvironmentVariable("USERPROFILE")
            ?? Environment.GetEnvironmentVariable("HOME")
            ?? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
    }

    public static string GetNexusDir()
    {
        string? over = Environment.GetEnvironmentVariable(StateEnv);
        string path = !string.IsNullOrWhiteSpace(over)
            ? Path.GetFullPath(over)
            : Path.Combine(HomeDir(), ".nexus", "nexus-agymail");
        Directory.CreateDirectory(path);
        return path;
    }

    public static TokenStore GetTokenStore() => new(GetNexusDir());

    public static List<string> GrantedScopes(JsonObject? info)
    {
        var list = new List<string>();
        if (info is null)
            return list;
        var node = info["scopes"];
        if (node is JsonValue val && val.TryGetValue<string>(out string? str))
        {
            list.AddRange(str.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
        }
        else if (node is JsonArray arr)
        {
            foreach (var item in arr)
            {
                if (item?.GetValue<string>() is { Length: > 0 } s)
                    list.Add(s);
            }
        }
        list.Sort(StringComparer.Ordinal);
        return list;
    }

    public static JsonObject ScopeReport(JsonObject info)
    {
        var granted = GrantedScopes(info);
        var missing = RequiredScopes.Where(s => !granted.Contains(s, StringComparer.Ordinal)).ToList();
        var excess = granted.Where(s => !RequiredScopes.Contains(s, StringComparer.Ordinal)).ToList();
        bool reauth = missing.Count > 0 || excess.Count > 0;

        var report = new JsonObject
        {
            ["granted"] = new JsonArray(granted.Select(s => (JsonNode)JsonValue.Create(s)!).ToArray()),
            ["required"] = new JsonArray(RequiredScopes.Select(s => (JsonNode)JsonValue.Create(s)!).ToArray()),
            ["missing"] = new JsonArray(missing.Select(s => (JsonNode)JsonValue.Create(s)!).ToArray()),
            ["excess"] = new JsonArray(excess.Select(s => (JsonNode)JsonValue.Create(s)!).ToArray()),
            ["reauth_required"] = reauth,
        };

        if (missing.Contains(ModifyScope, StringComparer.Ordinal))
        {
            var blocked = ModifyTools.Split(", ", StringSplitOptions.RemoveEmptyEntries);
            report["blocked_tools"] = new JsonArray(blocked.Select(s => (JsonNode)JsonValue.Create(s)!).ToArray());
        }

        if (excess.Count > 0)
        {
            report["message"] =
                $"the token carries extra scopes ({string.Join(", ", excess)}): sign in again ({ReauthHint}); meanwhile the tools keep working";
        }
        else if (missing.Count == 1 && missing[0] == ModifyScope)
        {
            report["message"] =
                $"token without gmail.modify: sign in again ({ReauthHint}) for the labelling and trash tools ({ModifyTools}); reading, drafts and sending keep working";
        }
        else if (missing.Count > 0)
        {
            report["message"] = $"missing scopes ({string.Join(", ", missing)}): sign in again ({ReauthHint})";
        }

        return report;
    }

    public static string? ModifyScopeProblem()
    {
        JsonObject? info;
        try
        {
            info = GetTokenStore().Load();
        }
        catch (TokenError)
        {
            return null;
        }

        if (info is null)
            return null;
        var granted = GrantedScopes(info);
        if (granted.Count == 0 || granted.Contains(ModifyScope, StringComparer.Ordinal))
            return null;
        return $"the token lacks the gmail.modify scope: the user signs in again ({ReauthHint}) for {ModifyTools}; reading and drafts still work";
    }

    public static string AccountOf(JsonObject? info)
    {
        string? envAcc = Environment.GetEnvironmentVariable(AccountEnv);
        if (!string.IsNullOrEmpty(envAcc))
            return envAcc;
        return info?["account"]?.GetValue<string>() ?? "";
    }

    public static JsonObject InspectToken()
    {
        var store = GetTokenStore();
        string? storage = store.Describe();
        if (storage is null)
        {
            return new JsonObject
            {
                ["present"] = false,
                ["message"] = $"no token ({store.TokenPath}): sign in first ({ReauthHint})",
            };
        }

        JsonObject? info;
        try
        {
            info = store.Load();
        }
        catch (TokenError ex)
        {
            return new JsonObject
            {
                ["present"] = true,
                ["storage"] = storage,
                ["error"] = ex.Message,
                ["code"] = ex.Code,
            };
        }

        if (info is null)
        {
            return new JsonObject
            {
                ["present"] = false,
                ["message"] = $"no token ({store.TokenPath})",
            };
        }

        return new JsonObject
        {
            ["present"] = true,
            ["storage"] = store.Describe(),
            ["account"] = AccountOf(info),
            ["scopes"] = ScopeReport(info),
        };
    }

    public static AuthCredentials GetCredentials()
    {
        var store = GetTokenStore();
        try
        {
            using (store.Lock())
            {
                var info = store.Load();
                if (info is null)
                    throw new GmailAuthError("no_token", $"no token ({store.TokenPath}): sign in first ({ReauthHint})");

                string token = info["token"]?.GetValue<string>() ?? "";
                string refreshToken = info["refresh_token"]?.GetValue<string>() ?? "";
                string clientId = info["client_id"]?.GetValue<string>() ?? "";
                string clientSecret = info["client_secret"]?.GetValue<string>() ?? "";
                string tokenUri = TokenUriOf(info);

                if (string.IsNullOrEmpty(refreshToken) || string.IsNullOrEmpty(clientId) || string.IsNullOrEmpty(clientSecret))
                {
                    throw new GmailAuthError("corrupt", $"incomplete token: sign in again ({ReauthHint})");
                }

                string? expiryStr = info["expiry"]?.GetValue<string>();
                bool isValid = false;
                string? normalizedExpiry = null;

                if (!string.IsNullOrEmpty(token) && !string.IsNullOrEmpty(expiryStr))
                {
                    if (DateTimeOffset.TryParse(expiryStr, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var dto))
                    {
                        normalizedExpiry = dto.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture) + "Z";
                        if (dto.UtcDateTime > DateTime.UtcNow.AddSeconds(10))
                            isValid = true;
                    }
                }
                else if (!string.IsNullOrEmpty(token) && string.IsNullOrEmpty(expiryStr))
                {
                    isValid = true;
                }

                if (isValid)
                {
                    return new AuthCredentials(
                        token,
                        refreshToken,
                        true,
                        normalizedExpiry,
                        AccountOf(info),
                        GrantedScopes(info));
                }

                // Refresh OAuth2 token
                var refreshed = RequestRefreshAsync(SharedHttp, tokenUri, refreshToken, clientId, clientSecret, CancellationToken.None)
                    .GetAwaiter().GetResult();
                string newAccess = refreshed["access_token"]!.GetValue<string>();

                int expiresIn = refreshed["expires_in"]?.GetValue<int>() ?? 3600;
                var newExpiry = DateTime.UtcNow.AddSeconds(expiresIn);
                normalizedExpiry = newExpiry.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture) + "Z";

                info["token"] = newAccess;
                info["expiry"] = normalizedExpiry;
                if (refreshed["refresh_token"]?.GetValue<string>() is { Length: > 0 } newRt)
                    info["refresh_token"] = newRt;

                store.Save(info);
                return new AuthCredentials(
                    newAccess,
                    info["refresh_token"]!.GetValue<string>(),
                    true,
                    normalizedExpiry,
                    AccountOf(info),
                    GrantedScopes(info));
            }
        }
        catch (TokenError ex)
        {
            throw new GmailAuthError(ex.Code, ex.Message);
        }
    }

    /// <summary>Adresse de renouvellement d'un jeton (NEXUS_AGYMAIL_TOKEN_URI pour les tests, sinon token_uri).</summary>
    public static string TokenUriOf(JsonObject info)
    {
        return Environment.GetEnvironmentVariable("NEXUS_AGYMAIL_TOKEN_URI")
            ?? info["token_uri"]?.GetValue<string>()
            ?? "https://oauth2.googleapis.com/token";
    }

    /// <summary>
    /// Renouvelle un jeton d'accès (grant refresh_token) ; rend la réponse de Google, qui porte toujours un
    /// access_token. Erreurs : network, revoked (invalid_grant), refused.
    /// </summary>
    public static async Task<JsonObject> RequestRefreshAsync(
        HttpClient http, string tokenUri, string refreshToken, string clientId, string clientSecret, CancellationToken ct)
    {
        var form = new Dictionary<string, string>
        {
            ["grant_type"] = "refresh_token",
            ["refresh_token"] = refreshToken,
            ["client_id"] = clientId,
            ["client_secret"] = clientSecret,
        };

        HttpResponseMessage response;
        string respBody;
        try
        {
            using var req = new HttpRequestMessage(HttpMethod.Post, tokenUri)
            {
                Content = new FormUrlEncodedContent(form),
            };
            response = await http.SendAsync(req, ct).ConfigureAwait(false);
            respBody = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex)
        {
            throw new GmailAuthError("network", $"network: the token could not be refreshed ({ex.Message})");
        }

        using (response)
        {
            if (!response.IsSuccessStatusCode)
            {
                if (respBody.Contains("invalid_grant", StringComparison.OrdinalIgnoreCase))
                    throw new GmailAuthError("revoked", $"token revoked or expired: sign in again ({ReauthHint})");
                throw new GmailAuthError("refused", $"token refresh refused by Google (HTTP {(int)response.StatusCode}); sign in again");
            }
        }

        JsonObject? refreshed;
        try
        {
            refreshed = JsonNode.Parse(respBody) as JsonObject;
        }
        catch (JsonException)
        {
            refreshed = null;
        }
        if (refreshed is null)
            throw new GmailAuthError("refused", "invalid token response from Google");
        if (refreshed["access_token"] is not JsonValue at || !at.TryGetValue<string>(out string? access) || string.IsNullOrEmpty(access))
            throw new GmailAuthError("refused", "missing access_token in Google refresh response");
        return refreshed;
    }
}
