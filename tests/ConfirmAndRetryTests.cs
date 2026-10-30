using System.Text.Json.Nodes;

namespace NexusAGYmail.Tests;

/// <summary>
/// 1) Nothing leaves the mailbox without the user's explicit yes: with a client that offers MCP elicitation, send_draft
/// opens its confirmation dialog (elicitation/create, or InputRequiredResult from 2026-07-28) and sends only on a real
/// true; no other tool ever asks. 2) Automatic retries with bounded backoff, as the Python engine: reads retried on 429,
/// 5xx and rate-limit 403 (5 retries), a send only on 429.
/// </summary>
public class ConfirmAndRetryTests
{
    private static JsonObject Elicitation => new() { ["elicitation"] = new JsonObject() };

    private static JsonObject A(params (string Key, JsonNode? Value)[] pairs)
    {
        var o = new JsonObject();
        foreach (var (k, v) in pairs) o[k] = v;
        return o;
    }

    private static string S(JsonNode? node) => node?.GetValue<string>() ?? "";

    private static JsonObject Tool(JsonObject response) =>
        JsonNode.Parse(response["result"]!["content"]![0]!["text"]!.GetValue<string>())!.AsObject();

    /// <summary>Calls send_draft and returns the dialog request the engine sent instead of an answer.</summary>
    private static (int CallId, JsonObject Dialog) AskToSend(Mailbox box, string draftId)
    {
        int id = box.Server.Send("tools/call", A(("name", "send_draft"), ("arguments", A(("draftId", draftId)))));
        var dialog = box.Server.Next();
        Assert.Equal("elicitation/create", S(dialog["method"]));
        return (id, dialog);
    }

    private static JsonObject Reply(Mailbox box, JsonObject dialog, JsonObject result, int callId)
    {
        box.Server.Write(A(("jsonrpc", "2.0"), ("id", dialog["id"]!.DeepClone()), ("result", result)));
        var answer = box.Server.Next();
        Assert.Equal(callId, answer["id"]!.GetValue<int>());
        return Tool(answer);
    }

    [Fact]
    public async Task The_dialog_shows_the_draft_and_a_real_yes_sends_it_once()
    {
        await using var box = new Mailbox("2025-06-18", Elicitation);
        box.Fake.StoredDraft("d1", "Bonjour Bob, voici le devis.", attachment: "devis.pdf");
        var (callId, dialog) = AskToSend(box, "d1");
        string message = S(dialog["params"]!["message"]);
        Assert.StartsWith("Send this email now? Nothing is sent unless you answer yes.", message);
        Assert.Contains("To: Bob <bob@example.com>", message);
        Assert.Contains("Subject: Devis signé", message);
        Assert.Contains("Attachment: devis.pdf", message);
        Assert.Contains("Bonjour Bob, voici le devis.", message);
        var schema = dialog["params"]!["requestedSchema"]!;
        Assert.Equal("boolean", S(schema["properties"]!["send"]!["type"]));
        Assert.Equal(["send"], schema["required"]!.AsArray().Select(x => S(x)));
        Assert.Null(dialog["params"]!["mode"]);  // before 2025-11-25
        Assert.Empty(box.Fake.Sent);
        var result = Reply(box, dialog, A(("action", "accept"), ("content", A(("send", true)))), callId);
        Assert.True(result["sent"]!.GetValue<bool>(), result.ToJsonString());
        Assert.Single(box.Fake.Sent);
    }

    [Theory]
    [InlineData("""{"action":"decline"}""", "declined")]
    [InlineData("""{"action":"cancel"}""", "cancelled")]
    [InlineData("""{"action":"accept","content":{"send":false}}""", "declined")]
    [InlineData("""{"action":"accept","content":{"send":"yes"}}""", "declined")]
    [InlineData("""{"action":"accept","content":{"send":1}}""", "declined")]
    [InlineData("""{"action":"accept"}""", "declined")]
    public async Task Anything_but_a_real_yes_sends_nothing(string answer, string status)
    {
        await using var box = new Mailbox("2025-11-25", Elicitation);
        box.Fake.StoredDraft("d1", "Message");
        var (callId, dialog) = AskToSend(box, "d1");
        Assert.Equal("form", S(dialog["params"]!["mode"]));
        var result = Reply(box, dialog, JsonNode.Parse(answer)!.AsObject(), callId);
        Assert.Equal(status, S(result["status"]));
        Assert.False(result["sent"]!.GetValue<bool>());
        Assert.Contains("nothing was sent", S(result["message"]));
        Assert.Empty(box.Fake.Sent);
        Assert.DoesNotContain("message-send", box.Fake.Kinds());
    }

