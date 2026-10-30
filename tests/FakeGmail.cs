using System.Net;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.WebUtilities;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Primitives;
using MimeKit;

namespace NexusAGYmail.Tests;

/// <summary>
/// Fake Gmail API over HTTP on loopback only (no network): messages (list filtered by labelIds and the q terms
/// after:/before:/rfc822msgid:/in:sent, get, attachments, modify, trash, untrash, send, batchModify), threads, labels,
/// drafts, profile, history, resumable uploads; paging, metadata/full/raw formats, programmable faults (429, 5xx),
/// lost or truncated send replies, a redirecting send, and one-shot hooks run right after a request of a given kind
/// (a concurrent change). Every request is recorded, so exact bytes, duplicates and unknown outcomes are observable.
/// </summary>
public sealed class FakeGmail : IAsyncDisposable
{
    public static readonly string[] NewScopes =
    [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
    ];

    private readonly object gate = new();
    private readonly WebApplication app;
    public string BaseUrl { get; }
    public string ApiBase => BaseUrl + "/gmail/v1/users/me";

    public Dictionary<string, JsonObject> Messages { get; } = [];
    public Dictionary<string, List<string>> Threads { get; } = [];
    public List<JsonObject> Drafts { get; } = [];
    public List<(string Kind, string Uri)> Calls { get; } = [];
    public Dictionary<string, Queue<int>> Faults { get; } = [];
    public List<JsonObject> Labels { get; } = [];
    public Dictionary<string, JsonObject> DraftStore { get; } = [];
    public List<JsonObject> Sent { get; } = [];
    public Dictionary<string, byte[]> Blobs { get; } = [];
    private readonly Dictionary<string, (string Method, string Route, JsonObject Metadata)> sessions = [];
    public long HistoryId { get; set; } = 1000;
    public long HistoryFloor { get; set; }
    public List<JsonObject> History { get; } = [];
    public Dictionary<string, Action> After { get; } = [];
    public int LostSends { get; set; }
    public int GarbledSends { get; set; }
    public byte[]? SendResponseOverride { get; set; }
    public bool RedirectSend { get; set; }

    public FakeGmail()
    {
        foreach (string id in new[] { "INBOX", "UNREAD", "STARRED", "IMPORTANT", "TRASH", "SPAM", "SENT", "DRAFT", "CHAT" })
            Labels.Add(new JsonObject { ["id"] = id, ["name"] = id, ["type"] = "system" });
        Labels.Add(new JsonObject { ["id"] = "Label_1", ["name"] = "Factures", ["type"] = "user" });
        Labels.Add(new JsonObject { ["id"] = "Label_2", ["name"] = "Clients/Acme", ["type"] = "user" });

        var builder = WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseKestrel(o =>
        {
            o.Listen(IPAddress.Loopback, 0);
            o.Limits.MaxRequestBodySize = 64 * 1024 * 1024;
        });
        app = builder.Build();
        app.Run(Handle);
        app.StartAsync().GetAwaiter().GetResult();
        string address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.First();
        BaseUrl = address.Replace("[::1]", "127.0.0.1").TrimEnd('/');
    }

    public async ValueTask DisposeAsync() => await app.DisposeAsync();

    public List<string> Kinds() { lock (gate) return Calls.Select(c => c.Kind).ToList(); }

    // --- builders (what Gmail stores)

    public static string B64(byte[] data) => Convert.ToBase64String(data).Replace('+', '-').Replace('/', '_');

    public static byte[] FromB64(string data)
    {
        string s = data.Replace('-', '+').Replace('_', '/');
        return Convert.FromBase64String(s.PadRight(s.Length + (4 - s.Length % 4) % 4, '='));
    }

    public static byte[] RawOf(JsonObject message) =>
        message["_raw"] is JsonValue v ? FromB64(v.GetValue<string>()) : [];

    public static byte[] Bytes(MimeMessage message)
    {
        using var stream = new MemoryStream();
        message.WriteTo(FormatOptions.Default, stream);
        return stream.ToArray();
    }

