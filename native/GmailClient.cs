using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;

namespace NexusAGYmail;

public sealed class GmailApiException : Exception
{
    public HttpStatusCode StatusCode { get; }
    public string Details { get; }

    public GmailApiException(HttpStatusCode code, string message, string details = "")
        : base($"{message} (HTTP {(int)code}): {details}")
    {
        StatusCode = code;
        Details = details;
    }
}

public sealed class GmailClient
{
    private static readonly HttpClient HttpClient = new(new SocketsHttpHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromSeconds(30) };
    private readonly string _apiBase;

    public GmailClient(string? customApiBase = null)
    {
        string? envBase = Environment.GetEnvironmentVariable("NEXUS_AGYMAIL_API_BASE");
        _apiBase = (customApiBase ?? envBase ?? "https://gmail.googleapis.com/gmail/v1/users/me").TrimEnd('/');
    }

    private HttpRequestMessage CreateRequest(HttpMethod method, string path, string? query = null)
    {
        string url = $"{_apiBase}/{path.TrimStart('/')}";
        if (!string.IsNullOrEmpty(query))
        {
            url += (url.Contains('?') ? "&" : "?") + query;
        }

        var creds = GmailAuth.GetCredentials();
        var req = new HttpRequestMessage(method, url);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", creds.AccessToken);
        req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        return req;
    }

    private JsonNode SendRequest(HttpRequestMessage req)
    {
        using var resp = HttpClient.Send(req);
        string text = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();

        if (!resp.IsSuccessStatusCode)
        {
            throw new GmailApiException(resp.StatusCode, resp.ReasonPhrase ?? "API error");
        }

        if (string.IsNullOrWhiteSpace(text))
        {
            return new JsonObject();
        }

        return JsonNode.Parse(text) ?? new JsonObject();
    }

    public JsonObject Execute(HttpMethod method, string path, JsonObject? payload = null, string? query = null)
    {
        using var request = CreateRequest(method, path, query);
        if (payload is not null) request.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json");
        return SendRequest(request) as JsonObject ?? new();
    }

    // Larger MIME bodies use Gmail's official resumable media endpoint. Never replay a send after ambiguous I/O.
    public JsonObject Upload(string path, HttpMethod method, JsonObject metadata, byte[] raw)
    {
        string uploadBase = _apiBase.Replace("/gmail/v1/", "/upload/gmail/v1/", StringComparison.Ordinal);
        using var start = CreateRequest(method, path, "uploadType=resumable");
        start.RequestUri = new Uri(uploadBase + "/" + path);
        start.RequestUri = new Uri(start.RequestUri + "?uploadType=resumable");
        start.Headers.Add("X-Upload-Content-Type", "message/rfc822");
        start.Headers.Add("X-Upload-Content-Length", raw.Length.ToString(System.Globalization.CultureInfo.InvariantCulture));
        start.Content = new StringContent(metadata.ToJsonString(), Encoding.UTF8, "application/json");
        using var initiated = HttpClient.Send(start);
        if (!initiated.IsSuccessStatusCode) throw new GmailApiException(initiated.StatusCode, "Media upload refused");
        var location = initiated.Headers.Location ?? throw new IOException("Upload session has no Location");
        if (!location.IsAbsoluteUri) location = new Uri(start.RequestUri, location);
        if (location.Scheme != start.RequestUri.Scheme || location.Host != start.RequestUri.Host || location.Port != start.RequestUri.Port)
            throw new IOException("Upload session changed origin; credentials not sent.");
        using var upload = CreateRequest(HttpMethod.Put, path);
        upload.RequestUri = location;
        upload.Content = new ByteArrayContent(raw);
        upload.Content.Headers.ContentType = new MediaTypeHeaderValue("message/rfc822");
        return SendRequest(upload) as JsonObject ?? new();
    }

    // --- Search & Messages

