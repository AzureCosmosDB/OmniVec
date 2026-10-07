using System.Buffers.Binary;
using System.Security.Cryptography;
using Newtonsoft.Json.Linq;

namespace OmniVec.Mock;

public static class Wire
{
    public const string Media = "application/vnd.omnivec.embeddings.f32";
    public const int MaxBody = 4_194_320;

    public static int Validate(byte[] body, int dimensions, int? expectedCount = null)
    {
        if (body.Length < 16 || !body.AsSpan(0, 4).SequenceEqual("OVEC"u8)
            || BinaryPrimitives.ReadUInt16LittleEndian(body.AsSpan(4)) != 1
            || BinaryPrimitives.ReadUInt16LittleEndian(body.AsSpan(6)) != 0)
            throw new ArgumentException("Invalid vector header");
        uint count = BinaryPrimitives.ReadUInt32LittleEndian(body.AsSpan(8));
        uint dim = BinaryPrimitives.ReadUInt32LittleEndian(body.AsSpan(12));
        if (count is < 1 or > 2048 || dim != dimensions
            || (expectedCount.HasValue && count != expectedCount.Value)
            || (ulong)count * dim > 1_048_576 || body.Length != 16L + (long)count * dim * 4)
            throw new ArgumentException("Vector dimensions, count, length or limit mismatch");
        for (int offset = 16; offset < body.Length; offset += 4)
            if (!float.IsFinite(BinaryPrimitives.ReadSingleLittleEndian(body.AsSpan(offset))))
                throw new ArgumentException("Non-finite vector value");
        return (int)count;
    }

    public static byte[] Pack(JArray outputs, int count, int dimensions)
    {
        if (count is < 1 or > 2048 || (long)count * dimensions > 1_048_576 || outputs.Count != count)
            throw new ArgumentException("Embedding count or size mismatch");
        var body = new byte[16 + count * dimensions * 4];
        "OVEC"u8.CopyTo(body);
        BinaryPrimitives.WriteUInt16LittleEndian(body.AsSpan(4), 1);
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(8), (uint)count);
        BinaryPrimitives.WriteUInt32LittleEndian(body.AsSpan(12), (uint)dimensions);
        int offset = 16;
        foreach (var output in outputs)
        {
            var vector = output[0] is JArray nested ? nested : output as JArray;
            if (vector is null || vector.Count != dimensions)
                throw new ArgumentException("Embedding dimension mismatch");
            foreach (var value in vector)
            {
                if (value.Type is not (JTokenType.Float or JTokenType.Integer))
                    throw new ArgumentException("Non-numeric embedding value");
                float number = (float)value;
                if (!float.IsFinite(number)) throw new ArgumentException("Non-finite embedding value");
                BinaryPrimitives.WriteSingleLittleEndian(body.AsSpan(offset), number);
                offset += 4;
            }
        }
        return body;
    }

    public static string Digest(byte[] body) => Convert.ToHexString(SHA256.HashData(body)).ToLowerInvariant();

    public static string Text(long index, int size, int seed)
    {
        string pattern = $"{index:x8}{seed:x8} document ";
        return string.Create(size, pattern, (span, value) =>
        {
            for (int i = 0; i < span.Length; i++) span[i] = value[i % value.Length];
        });
    }

    public static double RefreshSeconds()
    {
        double seconds = double.Parse(Environment.GetEnvironmentVariable("OMNIVEC_MOCK_CONFIG_REFRESH_SECONDS")
            ?? "300", System.Globalization.CultureInfo.InvariantCulture);
        if (!double.IsFinite(seconds) || seconds < 1)
            throw new InvalidOperationException("Mock configuration refresh must be finite and at least 1 second");
        return seconds;
    }

    public static double Epoch() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
}
