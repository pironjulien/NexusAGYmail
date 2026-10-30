using System.Text;
using MimeKit;

namespace NexusAGYmail.Tests;

/// <summary>
/// Properties of what an agent or a sender chooses and what ends up in a mail header or on disk, over many seeded
/// pseudo-random inputs (formerly the Python fuzz targets):
/// - recipient lists: an accepted list holds no line break of any kind (else one more header, a hidden recipient),
///   every address parses back, and normalizing again changes nothing;
/// - attachment names: the name stays in the chosen folder and Windows can create it as is (no path, no forbidden or
///   invisible character, no reserved name, no trailing dot or space, at most 255 UTF-16 units).
/// </summary>
public class PropertyTests
{
    private const string Alphabet = "abcAZ09.-_+@<>\"(),;:\\ \t\r\n\u0085\u2028\u2029\u0000\u200b\u00e9\u4e2d=?/*|%'[]{}!#$&~`^";

    private static IEnumerable<string> Inputs(int seed, int count)
    {
        var random = new Random(seed);
        string[] seeds =
        [
            "alice@example.com", "Bob <bob@example.com>", "\"Doe, John\" <john@example.com>", "a:a:a:b@c.d;",
            "=?utf-8?q?=C3=A9?= <e@example.com>", "CON.txt", "..\\..\\evil.exe", "report.pdf ", "nul", "a\u202etxt.exe",
        ];
        foreach (string s in seeds)
            yield return s;
        for (int i = 0; i < count; i++)
        {
            var text = new StringBuilder(seeds[random.Next(seeds.Length)]);
            int edits = random.Next(1, 12);
            for (int e = 0; e < edits; e++)
            {
                int at = random.Next(text.Length + 1);
                if (random.Next(3) == 0 && text.Length > 0 && at < text.Length) text.Remove(at, 1);
                else text.Insert(at, Alphabet[random.Next(Alphabet.Length)]);
            }
            if (random.Next(10) == 0) text.Append(new string('x', random.Next(200, 400)));
            yield return text.ToString();
        }
    }

    [Fact]
    public void Accepted_recipient_lists_never_carry_a_hidden_header()
    {
        int accepted = 0;
        foreach (string input in Inputs(20261030, 4000))
        {
            // The parser create_draft and update_draft use for to, cc and bcc.
            var message = new MimeMessage { Subject = "s", Body = new TextPart("plain") { Text = "b" } };
            try { MailOperations.SetRecipients(message.To, input, required: true); }
            catch (Exception ex) when (ex is ArgumentException or ParseException) { continue; }
            accepted++;
            Assert.All(message.To.Mailboxes, m => Assert.False(m.Address.IndexOfAny(['\r', '\n', '\0', '\u0085', '\u2028', '\u2029']) >= 0, $"line break in {m.Address} (from {input})"));
            using var stream = new MemoryStream();
            message.WriteTo(stream, TestContext.Current.CancellationToken);
            var reread = MimeMessage.Load(new MemoryStream(stream.ToArray()), TestContext.Current.CancellationToken);
            Assert.Equal(message.Headers.Select(h => h.Field), reread.Headers.Select(h => h.Field));
            Assert.Empty(reread.Bcc);
            Assert.Equal(message.To.Mailboxes.Select(m => m.Address), reread.To.Mailboxes.Select(m => m.Address));
        }
        Assert.True(accepted > 100, $"only {accepted} inputs accepted");
    }

    // Zero-width, bidirectional and invisible formatting characters (a name that hides its real extension).
    private static readonly System.Text.RegularExpressions.Regex Invisible = new(@"[\u200B-\u200F\u202A-\u202E\u2060-\u2064\u2066-\u2069\uFEFF]");

    private static readonly HashSet<string> Reserved = new(StringComparer.OrdinalIgnoreCase)
    {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "COM¹", "COM²", "COM³",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9", "LPT¹", "LPT²", "LPT³",
    };

    [Fact]
    public void Attachment_names_are_always_safe_on_disk()
    {
        string folder = Support.NewTemp("names");
        try
        {
            foreach (string input in Inputs(1618, 4000))
            {
                string name = AttachmentSecurity.SafeName(input);
                Assert.False(string.IsNullOrEmpty(name), $"empty name from {input}");
                Assert.True(name.IndexOfAny(Path.GetInvalidFileNameChars()) < 0 && name.IndexOfAny(['/', '\\', ':', '*', '?', '"', '<', '>', '|']) < 0, $"unsafe {name} (from {input})");
                Assert.False(Invisible.IsMatch(name), $"invisible character in {name} (from {input})");
                Assert.False(name.StartsWith('.') || name.EndsWith('.') || name.EndsWith(' '), $"leading dot or trailing dot/space in {name} (from {input})");
                Assert.False(Reserved.Contains(name.Split('.')[0].TrimEnd()), $"reserved name {name} (from {input})");
                Assert.True(name.Length <= 255, $"too long ({name.Length}) from {input}");
                string full = Path.GetFullPath(Path.Combine(folder, name));
                Assert.Equal(folder, Path.GetDirectoryName(full));
            }
        }
        finally { Support.Delete(folder); }
    }
}