    public JsonObject SearchThreads(string query = "", int pageSize = 20, string pageToken = "", bool includeTrash = false)
    {
        var qParams = new List<string>
        {
            $"maxResults={Math.Clamp(pageSize, 1, 100)}"
        };
        if (!string.IsNullOrEmpty(query)) qParams.Add($"q={Uri.EscapeDataString(query)}");
        if (!string.IsNullOrEmpty(pageToken)) qParams.Add($"pageToken={Uri.EscapeDataString(pageToken)}");
        if (includeTrash) qParams.Add("includeSpamTrash=true");

        using var req = CreateRequest(HttpMethod.Get, "threads", string.Join("&", qParams));
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject GetMessage(string messageId, string format = "full")
    {
        using var req = CreateRequest(HttpMethod.Get, $"messages/{Uri.EscapeDataString(messageId)}", $"format={format}");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject GetThread(string threadId, string format = "full")
    {
        using var req = CreateRequest(HttpMethod.Get, $"threads/{Uri.EscapeDataString(threadId)}", $"format={format}");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject ListHistory(ulong startHistoryId, string? labelId = null, string? pageToken = null, int maxResults = 100)
    {
        var qParams = new List<string>
        {
            $"startHistoryId={startHistoryId}",
            $"maxResults={Math.Clamp(maxResults, 1, 500)}"
        };
        if (!string.IsNullOrEmpty(labelId)) qParams.Add($"labelId={Uri.EscapeDataString(labelId)}");
        if (!string.IsNullOrEmpty(pageToken)) qParams.Add($"pageToken={Uri.EscapeDataString(pageToken)}");

        using var req = CreateRequest(HttpMethod.Get, "history", string.Join("&", qParams));
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public byte[] DownloadAttachment(string messageId, string attachmentId)
    {
        using var req = CreateRequest(HttpMethod.Get, $"messages/{Uri.EscapeDataString(messageId)}/attachments/{Uri.EscapeDataString(attachmentId)}");
        var node = SendRequest(req);
        string? dataStr = node["data"]?.GetValue<string>();
        if (string.IsNullOrEmpty(dataStr)) return Array.Empty<byte>();
        return MimeHelper.Base64UrlDecode(dataStr);
    }

    // --- Drafts

    public JsonObject ListDrafts(int pageSize = 20, string pageToken = "", string query = "")
    {
        var qParams = new List<string>
        {
            $"maxResults={Math.Clamp(pageSize, 1, 100)}"
        };
        if (!string.IsNullOrEmpty(pageToken)) qParams.Add($"pageToken={Uri.EscapeDataString(pageToken)}");
        if (!string.IsNullOrEmpty(query)) qParams.Add($"q={Uri.EscapeDataString(query)}");

        using var req = CreateRequest(HttpMethod.Get, "drafts", string.Join("&", qParams));
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject GetDraft(string draftId, string format = "full")
    {
        using var req = CreateRequest(HttpMethod.Get, $"drafts/{Uri.EscapeDataString(draftId)}", $"format={format}");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject CreateDraft(string rawBase64Url, string? threadId = null)
    {
        var msgNode = new JsonObject { ["raw"] = rawBase64Url };
        if (!string.IsNullOrEmpty(threadId)) msgNode["threadId"] = threadId;

        var payload = new JsonObject { ["message"] = msgNode };
        using var req = CreateRequest(HttpMethod.Post, "drafts");
        req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject UpdateDraft(string draftId, string rawBase64Url, string? threadId = null)
    {
        var msgNode = new JsonObject { ["raw"] = rawBase64Url };
        if (!string.IsNullOrEmpty(threadId)) msgNode["threadId"] = threadId;

        var payload = new JsonObject { ["id"] = draftId, ["message"] = msgNode };
        using var req = CreateRequest(HttpMethod.Put, $"drafts/{Uri.EscapeDataString(draftId)}");
        req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject SendDraftDirect(string draftId)
    {
        var payload = new JsonObject { ["id"] = draftId };
        using var req = CreateRequest(HttpMethod.Post, "drafts/send");
        req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    // --- Labels

    public JsonObject ListLabels()
    {
        using var req = CreateRequest(HttpMethod.Get, "labels");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject CreateLabel(string name, bool autoCreateParentLabels = true)
    {
        var payload = new JsonObject
        {
            ["name"] = name,
            ["labelListVisibility"] = "labelShow",
            ["messageListVisibility"] = "show",
        };
        using var req = CreateRequest(HttpMethod.Post, "labels");
        req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject ModifyMessage(string messageId, IEnumerable<string>? addLabels = null, IEnumerable<string>? removeLabels = null)
    {
        var payload = new JsonObject();
        if (addLabels != null)
        {
            var arr = new JsonArray();
            foreach (var l in addLabels) arr.Add(l);
            payload["addLabelIds"] = arr;
        }
        if (removeLabels != null)
        {
            var arr = new JsonArray();
            foreach (var l in removeLabels) arr.Add(l);
            payload["removeLabelIds"] = arr;
        }

        using var req = CreateRequest(HttpMethod.Post, $"messages/{Uri.EscapeDataString(messageId)}/modify");
        req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject ModifyThread(string threadId, IEnumerable<string>? addLabels = null, IEnumerable<string>? removeLabels = null)
    {
        var payload = new JsonObject();
        if (addLabels != null)
        {
            var arr = new JsonArray();
            foreach (var l in addLabels) arr.Add(l);
            payload["addLabelIds"] = arr;
        }
        if (removeLabels != null)
        {
            var arr = new JsonArray();
            foreach (var l in removeLabels) arr.Add(l);
            payload["removeLabelIds"] = arr;
        }

        using var req = CreateRequest(HttpMethod.Post, $"threads/{Uri.EscapeDataString(threadId)}/modify");
        req.Content = new StringContent(payload.ToJsonString(), Encoding.UTF8, "application/json");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject TrashMessage(string messageId)
    {
        using var req = CreateRequest(HttpMethod.Post, $"messages/{Uri.EscapeDataString(messageId)}/trash");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject UntrashMessage(string messageId)
    {
        using var req = CreateRequest(HttpMethod.Post, $"messages/{Uri.EscapeDataString(messageId)}/untrash");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject TrashThread(string threadId)
    {
        using var req = CreateRequest(HttpMethod.Post, $"threads/{Uri.EscapeDataString(threadId)}/trash");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }

    public JsonObject UntrashThread(string threadId)
    {
        using var req = CreateRequest(HttpMethod.Post, $"threads/{Uri.EscapeDataString(threadId)}/untrash");
        return SendRequest(req) as JsonObject ?? new JsonObject();
    }
}
