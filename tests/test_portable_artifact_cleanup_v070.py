"""Portable transport-artifact cleanup regression tests (v0.7.0 core).

Covers managed/legacy portable root discovery, fail-closed deletion safety
classification, path-boundary containment, delete-time TOCTOU revalidation,
and the import lifecycle (default export location, receipt-before-cleanup,
automatic reclamation, ``keep_portable_artifacts``, and cleanup-failure
isolation from imported scientific data).

All tests use temporary directories and fake laboratory objects only.  No real
CFD, external NRG, network, Windows, or large datasets are required.
``unittest.mock`` is used only for deterministic state-change/failure injection
(tests 29 and 39); provenance and TOCTOU tests favor real temporary filesystem
state.
"""

import csv
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from campaign_tools import portable_bundle as pb
from campaign_tools import portable_executor as pe
from nrg_analysis.provenance import sha256_file


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeLab:
    pass


# ---------------------------------------------------------------------------
# Lab/source helpers
# ---------------------------------------------------------------------------


def make_minimal_lab(root: Path):
    """A lab with only the roots needed for cleanup classification tests."""
    lab = FakeLab()
    lab.research_root = root / "repo"
    lab.research_root.mkdir(parents=True, exist_ok=True)
    lab.campaign_root = lab.research_root / "campaigns"
    lab.campaign_root.mkdir(parents=True, exist_ok=True)
    lab.runs_root = lab.research_root / "runs"
    lab.runs_root.mkdir(parents=True, exist_ok=True)
    return lab


def make_full_source(root: Path):
    """A lab + prepared campaign usable by export_bundle/import_results."""
    lab = FakeLab()
    lab.research_root = root / "repo"
    (lab.research_root / "campaign_tools").mkdir(parents=True)
    shutil.copy2(
        Path(pb.__file__).with_name("portable_executor.py"),
        lab.research_root / "campaign_tools" / "portable_executor.py",
    )
    source_root = Path(pb.__file__).resolve().parents[1]
    (lab.research_root / "nrg_analysis").mkdir(parents=True)
    shutil.copy2(
        source_root / "nrg_analysis" / "quasistationary_core.py",
        lab.research_root / "nrg_analysis" / "quasistationary_core.py",
    )
    lab.termination_profiles = (
        lab.research_root / "config" / "termination_profiles.json"
    )
    lab.termination_profiles.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        source_root / "config" / "termination_profiles.json",
        lab.termination_profiles,
    )
    lab.campaign_root = lab.research_root / "campaigns"
    lab.campaign_root.mkdir()
    lab.runs_root = lab.research_root / "runs"
    lab.runs_root.mkdir()
    lab.config_path = lab.research_root / "config" / "laboratory.toml"
    lab.config_path.parent.mkdir(parents=True, exist_ok=True)
    lab.config_path.write_text("# fake\n", encoding="utf-8")
    lab.local_config_path = None
    lab.computing_module = lab.research_root / "bin" / "computing_module"
    lab.computing_module.parent.mkdir()
    lab.computing_module.write_text("fake binary placeholder", encoding="utf-8")
    lab.runtime_manifest = lab.research_root / "bin" / "runtime_manifest.json"
    lab.runtime_manifest.write_text("{}\n", encoding="utf-8")

    camp = lab.campaign_root / "camp"
    (camp / "_setups").mkdir(parents=True)
    (camp / "campaign.toml").write_text('[campaign]\nname="camp"\n', encoding="utf-8")
    rows = []
    for number in (1, 2):
        cid = f"R{number:06d}"
        rel = f"camp/{cid}"
        cp = lab.runs_root / rel
        (cp / "task_setup").mkdir(parents=True)
        (cp / "data_output").mkdir()
        (cp / "setup_input.nml").write_text(
            f"&case_config\n case_fingerprint='{cid}-fp'\n/\n", encoding="utf-8"
        )
        (cp / "task_setup" / "solver_setup.inf").write_text("setup\n", encoding="utf-8")
        (cp / "data_output" / "000.dat").write_text("initial\n", encoding="utf-8")
        (camp / "_setups" / f"{cid}.nml").write_text("nml\n", encoding="utf-8")
        rows.append(
            {
                "case_id": cid,
                "case_fingerprint": f"{cid}-fp",
                "case_identity_fingerprint": f"{cid}-fp",
                "generated_attempt_fingerprint": f"{cid}-att",
                "case_path": str(cp),
                "data_save_path": str(cp / "data_save"),
                "data_output_path": str(cp / "data_output"),
            }
        )
    with (camp / "cases.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (camp / "campaign_manifest.json").write_text(
        json.dumps({"generation_runs_root": str(lab.runs_root)}), encoding="utf-8"
    )
    return lab, camp / "cases.csv"


