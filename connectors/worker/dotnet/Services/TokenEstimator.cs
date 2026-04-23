namespace OmniVec.Worker.Services;

/// <summary>
/// Dependency-free, script-aware token estimator.
///
/// Tokenizer ratios vary wildly by Unicode script. A naïve chars/4 heuristic
/// (tuned for English) under-counts tokens by 5-6× on CJK and Indic text,
/// which would silently bust the model context window.
///
/// We sample the first 256 chars, classify by Unicode block, and pick a
/// per-script tokens-per-char ratio. This is cheap (~ns), needs no
/// dependencies, and works for every embedding model family. We keep generous
/// safety margins; a residual 4xx from the embed endpoint triggers
/// bisect-and-dead-letter in the worker, so the estimator only needs to be
/// "usually right", not perfect.
/// </summary>
public static class TokenEstimator
{
    // Tokens-per-char ratios tuned conservatively for cl100k_base / SentencePiece-style
    // BPE tokenizers. Values lean high so we under-pack rather than overflow.
    private const double LatinRatio    = 0.30; // ~chars/3.3 — English/European text
    private const double CyrillicRatio = 0.55; // Russian/Ukrainian/etc.
    private const double ArabicRatio   = 0.75; // Arabic/Hebrew
    private const double IndicRatio    = 1.20; // Devanagari/Bengali/Tamil/etc.
    private const double CjkRatio      = 1.60; // Chinese/Japanese/Korean — worst case
    private const double UnknownRatio  = 0.80; // Mixed / emoji / base64 — be conservative

    /// <summary>
    /// Estimate token count for a single string. Returns at least 1 for non-empty input.
    /// </summary>
    public static int Estimate(string? text)
    {
        if (string.IsNullOrEmpty(text)) return 0;
        var ratio = TokensPerChar(text);
        var tokens = (int)Math.Ceiling(text.Length * ratio);
        return Math.Max(1, tokens);
    }

    /// <summary>
    /// Sample-based dominant-script detection. Uses up to 256 chars from the
    /// start of the string; this is sufficient for ratio selection because
    /// real-world inputs rarely mix scripts in their leading content.
    /// </summary>
    private static double TokensPerChar(string s)
    {
        int cjk = 0, indic = 0, arabic = 0, cyrillic = 0, latin = 0, other = 0;
        int sample = Math.Min(s.Length, 256);
        for (int i = 0; i < sample; i++)
        {
            int c = s[i];
            // Skip ASCII whitespace from script scoring (it occurs in every script)
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') continue;

            if      (c >= 0x4E00 && c <= 0x9FFF) cjk++;          // CJK Unified Ideographs
            else if (c >= 0x3040 && c <= 0x30FF) cjk++;          // Hiragana / Katakana
            else if (c >= 0xAC00 && c <= 0xD7AF) cjk++;          // Hangul syllables
            else if (c >= 0x3400 && c <= 0x4DBF) cjk++;          // CJK Extension A
            else if (c >= 0x0900 && c <= 0x0DFF) indic++;        // Devanagari..Sinhala
            else if (c >= 0x0600 && c <= 0x06FF) arabic++;       // Arabic
            else if (c >= 0x0590 && c <= 0x05FF) arabic++;       // Hebrew
            else if (c >= 0x0400 && c <= 0x04FF) cyrillic++;     // Cyrillic
            else if (c >= 0x0370 && c <= 0x03FF) cyrillic++;     // Greek (similar token cost)
            else if (c < 0x0250)                  latin++;        // Basic Latin + Latin-1
            else if (c >= 0x1F000)                other++;        // Emoji / supplementary
            else                                  other++;
        }

        int max = cjk;
        double ratio = CjkRatio;
        if (indic    > max) { max = indic;    ratio = IndicRatio; }
        if (arabic   > max) { max = arabic;   ratio = ArabicRatio; }
        if (cyrillic > max) { max = cyrillic; ratio = CyrillicRatio; }
        if (latin    > max) { max = latin;    ratio = LatinRatio; }
        if (other    > max) { max = other;    ratio = UnknownRatio; }

        // If nothing was scored (e.g. all whitespace), fall back to Latin.
        return max == 0 ? LatinRatio : ratio;
    }
}