    private static JsonArray Headers(IEnumerable<Header> headers) =>
        new(headers.Select(h => (JsonNode)new JsonObject { ["name"] = h.Field, ["value"] = h.Value }).ToArray());

    private static JsonObject Payload(MimeEntity entity, IEnumerable<Header> headers)
    {
        string mime = entity.ContentType.MimeType.ToLowerInvariant();
        if (entity is Multipart multipart)
        {
            return new JsonObject
            {
                ["partId"] = "", ["mimeType"] = mime, ["filename"] = "", ["headers"] = Headers(headers),
                ["body"] = new JsonObject { ["size"] = 0 },
                ["parts"] = new JsonArray(multipart.Select(child => (JsonNode)Payload(child, child.Headers)).ToArray()),
            };
        }
        byte[] data = [];
        string filename = "";
        if (entity is MimePart part)
        {
            using var stream = new MemoryStream();
            part.Content?.DecodeTo(stream);
            data = stream.ToArray();
            filename = part.FileName ?? "";
        }
        else if (entity is MessagePart inner)
        {
            using var stream = new MemoryStream();
            inner.Message?.WriteTo(stream);
            data = stream.ToArray();
        }
        return new JsonObject
        {
            ["partId"] = "", ["mimeType"] = mime, ["filename"] = filename, ["headers"] = Headers(headers),
            ["body"] = new JsonObject { ["size"] = data.Length, ["data"] = B64(data) },
        };
    }

    /// <summary>Message as Gmail stores it, from the raw bytes a client sent.</summary>
    public static JsonObject DraftMessage(string messageId, byte[] raw, string? threadId)
    {
        var parsed = MimeMessage.Load(new MemoryStream(raw));
        string plain = parsed.TextBody ?? "";
        return new JsonObject
        {
            ["id"] = messageId,
            ["threadId"] = string.IsNullOrEmpty(threadId) ? $"t-{messageId}" : threadId,
            ["labelIds"] = new JsonArray("DRAFT"),
            ["snippet"] = plain.Length > 100 ? plain[..100] : plain,
            ["payload"] = Payload(parsed.Body!, parsed.Headers.Concat(parsed.Body!.Headers)),
            ["_raw"] = B64(raw),
        };
    }

    /// <summary>A draft already in Gmail (written by the user), with an optional PDF attachment.</summary>
    public void StoredDraft(string draftId, string text, string attachment = "")
    {
        var message = new MimeMessage();
        message.To.Add(MailboxAddress.Parse("Bob <bob@example.com>"));
        message.Subject = "Devis signé";
        var body = new TextPart("plain") { Text = text + "\n" };
        if (attachment.Length > 0)
        {
            var pdf = new MimePart("application", "pdf")
            {
                Content = new MimeContent(new MemoryStream(Encoding.ASCII.GetBytes("%PDF-1.7 faux"))),
                ContentDisposition = new ContentDisposition(ContentDisposition.Attachment),
                ContentTransferEncoding = ContentEncoding.Base64,
                FileName = attachment,
            };
            message.Body = new Multipart("mixed") { body, pdf };
        }
        else
            message.Body = body;
        lock (gate)
            AddDraft(draftId, DraftMessage($"m-{draftId}", Bytes(message), $"t-{draftId}"));
    }

    /// <summary>A message arriving in the inbox (from Alice), recorded in the history.</summary>
    public JsonObject Received(string messageId, string text = "Bonjour", string subject = "Objet")
    {
        var mime = new MimeMessage();
        mime.From.Add(MailboxAddress.Parse("Alice <alice@example.com>"));
        mime.To.Add(MailboxAddress.Parse("test@example.com"));
        mime.Subject = subject;
        mime.MessageId = $"{messageId}@example.com";
        mime.Body = new TextPart("plain") { Text = text + "\n" };
        var message = DraftMessage(messageId, Bytes(mime), "t" + messageId);
        message["labelIds"] = new JsonArray("INBOX", "UNREAD");
        lock (gate)
            Receive(message);
        return message;
    }