# ---------------------------------------------------------------------------
# Artifact construction helpers
# ---------------------------------------------------------------------------


def make_minimal_results_zip(path: Path, bundle_id: str, contract_sha: str = "contract123") -> Path:
    """A minimal valid returned results ZIP with a results_manifest bundle_id."""
    import io

    result_manifest = {
        "results_schema_version": pb.RESULTS_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "campaign_contract_sha256": contract_sha,
        "cases": {},
    }
    bundle_manifest = {
        "bundle_id": bundle_id,
        "campaign_contract_sha256": contract_sha,
    }
    with zipfile_open(path) as zf:
        zf.writestr("results_manifest.json", json.dumps(result_manifest))
        zf.writestr("bundle_manifest.json", json.dumps(bundle_manifest))
        zf.writestr("cases.csv", "case_id,case_fingerprint\n")
    return path


def zipfile_open(path: Path):
    import zipfile

    return zipfile.ZipFile(path, "w")


def make_minimal_export(
    path: Path,
    bundle_id: str,
    contract_sha: str,
    created_at: str = "2026-01-01T00:00:00+00:00",
    with_lock: bool = False,
) -> Path:
    """A minimal expanded export directory with a bundle_manifest.json."""
    path.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle_id": bundle_id,
        "campaign_contract_sha256": contract_sha,
        "created_at_utc": created_at,
    }
    (path / "bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (path / "runs").mkdir(exist_ok=True)
    (path / "runs" / "payload.dat").write_text("x" * 500, encoding="utf-8")
    if with_lock:
        lock = path / ".executor.lock"
        lock.mkdir(exist_ok=True)
        (lock / "owner.json").write_text(json.dumps({"owner": "test"}), encoding="utf-8")
    return path


def write_receipt(
    campaign_dir: Path,
    bundle_id: str,
    contract_sha: str,
    archive_path: Path,
    imported_at: str = "2026-06-01T00:00:00+00:00",
    hash_override: str | None = None,
    empty_bid: bool = False,
) -> dict:
    """Write a durable _imports receipt (proof of successful import)."""
    imports = campaign_dir / "_imports"
    imports.mkdir(parents=True, exist_ok=True)
    h = hash_override if hash_override is not None else sha256_file(archive_path)
    rec = {
        "imported_at_utc": imported_at,
        "results_archive": str(archive_path.resolve()),
        "results_archive_sha256": h,
        "bundle_id": "" if empty_bid else bundle_id,
        "campaign_contract_sha256": contract_sha,
        "imported_case_ids": ["R000001"],
        "import_status": "success",
    }
    (imports / "receipt.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def fill_bundle_results(bundle_dir: Path, bundle_id: str) -> None:
    """Simulate remote execution inside an exported bundle."""
    for cid in ("R000001", "R000002"):
        cp = bundle_dir / "runs" / "camp" / cid
        (cp / "front_history.dat").write_text("1 2 3\n", encoding="utf-8")
        (cp / "run_status.json").write_text(
            json.dumps(
                {
                    "status": "finished",
                    "bundle_id": bundle_id,
                    "execution_origin": "portable_bundle_v1",
                    "nrg_termination_reason": "final_simulation_time",
                }
            ),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Classification tests (1-20)
# ---------------------------------------------------------------------------


class ClassificationTests(unittest.TestCase):
    BID = "bidAAA1234567890abcd"
    CONTRACT = "contract123"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.lab = make_minimal_lab(root)
        self.campaign = self.lab.campaign_root / "camp"
        self.campaign.mkdir(parents=True, exist_ok=True)
        # Ensure managed/legacy roots exist.
        pb.managed_exports_root(self.lab).mkdir(parents=True, exist_ok=True)
        pb.managed_returns_root(self.lab).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def _receipts(self):
        return pb._discover_import_receipts(self.lab)

    def test_01_managed_return_exact_proof_deletable(self):
        zip_path = pb.managed_returns_root(self.lab) / "results.zip"
        make_minimal_results_zip(zip_path, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, zip_path)
        cls = pb._classify_returned_archive(zip_path, self.lab, self._receipts())
        self.assertTrue(cls["can_delete"], cls)
        self.assertEqual(cls["classification"], "safe_imported")
        self.assertEqual(cls["bundle_id"], self.BID)

    def test_02_missing_unreadable_archive_bundle_id_not_deletable(self):
        zip_path = pb.managed_returns_root(self.lab) / "results.zip"
        zip_path.write_bytes(b"not a zip at all")
        write_receipt(self.campaign, self.BID, self.CONTRACT, zip_path)
        cls = pb._classify_returned_archive(zip_path, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "unreadable_or_missing_identity")

    def test_03_missing_receipt_bundle_id_not_deletable(self):
        zip_path = pb.managed_returns_root(self.lab) / "results.zip"
        make_minimal_results_zip(zip_path, self.BID, self.CONTRACT)
        write_receipt(self.campaign, "", self.CONTRACT, zip_path, empty_bid=True)
        cls = pb._classify_returned_archive(zip_path, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "no_receipt")

    def test_04_bundle_id_mismatch_not_deletable(self):
        zip_path = pb.managed_returns_root(self.lab) / "results.zip"
        make_minimal_results_zip(zip_path, self.BID, self.CONTRACT)
        write_receipt(self.campaign, "DIFFERENTBID00000", self.CONTRACT, zip_path)
        cls = pb._classify_returned_archive(zip_path, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "no_receipt")

    def test_05_archive_hash_mismatch_not_deletable(self):
        zip_path = pb.managed_returns_root(self.lab) / "results.zip"
        make_minimal_results_zip(zip_path, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, zip_path, hash_override="0" * 64)
        cls = pb._classify_returned_archive(zip_path, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "hash_mismatch")

    def test_06_malformed_zip_not_deletable(self):
        zip_path = pb.managed_returns_root(self.lab) / "results.zip"
        # Truncated/garbage bytes that are not a valid ZIP.
        zip_path.write_bytes(b"\x50\x4b\x05\x06" + b"\x00" * 18)
        write_receipt(self.campaign, self.BID, self.CONTRACT, zip_path)
        cls = pb._classify_returned_archive(zip_path, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "unreadable_or_missing_identity")

    def test_07_external_zip_not_deletable(self):
        external = self.lab.research_root / "external.zip"
        make_minimal_results_zip(external, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, external)
        cls = pb._classify_returned_archive(external, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "external")

    def test_08_imported_export_complete_proof_deletable(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT, created_at="2026-01-01T00:00:00+00:00")
        # Receipt imported after export creation.
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip, imported_at="2026-06-01T00:00:00+00:00")
        cls = pb._classify_export(exp, self.lab, self._receipts())
        self.assertTrue(cls["can_delete"], cls)
        self.assertEqual(cls["classification"], "safe_imported")

    def test_09_export_missing_bundle_id_not_deletable(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, "", self.CONTRACT)
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip)
        cls = pb._classify_export(exp, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])

    def test_10_export_missing_contract_hash_not_deletable(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, "")
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip)
        cls = pb._classify_export(exp, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])

    def test_11_valid_unimported_export_not_deletable_by_default(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT)
        cls = pb._classify_export(exp, self.lab, self._receipts(), include_unimported=False)
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "no_receipt")

    def test_12_unimported_export_deletable_in_advanced_mode(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT)
        cls = pb._classify_export(exp, self.lab, self._receipts(), include_unimported=True)
        self.assertTrue(cls["can_delete"])
        self.assertEqual(cls["classification"], "unimported_export_abandoned")

    def test_13_incomplete_identity_not_deletable_in_advanced_mode(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, "", self.CONTRACT)
        cls = pb._classify_export(exp, self.lab, self._receipts(), include_unimported=True)
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "incomplete_identity")

    def test_14_executor_lock_not_deletable(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT, with_lock=True)
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip)
        cls = pb._classify_export(exp, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "locked")

    def test_15_export_created_after_import_not_deletable(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        # Export created AFTER the import.
        make_minimal_export(exp, self.BID, self.CONTRACT, created_at="2026-12-01T00:00:00+00:00")
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip, imported_at="2026-06-01T00:00:00+00:00")
        cls = pb._classify_export(exp, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "temporal_mismatch")

    def test_16_malformed_timestamps_not_deletable(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT, created_at="not-a-timestamp")
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip, imported_at="also-bad")
        cls = pb._classify_export(exp, self.lab, self._receipts())
        self.assertFalse(cls["can_delete"])
        self.assertEqual(cls["classification"], "temporal_mismatch")

    def test_17_managed_and_legacy_roots_discovered(self):
        legacy_exp = pb.legacy_exports_root(self.lab) / "legacy__bid"
        make_minimal_export(legacy_exp, self.BID, self.CONTRACT)
        legacy_ret = pb.legacy_returns_root(self.lab)
        legacy_ret.mkdir(parents=True, exist_ok=True)
        legacy_zip = legacy_ret / "legacy.zip"
        make_minimal_results_zip(legacy_zip, self.BID, self.CONTRACT)
        managed_exp = pb.managed_exports_root(self.lab) / "m__bid"
        make_minimal_export(managed_exp, self.BID, self.CONTRACT)
        managed_zip = pb.managed_returns_root(self.lab) / "m.zip"
        make_minimal_results_zip(managed_zip, self.BID, self.CONTRACT)
        plan = pb.cleanup_plan(self.lab)
        paths = {a["path"] for a in plan["artifacts"]}
        self.assertIn(str(legacy_exp), paths)
        self.assertIn(str(legacy_zip), paths)
        self.assertIn(str(managed_exp), paths)
        self.assertIn(str(managed_zip), paths)
        root_types = {a["root_type"] for a in plan["artifacts"]}
        self.assertIn("legacy_exports", root_types)
        self.assertIn("legacy_returns", root_types)
        self.assertIn("managed_exports", root_types)
        self.assertIn("managed_returns", root_types)

    def test_18_recursive_byte_accounting_correct(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT)
        expected = sum(p.stat().st_size for p in exp.rglob("*") if p.is_file())
        self.assertEqual(pb._recursive_bytes(exp), expected)
        # A single file.
        f = pb.managed_returns_root(self.lab) / "f.zip"
        f.write_bytes(b"abc")
        self.assertEqual(pb._recursive_bytes(f), 3)
        # Missing path.
        self.assertEqual(pb._recursive_bytes(pb.managed_returns_root(self.lab) / "nope"), 0)

    def test_19_deterministic_ordering(self):
        # Create multiple artifacts; ordering must be deterministic by (kind, path).
        for name in ("b__bid", "a__bid", "c__bid"):
            make_minimal_export(pb.managed_exports_root(self.lab) / name, self.BID, self.CONTRACT)
        for name in ("z.zip", "m.zip", "a.zip"):
            make_minimal_results_zip(pb.managed_returns_root(self.lab) / name, self.BID, self.CONTRACT)
        plan = pb.cleanup_plan(self.lab)
        keys = [(a["kind"], a["path"]) for a in plan["artifacts"]]
        self.assertEqual(keys, sorted(keys))

    def test_20_runs_never_a_cleanup_candidate(self):
        # Place a stray file inside runs_root; it must never appear as a candidate.
        (self.lab.runs_root / "stray.zip").write_bytes(b"abc")
        (self.lab.runs_root / "camp").mkdir(parents=True, exist_ok=True)
        (self.lab.runs_root / "camp" / "bundle_manifest.json").write_text(
            json.dumps({"bundle_id": self.BID, "campaign_contract_sha256": self.CONTRACT}), encoding="utf-8"
        )
        plan = pb.cleanup_plan(self.lab)
        for a in plan["artifacts"]:
            self.assertNotIn(str(self.lab.runs_root), a["path"])


