using OmniVec.ChangeFeed.Models;
using OmniVec.ChangeFeed.Services;

internal static class GarnetSourceTests
{
    public static Task<int> RunAsync()
    {
        var failures = 0;
        void Check(string name, Action test)
        {
            try { test(); Console.WriteLine($"PASS {name}"); }
            catch (Exception error) { failures++; Console.WriteLine($"FAIL {name}: {error.Message}"); }
        }
        void Require(bool condition)
        {
            if (!condition) throw new Exception("Assertion failed");
        }
        Check("Garnet source preserves JSON fields, stable reference and version", () =>
        {
            var record = GarnetSourceRecordParser.Parse("reference",
                """{"id":"document","version":2,"content":"hello","custom":true}""");
            Require(record.Id == "document" && record.Version == 2 && record.Content == "hello");
            Require(record.RawFields["custom"] == "true" && record.ContentHash.Length == 64 && !record.Deleted);
        });
        Check("Garnet versioned tombstones are explicit and replayable", () =>
        {
            var record = GarnetSourceRecordParser.Parse("reference",
                """{"id":"document","version":3,"content":"","deleted":true}""");
            Require(record.Deleted && record.Version == 3);
        });
        Check("Garnet malformed source contracts fail explicitly", () =>
        {
            foreach (var json in new[] { "[]", "{}",
                """{"id":"doc","version":0,"content":"hi"}""",
                """{"id":"doc","version":1.5,"content":"hi"}""",
                """{"id":"doc","version":1,"content":null}""",
                """{"id":"doc","version":1,"content":"hi","deleted":"true"}""" })
            {
                try { GarnetSourceRecordParser.Parse("ref", json); }
                catch (Exception error) when (error is System.Text.Json.JsonException or InvalidDataException)
                { continue; }
                throw new Exception($"Invalid source accepted: {json}");
            }
        });
        Check("Garnet same-version mutation has a different checkpoint digest", () =>
        {
            var first = GarnetSourceRecordParser.Parse("ref",
                """{"id":"doc","version":1,"content":"first"}""");
            var second = GarnetSourceRecordParser.Parse("ref",
                """{"id":"doc","version":1,"content":"second"}""");
            Require(first.ContentHash != second.ContentHash);
        });
        Check("Garnet model edits and resets advance destination fencing revision", () =>
        {
            var pipeline = new Pipeline { UpdatedAt = "2026-10-06T04:00:00Z" };
            var first = GarnetSourceWatcher.GetPipelineRevision(pipeline);
            pipeline.UpdatedAt = "2026-10-06T04:01:00Z";
            var edited = GarnetSourceWatcher.GetPipelineRevision(pipeline);
            pipeline.ResetAt = "2026-10-06T04:02:00Z";
            Require(edited > first && GarnetSourceWatcher.GetPipelineRevision(pipeline) > edited);
        });
        Console.WriteLine($"GARNET SOURCE: {5 - failures} passed, {failures} failed");
        return Task.FromResult(failures);
    }
}
