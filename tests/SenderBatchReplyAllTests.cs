using System.Text.Json.Nodes;
using MimeKit;

namespace NexusAGYmail.Tests;

/// <summary>Sender authentication read from Gmail's own header, bulk labelling in one batchModify, retry advice on
/// failures and reply all.</summary>
public class SenderBatchReplyAllTests
{
    private static JsonObject A(params (string Key, JsonNode? Value)[] pairs)
    {
        var o = new JsonObject();
        foreach (var (k, v) in pairs) o[k] = v;
        return o;
    }

    private static string S(JsonNode? node) => node?.GetValue<string>() ?? "";

    private const string GmailPass = "mx.google.com; dkim=pass header.i=@example.com header.s=s1 header.b=abc; spf=pass (google.com: domain of alice@example.com designates 192.0.2.1 as permitted sender) smtp.mailfrom=alice@example.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com";
    private const string GmailFail = "mx.google.com; spf=fail (google.com: domain of bank.example does not designate 192.0.2.9 as permitted sender) smtp.mailfrom=bank.example; dmarc=fail (p=QUARANTINE sp=QUARANTINE dis=QUARANTINE) header.from=bank.example";

    /// <summary>An inbox message with its Authentication-Results headers, topmost first.</summary>
    private static void Store(FakeGmail fake, string id, string from, string to, string cc = "", params string[] results)
    {
        using var mime = new MimeMessage();
        mime.From.Add(MailboxAddress.Parse(from));
        mime.To.AddRange(InternetAddressList.Parse(to));
        if (cc.Length > 0) mime.Cc.AddRange(InternetAddressList.Parse(cc));
        mime.Subject = "Facture";
        mime.MessageId = id + "@example.com";
        mime.Body = new TextPart("plain") { Text = "Bonjour\n" };
        for (int i = results.Length - 1; i >= 0; i--) mime.Headers.Insert(0, new Header("Authentication-Results", results[i]));
        var message = FakeGmail.DraftMessage(id, FakeGmail.Bytes(mime), "t" + id);
        message["labelIds"] = new JsonArray("INBOX");
        fake.Add(message);
    }

    [Fact]
    public async Task Gmail_verdict_is_read_and_forged_headers_are_ignored()
    {
        await using var box = new Mailbox();
        Store(box.Fake, "pass", "Alice <alice@example.com>", "test@example.com", "", GmailPass);
        // Gmail says fail on top; a "pass" the sender wrote below, even naming mx.google.com, is not believed.
        Store(box.Fake, "spoof", "Banque <support@bank.example>", "test@example.com", "", GmailFail, GmailPass.Replace("example.com", "bank.example"));
        // Only another server's header (no Gmail check): unverified, whatever it claims.
        Store(box.Fake, "other", "Bob <bob@example.org>", "test@example.com", "", "relay.example.org; dmarc=pass header.from=example.org");

        var pass = box.Call("get_message", A(("messageId", "pass")))["message"]!;
        Assert.Equal("pass", S(pass["authentication"]!["verdict"]));
        Assert.Equal("pass", S(pass["authentication"]!["spf"]));
        Assert.Equal("pass", S(pass["authentication"]!["dkim"]));
        Assert.Equal("example.com", S(pass["authentication"]!["domain"]));
        Assert.Null(pass["authentication_warning"]);

        var spoof = box.Call("get_message", A(("messageId", "spoof")))["message"]!;
        Assert.Equal("fail", S(spoof["authentication"]!["verdict"]));
        Assert.Equal("fail", S(spoof["authentication"]!["spf"]));
        Assert.Contains("forged", S(spoof["authentication_warning"]));

        var other = box.Call("get_thread", A(("threadId", "tother")))["thread"]!["messages"]![0]!;
        Assert.Equal("unverified", S(other["authentication"]!["verdict"]));
        Assert.Null(other["authentication"]!["dmarc"]);
    }