# ---------------------------------------------------------------------------
# Path-boundary tests (21-24)
# ---------------------------------------------------------------------------


class PathBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.lab = make_minimal_lab(Path(self._td.name))

    def tearDown(self):
        self._td.cleanup()

    def test_21_ordinary_descendant_accepted(self):
        root = pb.managed_returns_root(self.lab)
        root.mkdir(parents=True, exist_ok=True)
        child = root / "results.zip"
        child.write_bytes(b"x")
        self.assertTrue(pb._strictly_inside(child, root))

    def test_22_managed_root_itself_not_deletable(self):
        root = pb.managed_returns_root(self.lab)
        root.mkdir(parents=True, exist_ok=True)
        self.assertFalse(pb._strictly_inside(root, root))

    def test_23_traversal_external_path_rejected(self):
        root = pb.managed_returns_root(self.lab)
        root.mkdir(parents=True, exist_ok=True)
        external = self.lab.research_root / "outside.zip"
        external.write_bytes(b"x")
        # A path constructed with .. that resolves outside the root.
        traversal = root / ".." / ".." / "outside.zip"
        self.assertFalse(pb._strictly_inside(traversal, root))
        self.assertFalse(pb._strictly_inside(external, root))

    def test_24_symlink_escape_rejected(self):
        root = pb.managed_returns_root(self.lab)
        root.mkdir(parents=True, exist_ok=True)
        target = self.lab.research_root / "escape-target.zip"
        target.write_bytes(b"x")
        link = root / "link.zip"
        try:
            os.symlink(target, link)
        except OSError:
            self.skipTest("symlinks not supported on this platform")
        # The symlink resolves outside the root -> rejected.
        self.assertFalse(pb._strictly_inside(link, root))


