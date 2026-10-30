using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Mail;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NexusAGYmail;

public static class AttachmentSecurity
{
    public const int MaxAttachBytes = 25 * 1024 * 1024;
    public const int MaxNameChars = 150;
    public const string ProtectedEnv = "NEXUS_AGYMAIL_PROTECTED";
    public const string ZoneIdentifierContent = "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://mail.google.com/\r\n";

    private static readonly Regex InvisibleRegex = new(@"[\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]", RegexOptions.Compiled);
    private static readonly Regex ForbiddenRegex = new(@"[<>:""/\\|?*\x00-\x1f\x7f]", RegexOptions.Compiled);
    private static readonly Regex ReservedRegex = new(@"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", RegexOptions.IgnoreCase | RegexOptions.Compiled);

    private static readonly string[] SecretDirs =
    {
        ".ssh", ".gnupg", ".nexus", ".aws", ".azure", ".kube", ".docker", ".config/gcloud", ".config/gh", ".codex",
        ".gemini", ".claude", ".antigravity", ".cursor", ".vscode-server", ".password-store"
    };

    private static readonly HashSet<string> SecretNames = new(StringComparer.OrdinalIgnoreCase)
    {
        ".credentials.json", "credentials.json", "client_secret.json", "token.json", "token.bin", ".claude.json",
        ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".dockercfg", "id_rsa", "id_ed25519", "id_ecdsa"
    };

    private static readonly string[] SecretSuffixes =
    {
        ".pem", ".key", ".pfx", ".p12", ".kdbx", ".keystore", ".jks", ".ppk"
    };

    private static readonly string[] EnvExamples = { ".example", ".sample", ".template", ".dist" };

    public static string DefaultDownloadDirectory()
    {
        string userProfile = GmailAuth.HomeDir();
        return Path.Combine(userProfile, "Downloads", "NexusAGYmail");
    }

