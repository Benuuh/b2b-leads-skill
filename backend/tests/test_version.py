"""The frozen bundle carries one version marker; no copy may drift."""
import re

from app import __version__
from app.capabilities import capabilities
from app.config import ROOT
from app.release import CORE_VERSION, SKILL_VERSION, release_identity


def skill_frontmatter():
    text = (ROOT / "skills" / "b2b-leads" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    header = text.split("---", 2)[1]
    values = {}
    for line in header.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"')
    return values, text


def test_one_version_marker_everywhere():
    marker = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", marker)
    assert marker == __version__ == CORE_VERSION == SKILL_VERSION

    header, text = skill_frontmatter()
    assert header["version"] == marker
    assert f"Skill {marker}" in text and f"核心 {marker}" in text

    envelope = capabilities()
    assert envelope["skill_version"] == envelope["core_version"] == marker
    assert release_identity()["skill_version"] == marker


def test_packaging_metadata_and_bundle_readme_match():
    pyproject = (ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1) == __version__
    # The installation record names the active release; it must not claim another core.
    installation = (ROOT / "INSTALLATION.md").read_text(encoding="utf-8")
    assert f"核心 `{__version__}`" in installation