# ---------------------------------------------------------------------------
# TOCTOU / delete-time revalidation tests (25-29)
# ---------------------------------------------------------------------------


class ToctouRevalidationTests(unittest.TestCase):
    BID = "bidBBB1234567890abcd"
    CONTRACT = "contract456"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.lab = make_minimal_lab(root)
        self.campaign = self.lab.campaign_root / "camp"
        self.campaign.mkdir(parents=True, exist_ok=True)
        pb.managed_exports_root(self.lab).mkdir(parents=True, exist_ok=True)
        pb.managed_returns_root(self.lab).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def _receipts(self):
        return pb._discover_import_receipts(self.lab)

    def test_25_plan_safe_zip_modified_before_execute_not_deleted(self):
        zip_path = pb.managed_returns_root(self.lab) / "results.zip"
        make_minimal_results_zip(zip_path, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, zip_path)
        plan = pb.cleanup_plan(self.lab)
        self.assertTrue(any(a["path"] == str(zip_path) and a["can_delete"] for a in plan["artifacts"]))
        # Mutate the ZIP after the plan; hash now differs from the receipt.
        make_minimal_results_zip(zip_path, self.BID, self.CONTRACT + "X")
        result = pb.cleanup_execute(self.lab)
        self.assertTrue(zip_path.exists())
        self.assertEqual(result["deleted_count"], 0)

    def test_26_plan_safe_export_manifest_identity_changes_not_deleted(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT, created_at="2026-01-01T00:00:00+00:00")
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip, imported_at="2026-06-01T00:00:00+00:00")
        plan = pb.cleanup_plan(self.lab)
        self.assertTrue(any(a["path"] == str(exp) and a["can_delete"] for a in plan["artifacts"]))
        # Change the manifest identity after the plan.
        make_minimal_export(exp, "DIFFERENTBID00001", self.CONTRACT, created_at="2026-01-01T00:00:00+00:00")
        result = pb.cleanup_execute(self.lab)
        self.assertTrue(exp.exists())
        self.assertEqual(result["deleted_count"], 0)

    def test_27_plan_safe_export_lock_appears_not_deleted(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT, created_at="2026-01-01T00:00:00+00:00")
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip, imported_at="2026-06-01T00:00:00+00:00")
        plan = pb.cleanup_plan(self.lab)
        self.assertTrue(any(a["path"] == str(exp) and a["can_delete"] for a in plan["artifacts"]))
        # A lock appears after the plan.
        lock = exp / ".executor.lock"
        lock.mkdir(exist_ok=True)
        (lock / "owner.json").write_text("{}", encoding="utf-8")
        result = pb.cleanup_execute(self.lab)
        self.assertTrue(exp.exists())
        self.assertEqual(result["deleted_count"], 0)

    def test_28_artifact_becomes_symlink_escape_external_target_untouched(self):
        exp = pb.managed_exports_root(self.lab) / "camp__bid"
        make_minimal_export(exp, self.BID, self.CONTRACT, created_at="2026-01-01T00:00:00+00:00")
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip, imported_at="2026-06-01T00:00:00+00:00")
        plan = pb.cleanup_plan(self.lab)
        self.assertTrue(any(a["path"] == str(exp) and a["can_delete"] for a in plan["artifacts"]))
        # Replace the export dir with a symlink pointing at an external victim.
        shutil.rmtree(exp)
        victim = self.lab.research_root / "victim_dir"
        victim.mkdir()
        (victim / "precious.dat").write_text("do-not-delete", encoding="utf-8")
        try:
            os.symlink(victim, exp)
        except OSError:
            self.skipTest("symlinks not supported on this platform")
        result = pb.cleanup_execute(self.lab)
        # The external victim must remain untouched.
        self.assertTrue((victim / "precious.dat").exists())
        self.assertEqual((victim / "precious.dat").read_text(), "do-not-delete")
        self.assertEqual(result["deleted_count"], 0)

    def test_29_execute_deletes_only_still_safe_and_reports_revalidation_failures(self):
        # One export stays safe; a second export becomes locked between plan and
        # per-artifact revalidation (deterministic state-change injection).
        exp_safe = pb.managed_exports_root(self.lab) / "a__safe"
        exp_flipped = pb.managed_exports_root(self.lab) / "b__flipped"
        for exp in (exp_safe, exp_flipped):
            make_minimal_export(exp, self.BID, self.CONTRACT, created_at="2026-01-01T00:00:00+00:00")
        any_zip = self.lab.research_root / "any.zip"
        make_minimal_results_zip(any_zip, self.BID, self.CONTRACT)
        write_receipt(self.campaign, self.BID, self.CONTRACT, any_zip, imported_at="2026-06-01T00:00:00+00:00")

        real_classify = pb._classify_export
        call_counts: dict[str, int] = {}

        def flipping_classify(path, lab, receipts, include_unimported=False):
            key = str(path)
            call_counts[key] = call_counts.get(key, 0) + 1
            # The plan-time call is count==1 (no lock -> deletable).  Install a
            # lock on the revalidation call (count==2) so the per-artifact
            # reclassification sees it as locked and skips deletion.
            if key == str(exp_flipped) and call_counts[key] == 2:
                lock = exp_flipped / ".executor.lock"
                lock.mkdir(exist_ok=True)
                (lock / "owner.json").write_text("{}", encoding="utf-8")
            return real_classify(path, lab, receipts, include_unimported=include_unimported)

        with patch.object(pb, "_classify_export", side_effect=flipping_classify):
            result = pb.cleanup_execute(self.lab)
        deleted_paths = {d["path"] for d in result["deleted"]}
        self.assertIn(str(exp_safe), deleted_paths)
        self.assertNotIn(str(exp_flipped), deleted_paths)
        self.assertFalse(exp_safe.exists())
        self.assertTrue(exp_flipped.exists())
        # A revalidation failure must be reported for the flipped export.
        self.assertTrue(any(f.get("path") == str(exp_flipped) for f in result["failures"]))