    public void Add(JsonObject message)
    {
        string id = message["id"]!.GetValue<string>();
        Messages[id] = message;
        string thread = message["threadId"]!.GetValue<string>();
        if (!Threads.TryGetValue(thread, out var list))
            Threads[thread] = list = [];
        list.Add(id);
    }

    private string Receive(JsonObject message)
    {
        message["internalDate"] ??= DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();
        Add(message);
        HistoryId++;
        var added = new JsonObject { ["id"] = message["id"]!.DeepClone(), ["threadId"] = message["threadId"]!.DeepClone(), ["labelIds"] = message["labelIds"]!.DeepClone() };
        History.Add(new JsonObject
        {
            ["id"] = HistoryId.ToString(),
            ["messages"] = new JsonArray(added.DeepClone()),
            ["messagesAdded"] = new JsonArray(new JsonObject { ["message"] = added }),
        });
        return HistoryId.ToString();
    }

    public JsonObject AddDraft(string draftId, JsonObject message)
    {
        DraftStore[draftId] = new JsonObject { ["id"] = draftId, ["message"] = message };
        return message;
    }

    public JsonObject DraftOf(string draftId) { lock (gate) return DraftStore[draftId]["message"]!.AsObject(); }

    public void Fault(string kind, params int[] statuses)
    {
        lock (gate)
            Faults[kind] = new Queue<int>(statuses);
    }

    // --- HTTP

    private async Task Handle(HttpContext context)
    {
        using var buffer = new MemoryStream();
        await context.Request.Body.CopyToAsync(buffer);
        byte[] body = buffer.ToArray();
        string path = context.Request.Path.Value ?? "";
        string uri = path + context.Request.QueryString.Value;
        int status;
        byte[] content;
        string? location = null;
        lock (gate)
        {
            if (path.StartsWith("/upload/", StringComparison.Ordinal) || path.StartsWith("/fake-session/", StringComparison.Ordinal))
                (status, content, location) = Upload(context.Request.Method, uri, body);
            else if (path.EndsWith("/messages/send", StringComparison.Ordinal) && RedirectSend)
            {
                Calls.Add(("redirect-attempt", uri));
                (status, content, location) = (307, [], BaseUrl + "/unexpected");
            }
            else
            {
                var (code, data) = Route(context.Request.Method, uri, body.Length > 0 ? Encoding.UTF8.GetString(body) : null);
                status = code;
                content = data["_garbled"] is not null ? Encoding.UTF8.GetBytes("{\"id\": \"s1\", \"thre") : Encoding.UTF8.GetBytes(data.ToJsonString());
                if (path.EndsWith("/messages/send", StringComparison.Ordinal) && SendResponseOverride is not null)
                    content = SendResponseOverride;
            }
        }
        context.Response.StatusCode = status;
        if (location is not null)
            context.Response.Headers.Location = location;
        if (status != 204 && content.Length > 0)
        {
            context.Response.ContentType = "application/json; charset=UTF-8";
            await context.Response.Body.WriteAsync(content);
        }
    }

    private (int, byte[], string?) Upload(string method, string uri, byte[] body)
    {
        string path = uri.Split('?')[0];
        if (path.StartsWith("/upload/", StringComparison.Ordinal))
        {
            string session = $"s{sessions.Count + 1}";
            var metadata = body.Length > 0 ? JsonNode.Parse(body)!.AsObject() : new JsonObject();
            sessions[session] = (method, path["/upload".Length..], metadata);
            Calls.Add(("upload-session", uri));
            return (200, [], $"{BaseUrl}/fake-session/{session}");
        }
        string id = path[(path.LastIndexOf('/') + 1)..];
        var (method0, route, meta) = sessions[id];
        sessions.Remove(id);
        JsonObject payload;
        if (route.EndsWith("/messages/send", StringComparison.Ordinal))
        {
            payload = meta.DeepClone().AsObject();
            payload["raw"] = B64(body);
        }
        else
        {
            payload = meta.DeepClone().AsObject();
            var message = (payload["message"] as JsonObject)?.DeepClone().AsObject() ?? new JsonObject();
            message["raw"] = B64(body);
            payload["message"] = message;
        }
        var (status, data) = Route(method0, route, payload.ToJsonString());
        return (status, Encoding.UTF8.GetBytes(data.ToJsonString()), null);
    }

