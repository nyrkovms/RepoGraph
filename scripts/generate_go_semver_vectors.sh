#!/usr/bin/env bash
# Expected answers of golang.org/x/mod (semver and module, the packages the go command uses) for the Go version scheme
# tests: IsValid, Canonical, Prerelease, Build, IsPseudoVersion, PseudoVersionTime/Rev for every input, and Compare for
# every pair of versions that are valid and in full form. Needs docker and the network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/backend/tests/fixtures/go/semver"
XMOD_VERSION="v0.22.0"
GO_IMAGE="golang:1.23"

docker run --rm --platform linux/amd64 -v "$DIR:/work" -w /tmp "$GO_IMAGE" sh -c "
  mkdir vectors && cd vectors
  go mod init vectors >/dev/null 2>&1
  cat > main.go <<'GO'
package main

import (
	\"encoding/json\"
	\"fmt\"
	\"os\"
	\"strings\"
	\"time\"

	\"golang.org/x/mod/module\"
	\"golang.org/x/mod/semver\"
)

type Entry struct {
	IsValid               bool    \`json:\"IsValid\"\`
	Canonical             string  \`json:\"Canonical\"\`
	CanonicalIsSelf       bool    \`json:\"CanonicalIsSelf\"\`
	Full                  bool    \`json:\"Full\"\`
	Major                 string  \`json:\"Major\"\`
	Prerelease            string  \`json:\"Prerelease\"\`
	Build                 string  \`json:\"Build\"\`
	IsPseudoVersion       bool    \`json:\"IsPseudoVersion\"\`
	PseudoVersionTime     *string \`json:\"PseudoVersionTime\"\`
	PseudoVersionRev      *string \`json:\"PseudoVersionRev\"\`
	HasIncompatibleSuffix bool    \`json:\"HasIncompatibleSuffix\"\`
}

type Output struct {
	Tool            string           \`json:\"tool\"\`
	Versions        map[string]Entry \`json:\"versions\"\`
	Order           []string         \`json:\"order\"\`
	CompareVersions []string         \`json:\"compare_versions\"\`
	Compare         [][]int          \`json:\"compare\"\`
}

func main() {
	var input struct {
		Versions []string \`json:\"versions\"\`
		Invalid  []string \`json:\"invalid_versions\"\`
	}
	data, err := os.ReadFile(\"/work/inputs.json\")
	if err != nil {
		panic(err)
	}
	if err := json.Unmarshal(data, &input); err != nil {
		panic(err)
	}
	out := Output{Tool: \"golang.org/x/mod ${XMOD_VERSION} (\" + \"${GO_IMAGE}\" + \")\", Versions: map[string]Entry{}}
	for _, v := range append(append([]string{}, input.Versions...), input.Invalid...) {
		e := Entry{
			IsValid:    semver.IsValid(v),
			Canonical:  semver.Canonical(v),
			Major:      semver.Major(v),
			Prerelease: semver.Prerelease(v),
			Build:      semver.Build(v),
		}
		e.CanonicalIsSelf = e.IsValid && e.Canonical == v
		// A module version always has three numbers: Canonical (without the build) plus the build is the input itself.
		e.Full = e.IsValid && e.Canonical+e.Build == v
		e.IsPseudoVersion = module.IsPseudoVersion(v)
		if t, err := module.PseudoVersionTime(v); err == nil {
			s := t.UTC().Format(time.RFC3339)
			e.PseudoVersionTime = &s
		}
		if rev, err := module.PseudoVersionRev(v); err == nil {
			e.PseudoVersionRev = &rev
		}
		e.HasIncompatibleSuffix = strings.HasSuffix(v, \"+incompatible\")
		out.Versions[v] = e
		out.Order = append(out.Order, v)
	}
	for _, v := range input.Versions {
		if out.Versions[v].Full {
			out.CompareVersions = append(out.CompareVersions, v)
		}
	}
	for _, a := range out.CompareVersions {
		row := []int{}
		for _, b := range out.CompareVersions {
			row = append(row, semver.Compare(a, b))
		}
		out.Compare = append(out.Compare, row)
	}
	text, err := json.MarshalIndent(out, \"\", \"  \")
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile(\"/work/expected.json\", append(text, '\n'), 0o644); err != nil {
		panic(err)
	}
	fmt.Printf(\"x/mod ${XMOD_VERSION}: %d inputs, %d full-form versions\n\", len(out.Order), len(out.CompareVersions))
}
GO
  go get golang.org/x/mod@${XMOD_VERSION} >/dev/null 2>&1
  go run .
"