    public static bool IsSecretName(string filename)
    {
        string lower = filename.ToLowerInvariant();
        if (SecretNames.Contains(lower)) return true;
        foreach (var sfx in SecretSuffixes)
        {
            if (lower.EndsWith(sfx, StringComparison.OrdinalIgnoreCase)) return true;
        }
        if (lower == ".env" || lower.StartsWith(".env.", StringComparison.Ordinal) || lower.EndsWith(".env", StringComparison.Ordinal))
        {
            foreach (var ex in EnvExamples)
            {
                if (lower.EndsWith(ex, StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
        }
        return false;
    }

    public static bool IsProtectedPath(string fullPath)
    {
        try
        {
            string resolved = Canonical(fullPath);
            string filename = Path.GetFileName(resolved);
            if (IsSecretName(filename)) return true;

            string userProfile = GmailAuth.HomeDir();
            foreach (var relDir in SecretDirs)
            {
                string secretDirPath = Canonical(Path.Combine(userProfile, relDir.Replace('/', Path.DirectorySeparatorChar)));
                if (resolved.StartsWith(secretDirPath, StringComparison.OrdinalIgnoreCase))
                {
                    if (resolved.Length == secretDirPath.Length || resolved[secretDirPath.Length] == Path.DirectorySeparatorChar)
                        return true;
                }
            }

            string? extra = Environment.GetEnvironmentVariable(ProtectedEnv);
            if (!string.IsNullOrEmpty(extra))
            {
                var parts = extra.Split(new[] { Path.PathSeparator, ';' }, StringSplitOptions.RemoveEmptyEntries);
                foreach (var p in parts)
                {
                    string extraPath = Canonical(p.Trim());
                    if (resolved.StartsWith(extraPath, StringComparison.OrdinalIgnoreCase))
                    {
                        if (resolved.Length == extraPath.Length || resolved[extraPath.Length] == Path.DirectorySeparatorChar)
                            return true;
                    }
                }
            }
        }
        catch
        {
            return true;
        }

        return false;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandleW(Microsoft.Win32.SafeHandles.SafeFileHandle file, StringBuilder path, uint length, uint flags);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint creation, uint attributes, IntPtr template);

    private static string FinalPath(Microsoft.Win32.SafeHandles.SafeFileHandle handle)
    {
        var target = new StringBuilder(32768);
        uint length = GetFinalPathNameByHandleW(handle, target, (uint)target.Capacity, 0);
        if (length == 0 || length >= target.Capacity) throw new IOException("Attachment path could not be resolved");
        string resolved = target.ToString();
        if (resolved.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + resolved[8..];
        return resolved.StartsWith(@"\\?\", StringComparison.Ordinal) ? resolved[4..] : resolved;
    }

    private static string Canonical(string path)
    {
        string resolved = Path.GetFullPath(path);
        // Resolve parent junctions as well as a final symlink, using the same handle Windows opens for the read.
        if (OperatingSystem.IsWindows())
        {
            if (File.Exists(resolved) || Directory.Exists(resolved))
            {
                using var handle = CreateFileW(resolved, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
                if (handle.IsInvalid) throw new IOException("Path could not be opened for canonical resolution");
                return FinalPath(handle);
            }
            string? parent = Path.GetDirectoryName(resolved);
            if (parent is not null && parent != resolved) return Path.Combine(Canonical(parent), Path.GetFileName(resolved));
        }
        if (resolved.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) return @"\\" + resolved[8..];
        return resolved.StartsWith(@"\\?\", StringComparison.Ordinal) ? resolved[4..] : resolved;
    }

    public static string SafeName(string name, string fallback = "attachment")
    {
        string text = InvisibleRegex.Replace(name ?? "", "");
        text = text.Replace('/', '\\');
        int lastSlash = text.LastIndexOf('\\');
        if (lastSlash >= 0) text = text.Substring(lastSlash + 1);

        text = ForbiddenRegex.Replace(text, "_").Trim().TrimEnd('.', ' ');
        text = text.TrimStart('.');
        if (string.IsNullOrEmpty(text)) text = fallback;

        if (ReservedRegex.IsMatch(text))
        {
            text = "_" + text;
        }

        if (text.Length > MaxNameChars)
        {
            int lastDot = text.LastIndexOf('.');
            string stem = lastDot > 0 ? text.Substring(0, lastDot) : text;
            string ext = lastDot > 0 ? text.Substring(lastDot) : "";
            if (ext.Length > 16)
            {
                stem = text;
                ext = "";
            }
            int available = MaxNameChars - ext.Length;
            if (available < 1) available = 1;
            text = (stem.Length > available ? stem.Substring(0, available) : stem) + ext;
        }

        return text;
    }

    public static string GetFreePath(string directory, string filename)
    {
        string fullPath = Path.Combine(directory, filename);
        if (!File.Exists(fullPath)) return fullPath;

        string stem = Path.GetFileNameWithoutExtension(filename);
        string ext = Path.GetExtension(filename);

        for (int i = 1; i < 10000; i++)
        {
            string candidate = Path.Combine(directory, $"{stem} ({i}){ext}");
            if (!File.Exists(candidate)) return candidate;
        }

        return Path.Combine(directory, $"{stem} ({Guid.NewGuid():N}){ext}");
    }

    public static string SaveDownloadedAttachment(string targetDir, string filename, byte[] content)
    {
        if (string.IsNullOrWhiteSpace(targetDir))
            targetDir = DefaultDownloadDirectory();
        else if (!Path.IsPathFullyQualified(targetDir))
            throw new ArgumentException("Download directory must be an absolute path");

        if (IsProtectedPath(targetDir)) throw new ArgumentException("Cannot download into a protected directory");
        Directory.CreateDirectory(targetDir);
        string cleanName = SafeName(filename);
        string freePath;
        string tempPath = Path.Combine(targetDir, ".nexus-agymail-" + Guid.NewGuid().ToString("N") + ".part");
        try
        {
            File.WriteAllBytes(tempPath, content);
            while (true)
            {
                freePath = GetFreePath(targetDir, cleanName);
                try { File.Move(tempPath, freePath); break; }
                catch (IOException) when (File.Exists(freePath)) { /* Another download took the name: choose again, never overwrite. */ }
            }
        }
        finally { if (File.Exists(tempPath)) File.Delete(tempPath); }

        if (OperatingSystem.IsWindows())
        {
            try
            {
                string zoneStream = freePath + ":Zone.Identifier";
                File.WriteAllText(zoneStream, ZoneIdentifierContent, Encoding.ASCII);
            }
            catch
            {
                // Silently ignore if alternate data streams are not supported (e.g. FAT32)
            }
        }

        return freePath;
    }

    public static (string Filename, string MimeType, byte[] Content) ValidateAttachmentForSend(string filePath)
    {
        if (string.IsNullOrWhiteSpace(filePath))
            throw new ArgumentException("attachment path cannot be empty");
        if (!Path.IsPathFullyQualified(filePath)) throw new ArgumentException("Attachment path must be absolute");

        string resolved = Canonical(filePath);
        if (!File.Exists(resolved))
            throw new FileNotFoundException($"attachment file not found: {filePath}");

        if (IsProtectedPath(resolved))
            throw new InvalidOperationException($"security: attachment from protected or secret path is forbidden ({filePath})");

        using var stream = new FileStream(resolved, FileMode.Open, FileAccess.Read, FileShare.Read);
        string opened = OperatingSystem.IsWindows() ? FinalPath(stream.SafeFileHandle) : resolved;
        if (IsProtectedPath(opened)) throw new InvalidOperationException("Opened attachment is in a protected location");
        if (stream.Length > MaxAttachBytes) throw new InvalidOperationException("Attachment exceeds maximum size of 25 MB");
        byte[] bytes = new byte[checked((int)stream.Length)]; stream.ReadExactly(bytes);
        string filename = Path.GetFileName(resolved);
        string ext = Path.GetExtension(resolved).ToLowerInvariant();
        string mimeType = GetMimeTypeFromExtension(ext);

        return (filename, mimeType, bytes);
    }

    public static string GetMimeTypeFromExtension(string extension)
    {
        return extension switch
        {
            ".txt" => "text/plain",
            ".html" or ".htm" => "text/html",
            ".pdf" => "application/pdf",
            ".jpg" or ".jpeg" => "image/jpeg",
            ".png" => "image/png",
            ".gif" => "image/gif",
            ".webp" => "image/webp",
            ".json" => "application/json",
            ".xml" => "application/xml",
            ".zip" => "application/zip",
            ".csv" => "text/csv",
            ".eml" => "message/rfc822",
            _ => "application/octet-stream"
        };
    }
}

public static class MimeHelper
{
    private static readonly Regex InvisibleRegex = new(@"[\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]", RegexOptions.Compiled);

    public static string CleanInvisible(string text)
    {
        return text == null ? "" : InvisibleRegex.Replace(text, "");
    }

    public static string Base64UrlEncode(byte[] input)
    {
        string b64 = Convert.ToBase64String(input);
        return b64.TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }

    public static byte[] Base64UrlDecode(string input)
    {
        string padded = input.Replace('-', '+').Replace('_', '/');
        int mod = padded.Length % 4;
        if (mod == 2) padded += "==";
        else if (mod == 3) padded += "=";
        else if (mod == 1) padded += "===";
        return Convert.FromBase64String(padded);
    }

    public static string NormalizeAddresses(string? addresses, string fieldName, bool required = false)
    {
        if (string.IsNullOrWhiteSpace(addresses))
        {
            if (required) throw new ArgumentException($"{fieldName}: at least one address is required");
            return "";
        }

        if (addresses.Contains('\r') || addresses.Contains('\n'))
            throw new ArgumentException($"{fieldName}: line breaks are forbidden");

        var clean = CleanInvisible(addresses).Trim();
        var parts = clean.Split(new[] { ',', ';' }, StringSplitOptions.RemoveEmptyEntries);
        var resultList = new List<string>();

        foreach (var p in parts)
        {
            string item = p.Trim();
            if (string.IsNullOrEmpty(item)) continue;
            try
            {
                var addr = new MailAddress(item);
                resultList.Add(addr.ToString());
            }
            catch (FormatException ex)
            {
                throw new ArgumentException($"{fieldName}: invalid email address '{item}' ({ex.Message})");
            }
        }

        if (required && resultList.Count == 0)
            throw new ArgumentException($"{fieldName}: at least one valid email address is required");

        return string.Join(", ", resultList);
    }

    public static byte[] BuildRfc2822(
        string to,
        string subject,
        string body,
        string? cc = null,
        string? bcc = null,
        string? inReplyTo = null,
        string? references = null,
        List<(string Name, string MimeType, byte[] Content)>? attachments = null)
    {
        var sb = new StringBuilder();
        string boundary = "----=_Part_" + Guid.NewGuid().ToString("N");

        sb.Append($"To: {to}\r\n");
        if (!string.IsNullOrWhiteSpace(cc)) sb.Append($"Cc: {cc}\r\n");
        if (!string.IsNullOrWhiteSpace(bcc)) sb.Append($"Bcc: {bcc}\r\n");
        sb.Append($"Subject: {EncodeHeaderValue(subject)}\r\n");

        if (!string.IsNullOrWhiteSpace(inReplyTo)) sb.Append($"In-Reply-To: {inReplyTo}\r\n");
        if (!string.IsNullOrWhiteSpace(references)) sb.Append($"References: {references}\r\n");
        else if (!string.IsNullOrWhiteSpace(inReplyTo)) sb.Append($"References: {inReplyTo}\r\n");

        sb.Append("MIME-Version: 1.0\r\n");

        if (attachments == null || attachments.Count == 0)
        {
            sb.Append("Content-Type: text/plain; charset=utf-8\r\n");
            sb.Append("Content-Transfer-Encoding: 8bit\r\n");
            sb.Append("\r\n");
            sb.Append(body ?? "");
            return Encoding.UTF8.GetBytes(sb.ToString());
        }

        sb.Append($"Content-Type: multipart/mixed; boundary=\"{boundary}\"\r\n\r\n");

        // Body part
        sb.Append($"--{boundary}\r\n");
        sb.Append("Content-Type: text/plain; charset=utf-8\r\n");
        sb.Append("Content-Transfer-Encoding: 8bit\r\n\r\n");
        sb.Append(body ?? "");
        sb.Append("\r\n");

        var byteStream = new MemoryStream();
        byte[] preHeaderBytes = Encoding.UTF8.GetBytes(sb.ToString());
        byteStream.Write(preHeaderBytes, 0, preHeaderBytes.Length);

        foreach (var att in attachments)
        {
            var partHeader = new StringBuilder();
            partHeader.Append($"--{boundary}\r\n");
            partHeader.Append($"Content-Type: {att.MimeType}; name=\"{EscapeHeaderParam(att.Name)}\"\r\n");
            partHeader.Append($"Content-Disposition: attachment; filename=\"{EscapeHeaderParam(att.Name)}\"\r\n");
            partHeader.Append("Content-Transfer-Encoding: base64\r\n\r\n");

            byte[] partHeaderBytes = Encoding.UTF8.GetBytes(partHeader.ToString());
            byteStream.Write(partHeaderBytes, 0, partHeaderBytes.Length);

            string b64Content = Convert.ToBase64String(att.Content, Base64FormattingOptions.InsertLineBreaks);
            byte[] b64Bytes = Encoding.ASCII.GetBytes(b64Content + "\r\n");
            byteStream.Write(b64Bytes, 0, b64Bytes.Length);
        }

        byte[] endBoundaryBytes = Encoding.UTF8.GetBytes($"--{boundary}--\r\n");
        byteStream.Write(endBoundaryBytes, 0, endBoundaryBytes.Length);

        return byteStream.ToArray();
    }

    private static string EncodeHeaderValue(string text)
    {
        if (string.IsNullOrEmpty(text)) return "";
        bool needsEncoding = text.Any(c => c >= 0x80 || c < 0x20);
        if (!needsEncoding) return text;
        string b64 = Convert.ToBase64String(Encoding.UTF8.GetBytes(text));
        return $"=?utf-8?B?{b64}?=";
    }

    private static string EscapeHeaderParam(string value)
    {
        return value.Replace("\"", "\\\"");
    }

    public static string ExtractTextFromBody(JsonNode? payload)
    {
        if (payload == null) return "";
        var sb = new StringBuilder();
        ExtractPartsRecursive(payload, sb);
        return CleanInvisible(sb.ToString().Trim());
    }

    private static void ExtractPartsRecursive(JsonNode node, StringBuilder sb)
    {
        if (node == null) return;
        string mimeType = node["mimeType"]?.GetValue<string>() ?? "";
        var bodyNode = node["body"];
        string rawData = bodyNode?["data"]?.GetValue<string>() ?? "";

        if (mimeType.Equals("text/plain", StringComparison.OrdinalIgnoreCase) && !string.IsNullOrEmpty(rawData))
        {
            try
            {
                byte[] decoded = Base64UrlDecode(rawData);
                string text = Encoding.UTF8.GetString(decoded);
                sb.AppendLine(text);
            }
            catch { }
        }
        else if (mimeType.Equals("text/html", StringComparison.OrdinalIgnoreCase) && !string.IsNullOrEmpty(rawData) && sb.Length == 0)
        {
            try
            {
                byte[] decoded = Base64UrlDecode(rawData);
                string html = Encoding.UTF8.GetString(decoded);
                sb.AppendLine(HtmlToPlainText(html));
            }
            catch { }
        }

        if (node["parts"] is JsonArray parts)
        {
            foreach (var p in parts)
            {
                if (p != null) ExtractPartsRecursive(p, sb);
            }
        }
    }

    public static string HtmlToPlainText(string html)
    {
        if (string.IsNullOrEmpty(html)) return "";
        string clean = Regex.Replace(html, @"<(script|style|head|noscript)[^>]*>.*?</\1>", "", RegexOptions.IgnoreCase | RegexOptions.Singleline);
        clean = Regex.Replace(clean, @"<!--.*?-->", "", RegexOptions.Singleline);
        clean = Regex.Replace(clean, @"<br\s*/?>", "\n", RegexOptions.IgnoreCase);
        clean = Regex.Replace(clean, @"</?(p|div|tr|li|h[1-6]|blockquote)[^>]*>", "\n", RegexOptions.IgnoreCase);
        clean = Regex.Replace(clean, @"<[^>]+>", " ");
        clean = System.Net.WebUtility.HtmlDecode(clean);
        clean = Regex.Replace(clean, @"[ \t]+", " ");
        clean = Regex.Replace(clean, @"\n\s*\n+", "\n\n");
        return clean.Trim();
    }
}