    private static string KindOf(string method, string[] segments)
    {
        string resource = segments[0];
        int count = segments.Length;
        string last = segments[^1];
        if (method == "DELETE" && resource == "drafts" && count == 2) return "draft-delete";
        if (method == "DELETE" || segments.Contains("batchDelete")) return "suppression";
        string? fixedKind = (method, resource, count) switch
        {
            ("GET", "messages", 1) => "list",
            ("GET", "messages", 2) => "get",
            ("GET", "threads", 1) => "thread-list",
            ("GET", "threads", 2) => "thread",
            ("GET", "labels", 1) => "labels",
            ("GET", "profile", 1) => "profile",
            ("GET", "history", 1) => "history",
            ("POST", "labels", 1) => "label-create",
            ("POST", "drafts", 1) => "draft",
            ("GET", "drafts", 1) => "drafts",
            ("GET", "drafts", 2) => "draft-get",
            ("PUT", "drafts", 2) => "draft-update",
            _ => null,
        };
        if (fixedKind is not null) return fixedKind;
        if (method == "GET" && resource == "messages" && count == 4 && segments[2] == "attachments") return "attachment";
        if (method == "POST" && resource == "drafts" && last == "send") return "draft-send";
        if (method == "POST" && resource == "messages" && last == "batchModify") return "batchModify";
        if (method == "POST" && resource == "messages" && last == "send") return "message-send";
        if (method == "POST" && count == 3 && last is "modify" or "trash" or "untrash") return $"{resource[..^1]}-{last}";
        return "autre";
    }

    private static (int, JsonObject) Missing() =>
        (404, new JsonObject { ["error"] = new JsonObject { ["code"] = 404, ["message"] = "Requested entity was not found." } });

    private static string Q(Dictionary<string, StringValues> query, string key, string fallback = "") =>
        query.TryGetValue(key, out var v) && v.Count > 0 ? v[0]! : fallback;

    private static JsonObject Page(IReadOnlyList<string> ids, Dictionary<string, StringValues> query, string name, Func<string, JsonNode> item)
    {
        int start = int.Parse(Q(query, "pageToken", "0"));
        int size = int.Parse(Q(query, "maxResults", "100"));
        var data = new JsonObject
        {
            [name] = new JsonArray(ids.Skip(start).Take(size).Select(item).ToArray()),
            ["resultSizeEstimate"] = ids.Count,
        };
        if (start + size < ids.Count)
            data["nextPageToken"] = (start + size).ToString();
        return data;
    }

