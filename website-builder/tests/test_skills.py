"""Phase 5 tests: real Hermes skill directories exist and are well-formed.

The runtime loads exactly three Website Builder Hermes skills, each a
directory containing a SKILL.md with valid frontmatter. Obsolete flat
placeholder markdown files must not exist. UI UX Pro Max is reused from the
Website Hermes profile, never vendored here.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


SKILLS_DIR = Path(__file__).parent.parent / "skills"

REQUIRED_SKILLS = [
    "website-builder-environment",
    "website-builder-product-scope",
    "website-builder-design-dna",
]

# Flat placeholder files from the old structure must be gone.
OBSOLETE_FLAT_FILES = [
    "environment.md",
    "product-scope.md",
    "design-dna.md",
    "ui-ux-pro-max.md",
    "browser-screenshot.md",
    "qa-rules.md",
    "git-vercel.md",
    "recovery.md",
]


def _read_frontmatter(skill_md: Path) -> dict:
    """Parse the YAML frontmatter block of a SKILL.md (name/description/etc.)."""
    text = skill_md.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}
    fields: dict = {}
    for line in match.group(1).splitlines():
        kv = re.match(r"^(\w+):\s*(.*)$", line)
        if kv:
            fields[kv.group(1)] = kv.group(2).strip()
    return fields


class TestSkills(unittest.TestCase):
    def test_skills_directory_exists(self):
        self.assertTrue(SKILLS_DIR.exists())
        self.assertTrue(SKILLS_DIR.is_dir())

    def test_required_skill_directories_exist(self):
        for skill in REQUIRED_SKILLS:
            skill_dir = SKILLS_DIR / skill
            self.assertTrue(skill_dir.is_dir(), f"Missing skill dir: {skill}")
            self.assertTrue(
                (skill_dir / "SKILL.md").is_file(),
                f"Missing SKILL.md in: {skill}",
            )

    def test_skill_frontmatter_names_match_directories(self):
        for skill in REQUIRED_SKILLS:
            fm = _read_frontmatter(SKILLS_DIR / skill / "SKILL.md")
            self.assertEqual(
                fm.get("name"),
                skill,
                f"{skill}/SKILL.md frontmatter name must match directory",
            )
            self.assertTrue(fm.get("description"), f"{skill} needs a description")

    def test_obsolete_flat_skill_files_removed(self):
        for flat in OBSOLETE_FLAT_FILES:
            self.assertFalse(
                (SKILLS_DIR / flat).exists(),
                f"Obsolete flat skill file still present: {flat}",
            )

    def test_no_flat_markdown_skills_at_top_level(self):
        """Only README.md may sit at the skills/ top level; skills are dirs."""
        for md in SKILLS_DIR.glob("*.md"):
            self.assertEqual(
                md.name,
                "README.md",
                f"Unexpected flat markdown skill file: {md.name}",
            )

    def test_environment_skill_content(self):
        content = (SKILLS_DIR / "website-builder-environment" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("workspace_root", content)
        self.assertIn("project_id", content)
        self.assertIn("MAX_WORKERS=1", content)
        self.assertIn("~/.hermes-website", content)
        self.assertIn("~/.hermes", content)  # must mention not to touch it

    def test_product_scope_skill_content(self):
        content = (SKILLS_DIR / "website-builder-product-scope" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Never invent business intent", content)
        self.assertIn("WEBSITE", content)
        self.assertIn("OUT_OF_SCOPE", content)

    def test_design_dna_skill_content(self):
        content = (SKILLS_DIR / "website-builder-design-dna" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("FRONTEND owns Design DNA", content)
        self.assertIn("palette", content)
        self.assertIn("typography", content)

    def test_ui_ux_pro_max_not_vendored(self):
        """UI UX Pro Max is reused from the Hermes profile, not duplicated."""
        skill_dirs = [d.name for d in SKILLS_DIR.iterdir() if d.is_dir()]
        self.assertNotIn("ui-ux-pro-max", skill_dirs)
        self.assertNotIn("ui_ux_pro_max", skill_dirs)
        self.assertFalse((SKILLS_DIR / "ui-ux-pro-max.md").exists())

    def test_no_omarchy_dependency(self):
        for skill_md in SKILLS_DIR.glob("*/SKILL.md"):
            content = skill_md.read_text(encoding="utf-8")
            self.assertNotIn(
                "install omarchy",
                content.lower(),
                f"{skill_md} should not require Omarchy installation",
            )


if __name__ == "__main__":
    unittest.main()
