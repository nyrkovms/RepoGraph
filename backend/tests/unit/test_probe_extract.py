import io
import zipfile

from app.probe.extract import ext_counts, extract_wheel_facts


def build_wheel(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def test_extracts_entry_points_and_top_level():
    payload = build_wheel(
        {
            "pkg/__init__.py": "",
            "pkg/cli.py": "",
            "pkg-1.0.dist-info/entry_points.txt": "[console_scripts]\npkg = pkg.cli:main\n",
            "pkg-1.0.dist-info/top_level.txt": "pkg\n",
        }
    )
    facts = extract_wheel_facts(payload, "pkg-1.0-py3-none-any.whl")
    assert facts.error is None
    assert "console_scripts" in facts.entry_points_txt
    assert facts.top_level_txt.strip() == "pkg"
    assert facts.file_facts["total"] == 4
    assert facts.file_facts["platform_wheel"] is False
    assert facts.file_facts["ext_counts"]["py"] == 2


def test_detects_data_scripts():
    payload = build_wheel(
        {
            "ruff/__init__.py": "",
            "ruff-0.1.0.data/scripts/ruff": "#!/bin/sh\n",
        }
    )
    facts = extract_wheel_facts(payload, "ruff-0.1.0-py3-none-manylinux_x86_64.whl")
    assert facts.error is None
    assert facts.file_facts["scripts"] == ["ruff"]
    assert facts.file_facts["platform_wheel"] is True


def test_missing_dist_info_files_yield_none():
    payload = build_wheel({"pkg/__init__.py": ""})
    facts = extract_wheel_facts(payload, "pkg-1.0-py3-none-any.whl")
    assert facts.error is None
    assert facts.entry_points_txt is None
    assert facts.top_level_txt is None


def test_metadata_description_extracted():
    payload = build_wheel(
        {
            "pkg/__init__.py": "",
            "pkg-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n\n# pkg\n\nDoes useful things.\n"
            ),
        }
    )
    facts = extract_wheel_facts(payload, "pkg-1.0-py3-none-any.whl")
    assert facts.error is None
    assert facts.metadata_description.startswith("# pkg")
    assert "Does useful things." in facts.metadata_description


def test_metadata_without_body_gives_no_description():
    payload = build_wheel(
        {"pkg-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"}
    )
    facts = extract_wheel_facts(payload, "pkg-1.0-py3-none-any.whl")
    assert facts.metadata_description is None


def test_bad_zip_is_permanent_error():
    facts = extract_wheel_facts(b"not a zip at all", "pkg-1.0-py3-none-any.whl")
    assert facts.error is not None
    assert facts.error.startswith("wheel parse error")
    assert facts.file_facts is None


def test_ext_counts_limits_and_no_extension():
    counts = ext_counts(["a.py", "b.PY", "Makefile", "dir/c.txt"])
    assert counts["py"] == 2
    assert counts["<none>"] == 1
    assert counts["txt"] == 1
