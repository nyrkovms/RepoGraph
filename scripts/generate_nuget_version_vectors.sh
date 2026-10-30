#!/usr/bin/env bash
# Expected answers of NuGet.Versioning (the library NuGet itself uses) for the NuGet version scheme tests: NuGetVersion
# .TryParse, ToNormalizedString and friends for every version input; CompareTo for every pair of valid versions;
# VersionRange.TryParse, the normalized range, Satisfies for every valid version and FindBestMatch over several
# candidate lists for every range input. Needs docker and the network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/backend/tests/fixtures/nuget/versioning"
NUGET_VERSIONING="6.11.1"
DOTNET_IMAGE="mcr.microsoft.com/dotnet/sdk:8.0"

SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT

cat > "$SRC/vectors.csproj" <<XML
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>disable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <InvariantGlobalization>true</InvariantGlobalization>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="NuGet.Versioning" Version="${NUGET_VERSIONING}" />
  </ItemGroup>
</Project>
XML

cat > "$SRC/Program.cs" <<'CS'
using System.Text.Json;
using System.Text.Json.Nodes;
using NuGet.Versioning;

var input = JsonNode.Parse(File.ReadAllText("/work/inputs.json"));
List<string> Strings(string key) => input[key].AsArray().Select(n => n.GetValue<string>()).ToList();

string Norm(NuGetVersion v) => v?.ToNormalizedString();

var versions = new JsonObject();
var valid = new List<(string Raw, NuGetVersion Version)>();
foreach (var raw in Strings("versions").Concat(Strings("edge_versions")))
{
    var entry = new JsonObject();
    if (NuGetVersion.TryParse(raw, out var v))
    {
        valid.Add((raw, v));
        entry["valid"] = true;
        entry["normalized"] = v.ToNormalizedString();
        entry["is_prerelease"] = v.IsPrerelease;
        entry["major"] = v.Major;
        entry["minor"] = v.Minor;
        entry["patch"] = v.Patch;
        entry["revision"] = v.Revision;
        entry["release_labels"] = new JsonArray(v.ReleaseLabels.Select(l => (JsonNode)JsonValue.Create(l)).ToArray());
        entry["has_metadata"] = v.HasMetadata;
    }
    else
    {
        entry["valid"] = false;
    }
    versions[raw] = entry;
}

var compare = new JsonArray();
foreach (var (_, a) in valid)
{
    var row = new JsonArray();
    foreach (var (_, b) in valid) row.Add(Math.Sign(a.CompareTo(b)));
    compare.Add(row);
}

JsonNode RangeSummary(VersionRange r, NuGetVersion[] pool)
{
    // FindBestMatch answers by position in the candidate list so equal versions with different spellings differ.
    var best = r.FindBestMatch(pool);
    return best == null ? null : JsonValue.Create(Array.FindIndex(pool, p => ReferenceEquals(p, best)));
}

var poolDefs = new List<List<string>> { valid.Select(x => x.Raw).ToList() };
poolDefs.AddRange(input["pools"].AsArray().Select(p => p.AsArray().Select(n => n.GetValue<string>()).ToList()));

var ranges = new JsonObject();
foreach (var raw in Strings("ranges"))
{
    if (!VersionRange.TryParse(raw, out var r)) { ranges[raw] = null; continue; }
    var entry = new JsonObject
    {
        ["normalized"] = r.ToNormalizedString(),
        ["min"] = Norm(r.MinVersion),
        ["min_inclusive"] = r.IsMinInclusive,
        ["max"] = Norm(r.MaxVersion),
        ["max_inclusive"] = r.IsMaxInclusive,
        ["floating"] = r.IsFloating ? r.Float.FloatBehavior.ToString() : null,
        ["float_min"] = r.IsFloating ? Norm(r.Float.MinVersion) : null,
        ["float_string"] = r.IsFloating ? r.Float.ToString() : null,
    };
    var satisfies = new JsonObject();
    var floatSatisfies = new JsonObject();
    foreach (var (vraw, v) in valid)
    {
        satisfies[vraw] = r.Satisfies(v);
        if (r.IsFloating) floatSatisfies[vraw] = r.Float.Satisfies(v);
    }
    entry["satisfies"] = satisfies;
    if (r.IsFloating) entry["float_satisfies"] = floatSatisfies;
    var best = new JsonArray();
    foreach (var poolRaw in poolDefs)
    {
        // Candidates that NuGet cannot parse never reach FindBestMatch.
        var pool = poolRaw.Select(p => NuGetVersion.Parse(p)).ToArray();
        best.Add(RangeSummary(r, pool));
    }
    entry["best_match"] = best;
    ranges[raw] = entry;
}

var pools = new JsonArray();
foreach (var p in poolDefs) pools.Add(new JsonArray(p.Select(x => (JsonNode)JsonValue.Create(x)).ToArray()));

var output = new JsonObject
{
    ["tool"] = "NuGet.Versioning " + typeof(NuGetVersion).Assembly.GetName().Version,
    ["versions"] = versions,
    ["compare_versions"] = new JsonArray(valid.Select(x => (JsonNode)JsonValue.Create(x.Raw)).ToArray()),
    ["compare"] = compare,
    ["pools"] = pools,
    ["ranges"] = ranges,
};
var options = new JsonSerializerOptions { WriteIndented = true, TypeInfoResolver = new System.Text.Json.Serialization.Metadata.DefaultJsonTypeInfoResolver(), Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
File.WriteAllText("/work/expected.json", output.ToJsonString(options) + "\n");
Console.WriteLine($"NuGet.Versioning: {valid.Count} valid versions, {ranges.Count} ranges");
CS

docker run --rm --platform linux/amd64 -v "$DIR:/work" -v "$SRC:/src:ro" -w /tmp "$DOTNET_IMAGE" sh -c '
  export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
  mkdir vectors && cp /src/vectors.csproj /src/Program.cs vectors/ && cd vectors
  dotnet run -c Release
'