# ---------------------------------------------------------------------------
# Import lifecycle integration tests (30-41)
# ---------------------------------------------------------------------------


class ImportLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        self.lab, self.cases = make_full_source(root)

    def tearDown(self):
        self._td.cleanup()

    def _export_default(self):
        result = pb.export_bundle(self.cases, self.lab, None)
        return Path(result["bundle"]), result["bundle_id"], result["campaign_contract_sha256"]

    def _export_explicit(self, out: Path):
        result = pb.export_bundle(self.cases, self.lab, out)
        return Path(result["bundle"]), result["bundle_id"], result["campaign_contract_sha256"]

    def _pack_to(self, bundle_dir: Path, bundle_id: str, archive: Path) -> Path:
        fill_bundle_results(bundle_dir, bundle_id)
        pe.pack_results(bundle_dir, archive)
        return archive

    def test_30_default_export_location_is_managed(self):
        bundle_dir, bundle_id, _ = self._export_default()
        managed = pb.managed_exports_root(self.lab)
        self.assertTrue(pb._strictly_inside(bundle_dir, managed))
        self.assertEqual(bundle_dir.name, f"{self.cases.parent.name}__{bundle_id}")

    def test_31_explicit_output_preserved(self):
        out = Path(self._td.name) / "explicit_bundle"
        bundle_dir, _, _ = self._export_explicit(out)
        self.assertEqual(bundle_dir.resolve(), out.resolve())

    def test_32_repeat_default_export_does_not_create_another_copy(self):
        bundle_dir, bundle_id, _ = self._export_default()
        # A second default export to the same deterministic destination must fail.
        with self.assertRaises((ValueError, FileExistsError)):
            pb.export_bundle(self.cases, self.lab, None)
        # No suffixed/timestamped sibling was created.
        siblings = [p for p in pb.managed_exports_root(self.lab).iterdir()]
        self.assertEqual(len(siblings), 1)
        self.assertEqual(siblings[0].resolve(), bundle_dir.resolve())

    def test_33_receipt_exists_before_cleanup(self):
        bundle_dir, bundle_id, _ = self._export_default()
        ret = pb.managed_returns_root(self.lab) / "results.zip"
        self._pack_to(bundle_dir, bundle_id, ret)
        pb.import_results(self.cases, ret, self.lab)
        # The durable receipt was written (and survived cleanup).
        receipts = list((self.cases.parent / "_imports").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        rec = json.loads(receipts[0].read_text())
        self.assertEqual(rec["import_status"], "success")
        self.assertEqual(rec["bundle_id"], bundle_id)

    def test_34_successful_import_deletes_matching_managed_return(self):
        bundle_dir, bundle_id, _ = self._export_default()
        ret = pb.managed_returns_root(self.lab) / "results.zip"
        self._pack_to(bundle_dir, bundle_id, ret)
        result = pb.import_results(self.cases, ret, self.lab)
        self.assertTrue(result["imported"])
        self.assertFalse(ret.exists())
        self.assertTrue(result["cleanup"]["deleted_count"] >= 1)
        kinds = {d["kind"] for d in result["cleanup"]["deleted"]}
        self.assertIn("returned_archive", kinds)

    def test_35_successful_import_deletes_matching_safe_managed_export(self):
        bundle_dir, bundle_id, _ = self._export_default()
        external = Path(self._td.name) / "external.zip"
        self._pack_to(bundle_dir, bundle_id, external)
        result = pb.import_results(self.cases, external, self.lab)
        self.assertTrue(result["imported"])
        # The managed export was reclaimed.
        self.assertFalse(bundle_dir.exists())
        kinds = {d["kind"] for d in result["cleanup"]["deleted"]}
        self.assertIn("expanded_export", kinds)

    def test_36_successful_import_retains_external_results_zip(self):
        bundle_dir, bundle_id, _ = self._export_default()
        external = Path(self._td.name) / "external.zip"
        self._pack_to(bundle_dir, bundle_id, external)
        pb.import_results(self.cases, external, self.lab)
        self.assertTrue(external.exists())

    def test_37_keep_portable_artifacts_retains_managed_artifacts(self):
        bundle_dir, bundle_id, _ = self._export_default()
        ret = pb.managed_returns_root(self.lab) / "results.zip"
        self._pack_to(bundle_dir, bundle_id, ret)
        result = pb.import_results(self.cases, ret, self.lab, keep_portable_artifacts=True)
        self.assertTrue(result["imported"])
        self.assertTrue(ret.exists())
        self.assertTrue(bundle_dir.exists())
        self.assertIsNone(result["cleanup"])

    def test_38_failed_import_retains_artifacts(self):
        bundle_dir, bundle_id, contract_sha = self._export_default()
        ret = pb.managed_returns_root(self.lab) / "results.zip"
        self._pack_to(bundle_dir, bundle_id, ret)
        # Corrupt the campaign contract so import fails: rewrite cases.csv
        # fingerprints so the contract hash no longer matches the results.
        rows = []
        with self.cases.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            for row in reader:
                row["case_fingerprint"] = row["case_fingerprint"] + "X"
                row["case_identity_fingerprint"] = row["case_identity_fingerprint"] + "X"
                rows.append(row)
        with self.cases.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaises(ValueError):
            pb.import_results(self.cases, ret, self.lab)
        # Nothing was deleted.
        self.assertTrue(ret.exists())
        self.assertTrue(bundle_dir.exists())

    def test_39_cleanup_failure_does_not_roll_back_imported_files(self):
        bundle_dir, bundle_id, _ = self._export_default()
        external = Path(self._td.name) / "external.zip"
        self._pack_to(bundle_dir, bundle_id, external)
        imported_file = self.lab.runs_root / "camp" / "R000001" / "front_history.dat"
        # Force only the managed export deletion to fail; the return archive is
        # external so only the managed export is a cleanup candidate.  The patch
        # is path-targeted so TemporaryDirectory staging cleanup still works.
        real_rmtree = shutil.rmtree
        bundle_resolved = bundle_dir.resolve()

        def failing_rmtree(path, *args, **kwargs):
            if Path(str(path)).resolve() == bundle_resolved:
                raise OSError("injected failure")
            return real_rmtree(path, *args, **kwargs)

        with patch.object(pb.shutil, "rmtree", side_effect=failing_rmtree):
            result = pb.import_results(self.cases, external, self.lab)
        self.assertTrue(result["imported"])
        # Imported scientific data remains intact.
        self.assertTrue(imported_file.exists())
        self.assertEqual(imported_file.read_text(), "1 2 3\n")
        # Cleanup failure is reported separately and the export was not removed.
        self.assertTrue(result["cleanup"]["failure_count"] >= 1)
        self.assertTrue(bundle_dir.exists())

    def test_40_no_second_durable_heavy_imported_campaign_tree(self):
        bundle_dir, bundle_id, _ = self._export_default()
        ret = pb.managed_returns_root(self.lab) / "results.zip"
        self._pack_to(bundle_dir, bundle_id, ret)
        pb.import_results(self.cases, ret, self.lab)
        # The only durable artifact under the campaign dir besides the original
        # generated files is the lightweight _imports receipt.
        camp = self.cases.parent
        new_entries = [p for p in camp.iterdir() if p.name not in {
            "campaign.toml", "cases.csv", "campaign_manifest.json", "_setups",
        }]
        self.assertEqual([p.name for p in new_entries], ["_imports"])
        # _imports contains only lightweight JSON.
        for p in (camp / "_imports").iterdir():
            self.assertEqual(p.suffix, ".json")
        # No legacy campaigns/exports/ tree was created.
        self.assertFalse((self.lab.campaign_root / "exports").exists())

    def test_41_preview_candidates_agree_with_cleanup_rules(self):
        bundle_dir, bundle_id, contract_sha = self._export_default()
        ret = pb.managed_returns_root(self.lab) / "results.zip"
        self._pack_to(bundle_dir, bundle_id, ret)
        plan = pb.import_plan(self.cases, ret, self.lab)
        preview = plan["post_import_cleanup"]
        self.assertTrue(preview["enabled"])
        self.assertTrue(preview["results_archive_managed"])
        # The export matches identity + has no lock -> exactly one candidate.
        self.assertEqual(preview["matching_export_candidate_count"], 1)
        # Candidates are explicitly not guaranteed deletions.
        self.assertIn("Candidates only", preview["note"])
        # Actual cleanup deletes the managed return + the managed export.
        result = pb.import_results(self.cases, ret, self.lab)
        self.assertEqual(result["cleanup"]["deleted_count"], 2)
        kinds = {d["kind"] for d in result["cleanup"]["deleted"]}
        self.assertEqual(kinds, {"returned_archive", "expanded_export"})


if __name__ == "__main__":
    unittest.main()