    [Fact]
    public async Task A_draft_changed_while_the_dialog_is_open_is_not_sent()
    {
        await using var box = new Mailbox("2025-06-18", Elicitation);
        box.Fake.StoredDraft("d1", "Version shown");
        var (callId, dialog) = AskToSend(box, "d1");
        box.Fake.StoredDraft("d1", "Version changed after the dialog");
        var result = Reply(box, dialog, A(("action", "accept"), ("content", A(("send", true)))), callId);
        Assert.Equal("confirmation_required", S(result["status"]));
        Assert.Empty(box.Fake.Sent);
    }

    [Fact]
    public async Task A_refused_dialog_falls_back_to_the_summary_and_sends_nothing()
    {
        await using var box = new Mailbox("2025-06-18", Elicitation);
        box.Fake.StoredDraft("d1", "Message");
        var (callId, dialog) = AskToSend(box, "d1");
        box.Server.Write(A(("jsonrpc", "2.0"), ("id", dialog["id"]!.DeepClone()), ("error", A(("code", -32601), ("message", "no dialog")))));
        var answer = box.Server.Next();
        Assert.Equal(callId, answer["id"]!.GetValue<int>());
        Assert.Equal("confirmation_required", S(Tool(answer)["status"]));
        Assert.Empty(box.Fake.Sent);
    }

    [Fact]
    public async Task Messages_received_during_the_dialog_are_answered_after_it()
    {
        await using var box = new Mailbox("2025-06-18", Elicitation);
        box.Fake.StoredDraft("d1", "Message");
        var (callId, dialog) = AskToSend(box, "d1");
        int pingId = box.Server.Send("ping");
        var result = Reply(box, dialog, A(("action", "accept"), ("content", A(("send", true)))), callId);
        Assert.True(result["sent"]!.GetValue<bool>());
        Assert.Equal(pingId, box.Server.Next()["id"]!.GetValue<int>());
    }

    [Fact]
    public async Task From_2026_07_28_the_dialog_travels_in_an_input_required_result()
    {
        await using var box = new Mailbox("2026-07-28", Elicitation);
        box.Fake.StoredDraft("d1", "Message");
        var first = box.Server.Rpc("tools/call", A(("name", "send_draft"), ("arguments", A(("draftId", "d1")))))["result"]!;
        Assert.Equal("input_required", S(first["resultType"]));
        var request = first["inputRequests"]![McpServer.ElicitKey]!;
        Assert.Equal("elicitation/create", S(request["method"]));
        Assert.Contains("Subject: Devis signé", S(request["params"]!["message"]));
        string state = S(first["requestState"]);
        Assert.Empty(box.Fake.Sent);

        JsonObject Again(string answer, string requestState) => Tool(box.Server.Rpc("tools/call", A(
            ("name", "send_draft"), ("arguments", A(("draftId", "d1"))),
            ("inputResponses", A((McpServer.ElicitKey, JsonNode.Parse(answer)))), ("requestState", requestState))));

        Assert.Equal("declined", S(Again("""{"action":"decline"}""", state)["status"]));
        Assert.Equal("confirmation_required", S(Again("""{"action":"accept","content":{"send":true}}""", "forged-state")["status"]));
        Assert.Empty(box.Fake.Sent);
        var sent = Again("""{"action":"accept","content":{"send":true}}""", state);
        Assert.True(sent["sent"]!.GetValue<bool>(), sent.ToJsonString());
        Assert.Single(box.Fake.Sent);
        Assert.Equal("confirmation_required", S(Again("""{"action":"accept","content":{"send":true}}""", state)["status"]));  // single use
        Assert.Single(box.Fake.Sent);
    }

    [Fact]
    public async Task No_other_tool_asks_for_confirmation()
    {
        await using var box = new Mailbox("2025-06-18", Elicitation);
        box.Fake.Received("m1");
        // Rpc fails if the engine sends a dialog request instead of the answer.
        Assert.Equal("success", S(box.Call("create_draft", A(("body", "Brouillon"), ("to", "a@example.com")))["status"]));
        Assert.Equal("success", S(box.Call("label_message", A(("messageId", "m1"), ("labelIds", new JsonArray("STARRED"))))["status"]));
        Assert.Equal("success", S(box.Call("trash_message", A(("messageId", "m1")))["status"]));
        Assert.Equal("success", S(box.Call("get_message", A(("messageId", "m1")))["status"]));
        Assert.Empty(box.Fake.Sent);
    }