    private (int, JsonObject) Route(string method, string uri, string? body)
    {
        string[] parts = uri.Split('?', 2);
        var query = QueryHelpers.ParseQuery(parts.Length > 1 ? parts[1] : "");
        const string prefix = "/gmail/v1/users/me/";
        string[] segments = parts[0].StartsWith(prefix, StringComparison.Ordinal) ? parts[0][prefix.Length..].Split('/') : ["?"];
        string kind = KindOf(method, segments);
        Calls.Add((kind, uri));
        if (Faults.TryGetValue(kind, out var queue) && queue.Count > 0)
        {
            int fault = queue.Dequeue();
            return (fault, new JsonObject
            {
                ["error"] = new JsonObject
                {
                    ["code"] = fault, ["message"] = "panne simulée",
                    ["errors"] = new JsonArray(new JsonObject { ["reason"] = "rateLimitExceeded" }),
                },
            });
        }
        var data = body is null ? new JsonObject() : JsonNode.Parse(body)!.AsObject();
        switch (kind)
        {
            case "list":
            {
                var ids = Messages.Where(m => Matches(m.Value, query)).Select(m => m.Key).Order(StringComparer.Ordinal).ToList();
                return (200, Page(ids, query, "messages", i => new JsonObject { ["id"] = i, ["threadId"] = Messages[i]["threadId"]!.DeepClone() }));
            }
            case "get":
            {
                var message = Messages.GetValueOrDefault(segments[1])
                    ?? DraftStore.Values.Select(d => d["message"]!.AsObject()).FirstOrDefault(m => m["id"]!.GetValue<string>() == segments[1]);
                return message is null ? Missing() : (200, Format(message, query));
            }
            case "thread":
                return Threads.TryGetValue(segments[1], out var members) && members.Count > 0
                    ? (200, new JsonObject { ["id"] = segments[1], ["messages"] = new JsonArray(members.Select(i => (JsonNode)Format(Messages[i], query)).ToArray()) })
                    : Missing();
            case "draft":
            {
                Drafts.Add(data);
                string draftId = $"d{Drafts.Count}";
                var message = data["message"]!.AsObject();
                var stored = AddDraft(draftId, DraftMessage($"dm{Drafts.Count}", FromB64(message["raw"]!.GetValue<string>()), message["threadId"]?.GetValue<string>()));
                return (200, new JsonObject { ["id"] = draftId, ["message"] = new JsonObject { ["id"] = stored["id"]!.DeepClone(), ["threadId"] = stored["threadId"]!.DeepClone() } });
            }
            case "thread-list":
                return (200, Page(Threads.Keys.Order(StringComparer.Ordinal).ToList(), query, "threads", i => new JsonObject { ["id"] = i }));
            case "profile":
                return (200, new JsonObject { ["emailAddress"] = "test@example.com", ["historyId"] = HistoryId.ToString(), ["messagesTotal"] = 0 });
            case "history":
            {
                long start = long.Parse(Q(query, "startHistoryId"));
                if (start < HistoryFloor)
                    return Missing();
                string label = Q(query, "labelId");
                var records = History.Where(r => long.Parse(r["id"]!.GetValue<string>()) > start &&
                    (label.Length == 0 || r["messagesAdded"]![0]!["message"]!["labelIds"]!.AsArray().Any(l => l!.GetValue<string>() == label))).ToList();
                int first = int.Parse(Q(query, "pageToken", "0"));
                int size = int.Parse(Q(query, "maxResults", "100"));
                var history = new JsonObject { ["historyId"] = HistoryId.ToString() };
                var page = records.Skip(first).Take(size).Select(r => (JsonNode)r.DeepClone()).ToArray();
                if (page.Length > 0) history["history"] = new JsonArray(page);
                if (first + size < records.Count) history["nextPageToken"] = (first + size).ToString();
                return (200, history);
            }
            case "attachment":
                return Blobs.TryGetValue(segments[3], out byte[]? blob)
                    ? (200, new JsonObject { ["attachmentId"] = segments[3], ["size"] = blob.Length, ["data"] = B64(blob) })
                    : Missing();
            case "labels":
                return (200, new JsonObject { ["labels"] = new JsonArray(Labels.Select(l => (JsonNode)l.DeepClone()).ToArray()) });
            case "label-create":
            {
                string name = data["name"]!.GetValue<string>();
                if (Labels.Any(l => string.Equals(l["name"]!.GetValue<string>(), name, StringComparison.OrdinalIgnoreCase)))
                    return (409, new JsonObject { ["error"] = new JsonObject { ["code"] = 409, ["message"] = "Label name exists or conflicts" } });
                var label = new JsonObject { ["id"] = $"Label_{Labels.Count + 1}", ["name"] = name, ["type"] = "user" };
                Labels.Add(label);
                return (200, label.DeepClone().AsObject());
            }
            case "batchModify":
            {
                var known = Labels.Select(l => l["id"]!.GetValue<string>()).ToHashSet();
                if (Strings(data["addLabelIds"]).Concat(Strings(data["removeLabelIds"])).Any(i => !known.Contains(i)))
                    return (400, new JsonObject { ["error"] = new JsonObject { ["code"] = 400, ["message"] = "Invalid label" } });
                foreach (string id in Strings(data["ids"]))
                    if (Messages.TryGetValue(id, out var m)) Relabel(m, data);
                return (204, new JsonObject());
            }
            case "message-modify" or "message-trash" or "message-untrash":
            {
                if (!Messages.TryGetValue(segments[1], out var message))
                    return Missing();
                Relabel(message, LabelChange(kind, data));
                return (200, new JsonObject { ["id"] = message["id"]!.DeepClone(), ["threadId"] = message["threadId"]!.DeepClone(), ["labelIds"] = message["labelIds"]!.DeepClone() });
            }
            case "thread-modify" or "thread-trash" or "thread-untrash":
            {
                if (!Threads.TryGetValue(segments[1], out var ids) || ids.Count == 0)
                    return Missing();
                foreach (string id in ids)
                    Relabel(Messages[id], LabelChange(kind, data));
                return (200, new JsonObject { ["id"] = segments[1], ["messages"] = new JsonArray(ids.Select(i => (JsonNode)new JsonObject { ["id"] = i }).ToArray()) });
            }
            case "drafts":
                return (200, Page(DraftStore.Keys.Order(StringComparer.Ordinal).ToList(), query, "drafts",
                    i => new JsonObject { ["id"] = i, ["message"] = new JsonObject { ["id"] = DraftStore[i]["message"]!["id"]!.DeepClone() } }));
            case "draft-get":
            {
                if (!DraftStore.TryGetValue(segments[1], out var draft))
                    return Missing();
                var answer = new JsonObject { ["id"] = draft["id"]!.DeepClone(), ["message"] = Format(draft["message"]!.AsObject(), query) };
                if (After.Remove(kind, out var hook)) hook();  // concurrent change AFTER the returned read
                return (200, answer);
            }
            case "draft-delete":
                return DraftStore.Remove(segments[1]) ? (204, new JsonObject()) : Missing();
            case "message-send":
            {
                byte[] raw = FromB64(data["raw"]!.GetValue<string>());
                string? thread = data["threadId"]?.GetValue<string>();
                var sent = DraftMessage($"s{Sent.Count + 1}", raw, thread);
                sent["threadId"] = string.IsNullOrEmpty(thread) ? sent["id"]!.DeepClone() : thread;
                sent["labelIds"] = new JsonArray("SENT");
                sent["internalDate"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();
                Sent.Add(sent);
                Add(sent);
                if (LostSends > 0)  // sent, but the reply is lost: unknown outcome for the client
                {
                    LostSends--;
                    return (503, new JsonObject { ["error"] = new JsonObject { ["code"] = 503, ["message"] = "réponse perdue" } });
                }
                if (GarbledSends > 0)  // sent, truncated 200 reply
                {
                    GarbledSends--;
                    return (200, new JsonObject { ["_garbled"] = true });
                }
                return (200, new JsonObject { ["id"] = sent["id"]!.DeepClone(), ["threadId"] = sent["threadId"]!.DeepClone(), ["labelIds"] = sent["labelIds"]!.DeepClone() });
            }
            case "draft-update":
            {
                if (!DraftStore.TryGetValue(segments[1], out var draft))
                    return Missing();
                var message = data["message"]!.AsObject();
                var updated = DraftMessage(draft["message"]!["id"]!.GetValue<string>(), FromB64(message["raw"]!.GetValue<string>()), message["threadId"]?.GetValue<string>());
                draft["message"] = updated;
                return (200, new JsonObject { ["id"] = draft["id"]!.DeepClone(), ["message"] = new JsonObject { ["id"] = updated["id"]!.DeepClone(), ["threadId"] = updated["threadId"]!.DeepClone() } });
            }
            case "draft-send":
            {
                string id = data["id"]?.GetValue<string>() ?? "";
                if (!DraftStore.Remove(id, out var draft))
                    return Missing();
                var sent = draft["message"]!.DeepClone().AsObject();
                sent["labelIds"] = new JsonArray("SENT");
                Sent.Add(sent);
                Add(sent);
                return (200, new JsonObject { ["id"] = sent["id"]!.DeepClone(), ["threadId"] = sent["threadId"]!.DeepClone(), ["labelIds"] = sent["labelIds"]!.DeepClone() });
            }
        }
        return (404, new JsonObject { ["error"] = new JsonObject { ["code"] = 404, ["message"] = "route inconnue" } });
    }

    private static IEnumerable<string> Strings(JsonNode? node) =>
        node is JsonArray array ? array.Select(x => x!.GetValue<string>()) : [];

    private static JsonObject LabelChange(string kind, JsonObject data) =>
        kind.EndsWith("-trash", StringComparison.Ordinal) ? new JsonObject { ["addLabelIds"] = new JsonArray("TRASH") }
        : kind.EndsWith("-untrash", StringComparison.Ordinal) ? new JsonObject { ["removeLabelIds"] = new JsonArray("TRASH") }
        : data;

    private static void Relabel(JsonObject message, JsonObject change)
    {
        var removed = Strings(change["removeLabelIds"]).ToHashSet();
        var labels = Strings(message["labelIds"]).Where(l => !removed.Contains(l)).ToList();
        labels.AddRange(Strings(change["addLabelIds"]).Where(l => !labels.Contains(l)).ToList());
        message["labelIds"] = new JsonArray(labels.Select(l => (JsonNode)JsonValue.Create(l)!).ToArray());
    }

    public static List<string> LabelsOf(JsonObject message) => Strings(message["labelIds"]).ToList();

    private static readonly Regex Bound = new(@"^(after|before):(\d+)$");

    /// <summary>messages.list filter: every labelIds, and the q terms after:/before: (epoch seconds against
    /// internalDate), rfc822msgid: (Message-ID header) and in:sent. Another term fails the test instead of being ignored.</summary>
    private static bool Matches(JsonObject message, Dictionary<string, StringValues> query)
    {
        var labels = LabelsOf(message);
        if (query.TryGetValue("labelIds", out var wanted) && wanted.Any(l => !labels.Contains(l!)))
            return false;
        long received = long.Parse(message["internalDate"]?.GetValue<string>() ?? "0") / 1000;
        foreach (string term in Q(query, "q").Split(' ', StringSplitOptions.RemoveEmptyEntries))
        {
            if (term.StartsWith("rfc822msgid:", StringComparison.Ordinal))
            {
                string id = message["payload"]!["headers"]!.AsArray()
                    .Where(h => string.Equals(h!["name"]!.GetValue<string>(), "message-id", StringComparison.OrdinalIgnoreCase))
                    .Select(h => h!["value"]!.GetValue<string>()).FirstOrDefault() ?? "";
                if (id.Trim().Trim('<', '>') != term["rfc822msgid:".Length..].Trim('<', '>'))
                    return false;
                continue;
            }
            if (term == "in:sent")
            {
                if (!labels.Contains("SENT")) return false;
                continue;
            }
            var match = Bound.Match(term);
            if (!match.Success)
                throw new InvalidOperationException($"fake Gmail: unsupported query term {term}");
            long bound = long.Parse(match.Groups[2].Value);
            if ((match.Groups[1].Value == "after" && received < bound) || (match.Groups[1].Value == "before" && received >= bound))
                return false;
        }
        return true;
    }

    private static JsonObject Format(JsonObject message, Dictionary<string, StringValues> query)
    {
        string format = Q(query, "format", "full");
        if (format == "raw")
            return new JsonObject { ["id"] = message["id"]!.DeepClone(), ["threadId"] = message["threadId"]!.DeepClone(), ["raw"] = B64(RawOf(message)) };
        var copy = new JsonObject();
        foreach (var (key, value) in message)
            if (key != "_raw" && (format != "metadata" || key != "payload"))
                copy[key] = value?.DeepClone();
        if (format == "metadata")
        {
            var names = query.TryGetValue("metadataHeaders", out var h) ? h.Select(x => x!.ToLowerInvariant()).ToHashSet() : [];
            var headers = message["payload"]!["headers"]!.AsArray().Where(x => names.Count == 0 || names.Contains(x!["name"]!.GetValue<string>().ToLowerInvariant()));
            copy["payload"] = new JsonObject
            {
                ["mimeType"] = message["payload"]!["mimeType"]!.DeepClone(),
                ["headers"] = new JsonArray(headers.Select(x => x!.DeepClone()).ToArray()),
            };
        }
        return copy;
    }
}
