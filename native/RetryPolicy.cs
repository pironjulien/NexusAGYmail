using System.Net;

namespace NexusAGYmail;

/// <summary>
/// Automatic retries, as the Python engine did (google-api-python-client, num_retries=5):
/// - every request except a send: up to 5 retries on 429, 500, 502, 503, 504, a 403 whose reason is a rate limit
///   (rateLimitExceeded, userRateLimitExceeded) and a network failure or timeout; wait random(0..1) × 2^n seconds
///   before retry n (n = 1..5), Retry-After not read;
/// - a send (messages.send): only a 429, refused before any processing, up to 5 retries, wait
///   min(32, 2^n) × random(0.5..1) seconds. A 5xx, a timeout or a network failure may mean the mail left: never replayed.
/// NEXUS_AGYMAIL_BACKOFF_SCALE (tests only) multiplies the waits.
/// </summary>
public static class RetryPolicy
{
    public const int NumRetries = 5;
    private static readonly double Scale =
        double.TryParse(Environment.GetEnvironmentVariable("NEXUS_AGYMAIL_BACKOFF_SCALE"), System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out double scale) && scale >= 0 ? scale : 1.0;

    public static Action<TimeSpan> Sleep { get; set; } = Thread.Sleep;

    /// <summary>Wait before retry <paramref name="attempt"/> (1..5).</summary>
    public static TimeSpan Delay(int attempt, bool send, Random? random = null)
    {
        random ??= Random.Shared;
        double seconds = send
            ? Math.Min(32.0, Math.Pow(2, attempt)) * (0.5 + random.NextDouble() * 0.5)
            : random.NextDouble() * Math.Pow(2, attempt);
        return TimeSpan.FromSeconds(seconds);
    }

    public static bool Retryable(Exception error, bool send)
    {
        if (error is GmailApiException api)
        {
            int status = (int)api.StatusCode;
            if (status == 429) return true;
            if (send) return false;
            return status is 500 or 502 or 503 or 504 ||
                   (api.StatusCode == HttpStatusCode.Forbidden &&
                    (api.ResponseBody.Contains("userRateLimitExceeded", StringComparison.Ordinal) ||
                     api.ResponseBody.Contains("rateLimitExceeded", StringComparison.Ordinal)));
        }
        return !send && error is HttpRequestException or TaskCanceledException;
    }

    public static T Run<T>(bool send, Func<T> action)
    {
        for (int attempt = 0; ; attempt++)
        {
            try { return action(); }
            catch (Exception error) when (attempt < NumRetries && Retryable(error, send))
            {
                Sleep(Delay(attempt + 1, send) * Scale);
            }
        }
    }
}