    [Fact]
    public async Task Without_a_dialog_the_accord_is_still_required()
    {
        await using var box = new Mailbox("2026-07-28");
        box.Fake.StoredDraft("d1", "Message");
        var preview = box.Call("send_draft", A(("draftId", "d1"), ("confirm", true)));
        Assert.Equal("confirmation_required", S(preview["status"]));
        Assert.Empty(box.Fake.Sent);
    }

    // --- retries

    [Theory]
    [InlineData(429)]
    [InlineData(500)]
    [InlineData(503)]
    [InlineData(403)]  // the fake's fault carries the reason rateLimitExceeded
    public async Task Reads_are_retried_then_succeed(int status)
    {
        await using var box = new Mailbox();
        box.Fake.Received("m1", "Contenu");
        box.Fake.Fault("get", status, status);
        var message = box.Call("get_message", A(("messageId", "m1")));
        Assert.Equal("success", S(message["status"]));
        Assert.Equal(3, box.Fake.Kinds().Count(k => k == "get"));
    }

    [Fact]
    public async Task Reads_give_up_after_five_retries()
    {
        await using var box = new Mailbox();
        box.Fake.Received("m1");
        box.Fake.Fault("get", 429, 429, 429, 429, 429, 429, 429);
        Assert.Equal("quota", S(box.Call("get_message", A(("messageId", "m1")))["code"]));
        Assert.Equal(6, box.Fake.Kinds().Count(k => k == "get"));
    }

    [Fact]
    public async Task A_send_refused_with_429_is_retried_and_sent_once()
    {
        await using var box = new Mailbox();
        box.Fake.StoredDraft("d1", "Message");
        var preview = box.Call("send_draft", A(("draftId", "d1")));
        box.Fake.Fault("message-send", 429, 429);
        var result = box.Call("send_draft", A(("draftId", "d1"), ("confirm", true), ("accord", preview["accord"]!.DeepClone())));
        Assert.True(result["sent"]!.GetValue<bool>(), result.ToJsonString());
        Assert.Equal(3, box.Fake.Kinds().Count(k => k == "message-send"));
        Assert.Single(box.Fake.Sent);
    }

    [Fact]
    public async Task A_send_answered_with_5xx_is_never_replayed()
    {
        await using var box = new Mailbox();
        box.Fake.StoredDraft("d1", "Message");
        var preview = box.Call("send_draft", A(("draftId", "d1")));
        box.Fake.Fault("message-send", 503);
        var result = box.Call("send_draft", A(("draftId", "d1"), ("confirm", true), ("accord", preview["accord"]!.DeepClone())));
        Assert.Equal("unknown", S(result["status"]));
        Assert.Equal(1, box.Fake.Kinds().Count(k => k == "message-send"));
    }

    [Fact]
    public void Waits_are_bounded_like_the_python_engine()
    {
        var random = new Random(1618);
        for (int attempt = 1; attempt <= RetryPolicy.NumRetries; attempt++)
        {
            for (int i = 0; i < 200; i++)
            {
                double read = RetryPolicy.Delay(attempt, send: false, random).TotalSeconds;
                Assert.InRange(read, 0, Math.Pow(2, attempt));
                double send = RetryPolicy.Delay(attempt, send: true, random).TotalSeconds;
                Assert.InRange(send, 0.5 * Math.Min(32, Math.Pow(2, attempt)), Math.Min(32, Math.Pow(2, attempt)));
            }
        }
        Assert.Equal(5, RetryPolicy.NumRetries);
        Assert.False(RetryPolicy.Retryable(new GmailApiException(System.Net.HttpStatusCode.ServiceUnavailable, "x"), send: true));
        Assert.True(RetryPolicy.Retryable(new GmailApiException(System.Net.HttpStatusCode.TooManyRequests, "x"), send: true));
        Assert.False(RetryPolicy.Retryable(new HttpRequestException("reset"), send: true));
        Assert.True(RetryPolicy.Retryable(new HttpRequestException("reset"), send: false));
        Assert.False(RetryPolicy.Retryable(new GmailApiException(System.Net.HttpStatusCode.Forbidden, "x") { ResponseBody = "insufficientPermissions" }, send: false));
        Assert.False(RetryPolicy.Retryable(new GmailApiException(System.Net.HttpStatusCode.NotFound, "x"), send: false));
    }
}
