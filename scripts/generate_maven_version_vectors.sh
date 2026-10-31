#!/usr/bin/env bash
# Expected answers of maven-artifact (the library Maven itself uses) for the Maven version scheme tests:
# ComparableVersion.getCanonical for every version input; compareTo for every pair of versions (and for the
# non-transitive set kept apart); VersionRange.createFromVersionSpec (or its exception), the restrictions, the
# recommended version, containsVersion for every version and matchVersion over several candidate lists for every range
# input. Needs docker and the network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/backend/tests/fixtures/maven/versions"
MAVEN_ARTIFACT="3.9.9"
JACKSON="2.17.2"
MAVEN_IMAGE="maven:3.9-eclipse-temurin-21"

SRC="$(mktemp -d)"
trap 'rm -rf "$SRC"' EXIT

cat > "$SRC/pom.xml" <<XML
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>packagehub</groupId>
  <artifactId>vectors</artifactId>
  <version>1</version>
  <dependencies>
    <dependency>
      <groupId>org.apache.maven</groupId>
      <artifactId>maven-artifact</artifactId>
      <version>${MAVEN_ARTIFACT}</version>
    </dependency>
    <dependency>
      <groupId>com.fasterxml.jackson.core</groupId>
      <artifactId>jackson-databind</artifactId>
      <version>${JACKSON}</version>
    </dependency>
  </dependencies>
</project>
XML

cat > "$SRC/Vectors.java" <<'JAVA'
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.JsonNodeFactory;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.File;
import java.util.ArrayList;
import java.util.List;
import org.apache.maven.artifact.versioning.ArtifactVersion;
import org.apache.maven.artifact.versioning.ComparableVersion;
import org.apache.maven.artifact.versioning.DefaultArtifactVersion;
import org.apache.maven.artifact.versioning.Restriction;
import org.apache.maven.artifact.versioning.VersionRange;

public class Vectors {
    static final JsonNodeFactory F = JsonNodeFactory.instance;

    static List<String> strings(JsonNode node) {
        List<String> out = new ArrayList<>();
        for (JsonNode n : node) out.add(n.asText());
        return out;
    }

    // One string per row, one character per column: '<' when the row's version is lower, '=' equal, '>' higher.
    static ArrayNode matrix(List<String> versions) {
        ArrayNode rows = F.arrayNode();
        for (String a : versions) {
            StringBuilder row = new StringBuilder();
            for (String b : versions) {
                int sign = Integer.signum(new ComparableVersion(a).compareTo(new ComparableVersion(b)));
                row.append(sign < 0 ? '<' : sign == 0 ? '=' : '>');
            }
            rows.add(row.toString());
        }
        return rows;
    }

    static JsonNode bound(ArtifactVersion v) {
        return v == null ? F.nullNode() : F.textNode(v.toString());
    }

    public static void main(String[] args) throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        JsonNode input = mapper.readTree(new File("/work/inputs.json"));
        List<String> versions = strings(input.get("versions"));
        List<String> edge = strings(input.get("edge_versions"));
        List<String> cycle = strings(input.get("cycle_versions"));
        List<String> ranges = strings(input.get("ranges"));

        ObjectNode canonical = F.objectNode();
        List<String> all = new ArrayList<>(versions);
        all.addAll(edge);
        all.addAll(cycle);
        for (String raw : all) canonical.put(raw, new ComparableVersion(raw).getCanonical());

        List<List<String>> pools = new ArrayList<>();
        pools.add(versions);
        for (JsonNode pool : input.get("pools")) pools.add(strings(pool));

        ObjectNode rangeOut = F.objectNode();
        for (String spec : ranges) {
            ObjectNode entry = F.objectNode();
            VersionRange range;
            try {
                range = VersionRange.createFromVersionSpec(spec);
            } catch (Exception e) {
                entry.put("valid", false);
                entry.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
                rangeOut.set(spec, entry);
                continue;
            }
            entry.put("valid", true);
            entry.set("recommended", bound(range.getRecommendedVersion()));
            ArrayNode restrictions = F.arrayNode();
            for (Restriction r : range.getRestrictions()) {
                ArrayNode row = F.arrayNode();
                row.add(bound(r.getLowerBound()));
                row.add(r.isLowerBoundInclusive());
                row.add(bound(r.getUpperBound()));
                row.add(r.isUpperBoundInclusive());
                restrictions.add(row);
            }
            entry.set("restrictions", restrictions);
            ObjectNode contains = F.objectNode();
            for (String v : versions) contains.put(v, range.containsVersion(new DefaultArtifactVersion(v)));
            entry.set("contains", contains);
            ArrayNode best = F.arrayNode();
            for (List<String> pool : pools) {
                List<ArtifactVersion> candidates = new ArrayList<>();
                for (String p : pool) candidates.add(new DefaultArtifactVersion(p));
                ArtifactVersion match = range.matchVersion(candidates);
                int index = -1;
                for (int i = 0; i < candidates.size(); i++) if (candidates.get(i) == match) { index = i; break; }
                if (match == null) best.addNull(); else best.add(index);
            }
            entry.set("best_match", best);
            rangeOut.set(spec, entry);
        }

        List<String> compared = new ArrayList<>(versions);
        compared.addAll(edge);
        ObjectNode out = F.objectNode();
        out.put("tool", "maven-artifact " + System.getProperty("maven.artifact.version"));
        out.set("canonical", canonical);
        out.set("compare_versions", mapper.valueToTree(compared));
        out.set("compare", matrix(compared));
        out.set("cycle_versions", mapper.valueToTree(cycle));
        out.set("cycle_compare", matrix(cycle));
        ArrayNode poolsOut = F.arrayNode();
        for (List<String> p : pools) poolsOut.add(mapper.valueToTree(p));
        out.set("pools", poolsOut);
        out.set("ranges", rangeOut);
        mapper.enable(SerializationFeature.INDENT_OUTPUT);
        // ObjectMapper escapes nothing but control characters, so the non-ASCII inputs stay readable
        mapper.writeValue(new File("/work/expected.json"), out);
        System.out.println("maven-artifact: " + compared.size() + " versions, " + ranges.size() + " ranges");
    }
}
JAVA

docker run --rm --platform linux/amd64 -v "$DIR:/work" -v "$SRC:/src:ro" -w /tmp "$MAVEN_IMAGE" sh -c '
  set -e
  mkdir vectors && cp /src/pom.xml /src/Vectors.java vectors/ && cd vectors
  mvn -q -B dependency:copy-dependencies -DoutputDirectory=lib
  javac -encoding UTF-8 -cp "lib/*" -d out Vectors.java
  java -Dfile.encoding=UTF-8 -Dmaven.artifact.version='"$MAVEN_ARTIFACT"' -cp "out:lib/*" Vectors
'
