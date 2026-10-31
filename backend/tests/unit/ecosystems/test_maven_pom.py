"""POM parsing and the effective model (IMPL J3, SPEC §7.4) on hand-written POMs."""

import asyncio

import pytest

from app.ecosystems.maven.pom import Exclusion, Pom, PomError, effective_model, parse_pom

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(body: str, ns: bool = True) -> str:
    return f'<?xml version="1.0"?><project {NS if ns else ""}><modelVersion>4.0.0</modelVersion>{body}</project>'


PARENT = _pom("""
  <groupId>org.example</groupId><artifactId>parent</artifactId><version>2.0</version><packaging>pom</packaging>
  <url>https://example.org</url>
  <properties><jackson.version>2.17.1</jackson.version><junit.version>5.10.0</junit.version></properties>
  <licenses><license><name>Apache License, Version 2.0</name></license></licenses>
  <dependencyManagement><dependencies>
    <dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId>
      <version>${jackson.version}</version>
      <exclusions><exclusion><groupId>org.unwanted</groupId><artifactId>thing</artifactId></exclusion></exclusions>
    </dependency>
    <dependency><groupId>org.junit</groupId><artifactId>junit-bom</artifactId><version>${junit.version}</version>
      <type>pom</type><scope>import</scope></dependency>
  </dependencies></dependencyManagement>
  <dependencies><dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId><version>2.0.13</version></dependency></dependencies>
""")
JUNIT_BOM = _pom("""
  <groupId>org.junit</groupId><artifactId>junit-bom</artifactId><version>5.10.0</version><packaging>pom</packaging>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><version>5.10.0</version></dependency>
    <dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId><version>1.0</version></dependency>
  </dependencies></dependencyManagement>
""")
CHILD = _pom("""
  <parent><groupId>org.example</groupId><artifactId>parent</artifactId><version>2.0</version></parent>
  <artifactId>child</artifactId>
  <properties><jackson.version>2.18.0</jackson.version></properties>
  <dependencies>
    <dependency><groupId>com.fasterxml.jackson.core</groupId><artifactId>jackson-databind</artifactId></dependency>
    <dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId><scope>test</scope></dependency>
    <dependency><groupId>${project.groupId}</groupId><artifactId>sibling</artifactId><version>${project.version}</version>
      <optional>true</optional></dependency>
    <dependency><groupId>org.env</groupId><artifactId>env</artifactId><version>${env.VERSION}</version></dependency>
  </dependencies>
  <profiles>
    <profile><id>default</id><activation><activeByDefault>true</activeByDefault></activation>
      <dependencies><dependency><groupId>org.profile</groupId><artifactId>on</artifactId><version>1</version></dependency></dependencies></profile>
    <profile><id>jdk</id><activation><jdk>21</jdk></activation>
      <dependencies><dependency><groupId>org.profile</groupId><artifactId>off</artifactId><version>1</version></dependency></dependencies></profile>
  </profiles>
""")


def _loader(poms: dict[tuple[str, str, str], str]):
    asked: list[tuple[str, str, str]] = []

    async def load(group: str, artifact: str, version: str) -> Pom | None:
        asked.append((group, artifact, version))
        text = poms.get((group, artifact, version))
        return parse_pom(text) if text else None

    return load, asked


def test_parse_with_and_without_namespace() -> None:
    for ns in (True, False):
        pom = parse_pom(_pom("<groupId>g</groupId><artifactId>a</artifactId><version>1</version>", ns))
        assert (pom.group, pom.artifact, pom.version, pom.packaging) == ("g", "a", "1", "jar")
    with pytest.raises(PomError):
        parse_pom("<notaproject/>")
    with pytest.raises(PomError):
        parse_pom("<project><broken>")
    with pytest.raises(PomError):
        parse_pom(_pom("<groupId>g</groupId>"))


def test_the_effective_model_inherits_interpolates_and_imports() -> None:
    load, asked = _loader({("org.example", "parent", "2.0"): PARENT, ("org.junit", "junit-bom", "5.10.0"): JUNIT_BOM})
    model = asyncio.run(effective_model(parse_pom(CHILD), load))
    assert (model.group, model.artifact, model.version, model.url) == ("org.example", "child", "2.0", "https://example.org")
    deps = {(d.group, d.artifact): d for d in model.dependencies}
    # The child's property wins over the parent's; managed exclusions are added.
    jackson = deps[("com.fasterxml.jackson.core", "jackson-databind")]
    assert jackson.version == "2.18.0" and jackson.exclusions == (Exclusion("org.unwanted", "thing"),)
    # From the imported BOM; the POM's own entry for jackson wins over the BOM's 1.0.
    assert deps[("org.junit.jupiter", "junit-jupiter")].version == "5.10.0"
    assert deps[("org.junit.jupiter", "junit-jupiter")].scope == "test"
    sibling = deps[("org.example", "sibling")]
    assert sibling.version == "2.0" and sibling.optional
    assert deps[("org.env", "env")].unresolved == ("env.VERSION",)
    assert ("org.slf4j", "slf4j-api") in deps                      # inherited from the parent
    assert ("org.profile", "on") in deps and ("org.profile", "off") not in deps
    assert model.license_spdx == "Apache-2.0" and model.problems == []
    assert ("org.junit", "junit-bom", "5.10.0") in asked
    assert all(d.scope != "import" for d in model.managed)


def test_missing_parents_and_boms_are_problems_not_failures() -> None:
    load, _ = _loader({})
    child = parse_pom(_pom("""<parent><groupId>g</groupId><artifactId>p</artifactId><version>1</version></parent>
      <artifactId>c</artifactId>
      <dependencyManagement><dependencies><dependency><groupId>x</groupId><artifactId>bom</artifactId>
        <version>1</version><type>pom</type><scope>import</scope></dependency></dependencies></dependencyManagement>"""))
    model = asyncio.run(effective_model(child, load))
    assert model.group == "g" and model.version == "1"
    assert model.problems == ["parent g:p:1 not found", "BOM x:bom:1 not found"]


def test_a_parent_cycle_stops() -> None:
    a = _pom("<parent><groupId>g</groupId><artifactId>b</artifactId><version>1</version></parent><artifactId>a</artifactId>")
    b = _pom("<parent><groupId>g</groupId><artifactId>a</artifactId><version>1</version></parent><artifactId>b</artifactId>")
    load, _ = _loader({("g", "b", "1"): b, ("g", "a", "1"): a})
    model = asyncio.run(effective_model(parse_pom(a), load))
    assert any("cycle" in p for p in model.problems)


def test_relocation_and_licenses() -> None:
    pom = parse_pom(_pom("""<groupId>old</groupId><artifactId>a</artifactId><version>1</version>
      <licenses><license><name>MIT License</name></license><license><name>Some Custom License</name><url>https://x</url></license></licenses>
      <distributionManagement><relocation><groupId>new</groupId><message>moved</message></relocation></distributionManagement>"""))
    load, _ = _loader({})
    model = asyncio.run(effective_model(pom, load))
    assert model.relocation is not None and (model.relocation.group, model.relocation.message) == ("new", "moved")
    assert model.license_raw == "MIT License OR Some Custom License" and model.license_spdx is None
