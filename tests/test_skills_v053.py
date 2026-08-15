from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / ".pi" / "skills"


def read_skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise AssertionError("skill must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise AssertionError("skill frontmatter is not terminated")
    out: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise AssertionError(f"invalid frontmatter line: {line!r}")
        out[key.strip()] = value.strip()
    return out


class SkillContractTests(unittest.TestCase):
    expected = {
        "nrg-study-analysis",
        "nrg-campaign-design",
        "nrg-0d-ignition-analysis",
        "nrg-plot-data",
    }

    def test_expected_project_skills_exist(self) -> None:
        found = {
            p.parent.name
            for p in SKILLS.glob("*/SKILL.md")
            if p.is_file()
        }
        self.assertTrue(self.expected.issubset(found), (self.expected, found))

    def test_skill_frontmatter_names_match_directories(self) -> None:
        descriptions: set[str] = set()
        for name in sorted(self.expected):
            meta = frontmatter(read_skill(name))
            self.assertEqual(meta.get("name"), name)
            description = meta.get("description", "")
            self.assertGreater(len(description), 40)
            self.assertNotIn(description, descriptions)
            descriptions.add(description)

    def test_study_analysis_encodes_results_before_interpretation(self) -> None:
        text = read_skill("nrg-study-analysis")
        for token in (
            "per-case structured results",
            "nrg_analysis",
            "nrg_run_study",
            "nrg_read_study_summary",
            "nrg-plot-data",
            "Do not modify raw CFD results",
        ):
            self.assertIn(token, text)
        self.assertRegex(text, re.compile(r"raw case histories.*per-case structured results.*interpretation", re.S))

    def test_campaign_design_encodes_identity_semantics(self) -> None:
        text = read_skill("nrg-campaign-design")
        for token in (
            "Logical identity axes",
            "Campaign constants",
            "Attempt-tunable parameters",
            "nrg_campaign_append_preview",
            "convergence campaign",
            "explicit confirmation",
        ):
            self.assertIn(token, text)
        self.assertIn("same parameter can belong to different classes in different campaigns", text)

    def test_ignition_skill_preserves_scientific_semantics(self) -> None:
        text = read_skill("nrg-0d-ignition-analysis")
        for token in (
            "tau_dTdt_s",
            "tau_dpdt_s",
            "tau_Tplus400_s",
            "0d_cv_post_ignition_quasistationary_v1",
            "post-ignition quasistationary product state",
            "mass-fraction closure",
            "rate-of-production",
            "nrg-plot-data",
        ):
            self.assertIn(token, text)
        self.assertIn("not thermodynamic equilibrium", text)
        self.assertIn("this campaign alone does not establish", text.lower())

    def test_skills_do_not_override_global_policy(self) -> None:
        joined = "\n".join(read_skill(name) for name in sorted(self.expected))
        forbidden = (
            "ignore AGENTS.md",
            "you may bypass trusted",
            "modify raw case data",
            "skip user confirmation",
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, joined)


if __name__ == "__main__":
    unittest.main()