    [Fact]
    public async Task Message_labels_go_in_one_batch_and_threads_stay_one_by_one()
    {
        await using var box = new Mailbox();
        var ids = Enumerable.Range(1, 1000).Select(i => "b" + i).ToList();
        foreach (string id in ids) Store(box.Fake, id, "Alice <alice@example.com>", "test@example.com");
        var result = box.Call("unlabel_message", A(("messageId", new JsonArray(ids.Select(i => (JsonNode)i).ToArray())), ("labelIds", new JsonArray("INBOX"))));
        Assert.Equal(1000, result["modified"]!.GetValue<int>());
        Assert.Equal(1, box.Fake.Kinds().Count(k => k == "batchModify"));
        Assert.DoesNotContain("message-modify", box.Fake.Kinds());
        Assert.All(ids, id => Assert.DoesNotContain("INBOX", FakeGmail.LabelsOf(box.Fake.Messages[id])));

        var tooMany = box.Call("label_message", A(("messageId", new JsonArray(Enumerable.Range(0, 1001).Select(i => (JsonNode)("x" + i)).ToArray())), ("labelIds", new JsonArray("STARRED"))));
        Assert.Equal("invalid_argument", S(tooMany["code"]));
        Assert.Equal("not_done", S(tooMany["outcome"]));

        Assert.Equal("success", S(box.Call("label_thread", A(("threadId", new JsonArray("tb1", "tb2")), ("labelIds", new JsonArray("STARRED"))))["status"]));
        Assert.Equal(2, box.Fake.Kinds().Count(k => k == "thread-modify"));
    }

    [Fact]
    public async Task Failures_say_whether_a_new_attempt_is_safe()
    {
        await using var box = new Mailbox();
        Store(box.Fake, "m1", "Alice <alice@example.com>", "test@example.com");
        Store(box.Fake, "m2", "Alice <alice@example.com>", "test@example.com");

        box.Fake.Fault("get", 429, 429, 429, 429, 429, 429);
        var quota = box.Call("get_message", A(("messageId", "m1")));
        Assert.Equal("quota", S(quota["code"]));
        Assert.True(quota["retry_safe"]!.GetValue<bool>());
        Assert.Equal("not_done", S(quota["outcome"]));

        box.Fake.FaultReason = "insufficientPermissions";
        box.Fake.Fault("get", 403);
        var forbidden = box.Call("get_message", A(("messageId", "m1")));
        Assert.Equal("forbidden", S(forbidden["code"]));
        Assert.False(forbidden["retry_safe"]!.GetValue<bool>());

        box.Fake.FaultReason = "backendError";
        // A draft creation Gmail may have received: never replayed blindly (a second draft).
        box.Fake.Fault("draft", 503, 503, 503, 503, 503, 503);
        var draft = box.Call("create_draft", A(("body", "x"), ("to", "a@example.com")));
        Assert.Equal("unknown", S(draft["outcome"]));
        Assert.False(draft["retry_safe"]!.GetValue<bool>());

        // Labelling is idempotent: an unknown outcome may be resent; a thread loop stopped midway is partial.
        box.Fake.Fault("thread-modify", 200, 503, 503, 503, 503, 503, 503);
        var partial = box.Call("label_thread", A(("threadId", new JsonArray("tm1", "tm2")), ("labelIds", new JsonArray("STARRED"))));
        Assert.Equal("partial", S(partial["outcome"]));
        Assert.True(partial["retry_safe"]!.GetValue<bool>());
        Assert.Equal("tm1", S(partial["completed"]![0]));
    }

    [Fact]
    public async Task Reply_all_copies_the_original_recipients_without_this_account()
    {
        await using var box = new Mailbox();
        Store(box.Fake, "r1", "Alice <alice@example.com>", "test@example.com, Carol <carol@example.com>", "alias@example.com, dave@example.com, Alice <alice@example.com>");
        box.Call("create_draft", A(("body", "Merci"), ("replyToMessageId", "r1"), ("replyAll", true)));
        using var draft = MimeMessage.Load(new MemoryStream(FakeGmail.FromB64(S(box.Fake.Drafts[^1]["message"]!["raw"]))), TestContext.Current.CancellationToken);
        Assert.Equal(["alice@example.com", "carol@example.com"], draft.To.Mailboxes.Select(m => m.Address));
        Assert.Equal(["dave@example.com"], draft.Cc.Mailboxes.Select(m => m.Address));
        Assert.Equal("r1@example.com", draft.InReplyTo);
        Assert.Empty(box.Fake.Sent);

        // Without replyAll: the sender only, as before.
        box.Call("create_draft", A(("body", "Merci"), ("replyToMessageId", "r1")));
        using var single = MimeMessage.Load(new MemoryStream(FakeGmail.FromB64(S(box.Fake.Drafts[^1]["message"]!["raw"]))), TestContext.Current.CancellationToken);
        Assert.Equal(["alice@example.com"], single.To.Mailboxes.Select(m => m.Address));
        Assert.Empty(single.Cc);
        Assert.DoesNotContain("message-send", box.Fake.Kinds());
    }
}
