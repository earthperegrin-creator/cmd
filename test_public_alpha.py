import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from http.server import SimpleHTTPRequestHandler
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import cmd_db
import server
from cmd_app import config, lifecycle, onboarding, settings, worker_contract
from scripts import (
    audit_public_foundation,
    audit_public_snapshot,
    build_public_snapshot,
    demo_workspace,
    validate_public_snapshot,
)


PROFILE = {
    "first_name": "Nick",
    "summary": "Builds products with coding agents.",
    "authorized_sources": ["~/notes"],
    "outcomes_90_days": ["Ship a useful product", "Interview ten users"],
}


class PublicAlphaTests(unittest.TestCase):
    def test_fresh_install_uses_external_state_but_preserves_legacy_dogfood(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "product"
            root.mkdir()
            with patch.dict(os.environ, {"CMD_HOME": str(Path(tmpdir) / "private")}, clear=False):
                self.assertEqual(config.state_dir(root), Path(tmpdir) / "private" / "state")
                (root / ".cmd").mkdir()
                self.assertEqual(config.state_dir(root), root / ".cmd")

    def test_connectors_and_workers_are_opt_in_for_fresh_settings(self):
        defaults = settings.default_settings("test-model")
        self.assertEqual(defaults["background_agent"], "none")
        self.assertFalse(defaults["gmail_ingestion_enabled"])
        self.assertFalse(defaults["email_triage_enabled"])
        self.assertEqual(defaults["worker_adapter"], "public")

    def test_onboarding_creates_private_profile_registries_policy_and_outcomes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state"
            result = onboarding.initialize_private_layer(state, PROFILE, agent="codex")
            profile = json.loads((state / "profile.json").read_text(encoding="utf-8"))
            saved_settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
            outcomes = cmd_db.list_outcomes(state / "cmd.db")

        self.assertTrue(result["ok"])
        self.assertEqual(profile["workspace_title"], "Nick in Command")
        self.assertEqual(saved_settings["background_agent"], "codex")
        self.assertFalse(saved_settings["gmail_ingestion_enabled"])
        self.assertEqual([item["title"] for item in outcomes], PROFILE["outcomes_90_days"])

    def test_setup_from_confirmed_profile_is_noninteractive_and_does_not_start_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_file = root / "profile.json"
            profile_file.write_text(json.dumps(PROFILE), encoding="utf-8")
            args = Namespace(
                state_dir=str(root / "state"), profile_json=str(profile_file), agent="none",
                no_start=True, no_open=True, host="127.0.0.1", port=8765,
            )
            result = lifecycle.setup(args)
            state_mode = (root / "state").stat().st_mode & 0o777

        self.assertTrue(result["ok"])
        self.assertEqual(result["outcomes_created"], 2)
        self.assertEqual(state_mode, 0o700)

    def test_backup_restore_round_trip_and_restore_rejects_nonempty_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            onboarding.initialize_private_layer(state, PROFILE)
            lifecycle.write_json(state / "runtime.json", {"pid": 123})
            archive = Path(lifecycle.backup(state, str(root / "archives"))["archive"])
            second_archive = Path(lifecycle.backup(state, str(root / "archives"))["archive"])
            with tarfile.open(archive, "r:gz") as bundle:
                members = set(bundle.getnames())
            restored = root / "restored"
            result = lifecycle.restore(restored, str(archive))
            with self.assertRaises(ValueError):
                lifecycle.restore(restored, str(archive))
            self.assertTrue(result["ok"])
            self.assertTrue((restored / "profile.json").exists())
            self.assertTrue((restored / "cmd.db").exists())
            self.assertNotIn("runtime.json", members)
            self.assertNotEqual(archive, second_archive)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_uninstall_preserves_data_unless_purge_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state"
            onboarding.initialize_private_layer(state, PROFILE)
            preserved = lifecycle.uninstall(state)
            self.assertTrue((state / "cmd.db").exists())
            self.assertIn("preserved", preserved["data"])
            purged = lifecycle.uninstall(state, purge_data=True)
            self.assertEqual(purged["data"], "deleted")
            self.assertFalse(state.exists())
        with self.assertRaisesRegex(ValueError, "broad directory"):
            lifecycle.uninstall(Path.home(), purge_data=True)

    def test_stop_ignores_a_stale_reused_pid_instead_of_signaling_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state"
            state.mkdir()
            lifecycle.write_json(state / "runtime.json", {"pid": 4242})
            with patch("cmd_app.lifecycle.pid_is_alive", return_value=True), patch(
                "cmd_app.lifecycle.process_matches_server", return_value=False
            ), patch("cmd_app.lifecycle.os.kill") as kill:
                result = lifecycle.stop_service(state)
        self.assertEqual(result["status"], "stale_runtime")
        kill.assert_not_called()

    def test_stop_accepts_a_verified_server_from_the_recorded_pre_move_checkout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "state"
            old_root = Path(tmpdir) / "old-checkout"
            state.mkdir()
            lifecycle.write_json(state / "runtime.json", {
                "pid": 4242,
                "root": str(old_root),
                "state_dir": str(state),
            })
            with patch("cmd_app.lifecycle.pid_is_alive", side_effect=[True, False, False]), patch(
                "cmd_app.lifecycle.process_matches_server", return_value=True
            ) as matches, patch("cmd_app.lifecycle.os.kill") as kill:
                result = lifecycle.stop_service(state)
        self.assertEqual(result["status"], "stopped")
        matches.assert_called_once_with(4242, old_root)
        kill.assert_called_once()

    def test_update_refuses_a_dirty_checkout_before_backing_up_or_pulling(self):
        dirty = subprocess.CompletedProcess(["git"], 0, stdout=" M local-change\n", stderr="")
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "cmd_app.lifecycle.subprocess.run", return_value=dirty
        ), patch("cmd_app.lifecycle.backup") as backup:
            with self.assertRaisesRegex(ValueError, "local changes"):
                lifecycle.update_product(Path(tmpdir))
        backup.assert_not_called()

    def test_server_host_and_origin_guards(self):
        self.assertTrue(server.request_host_allowed("127.0.0.1:8765"))
        self.assertTrue(server.request_host_allowed("localhost:8765"))
        self.assertFalse(server.request_host_allowed("example.com"))
        self.assertTrue(server.request_origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765"))
        self.assertFalse(server.request_origin_allowed("https://evil.example", "127.0.0.1:8765"))
        self.assertIsNot(server.CommandHandler.do_HEAD, SimpleHTTPRequestHandler.do_HEAD)

    def test_demo_workspace_is_isolated_sanitized_and_reset_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / ".cmd-demo-test"
            result = demo_workspace.create_demo_workspace(state)
            saved_settings = json.loads((state / "settings.json").read_text(encoding="utf-8"))
            queue = cmd_db.list_agent_queue(state / "cmd.db")
            with cmd_db.connect(state / "cmd.db") as conn:
                roots = conn.execute(
                    "SELECT category, urgency FROM work_items WHERE parent_item_id IS NULL"
                ).fetchall()
                linked_action_count = conn.execute(
                    "SELECT COUNT(*) FROM actions WHERE item_id IS NOT NULL"
                ).fetchone()[0]
            demo_workspace.reset_demo_workspace(state)

        self.assertTrue(result["created"])
        self.assertEqual(saved_settings["background_agent"], "none")
        self.assertFalse(saved_settings["gmail_ingestion_enabled"])
        self.assertEqual(
            {item["state"] for item in queue},
            {"approval", "awaiting_human", "working", "blocked"},
        )
        self.assertEqual(linked_action_count, 4)
        self.assertEqual(
            {row["category"] for row in roots},
            {"work", "building", "writing", "personal"},
        )
        self.assertEqual({row["urgency"] for row in roots}, {"red", "yellow", "low"})
        self.assertFalse(state.exists())
        with self.assertRaises(ValueError):
            demo_workspace.reset_demo_workspace(Path.home())

    def test_public_foundation_has_no_private_coupling(self):
        self.assertEqual(audit_public_foundation.violations(), [])

    def test_public_snapshot_is_allowlisted_complete_and_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = Path(tmpdir) / "public"
            receipt = build_public_snapshot.build_snapshot(snapshot)
            problems = audit_public_snapshot.audit(snapshot)
        self.assertTrue(receipt["ok"])
        self.assertGreater(receipt["file_count"], 50)
        self.assertEqual(problems, [])

    def test_public_validation_staging_excludes_existing_git_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source"
            destination = root / "staged"
            source.mkdir()
            (source / "README.md").write_text("CMD\n", encoding="utf-8")
            (source / ".git").mkdir()
            (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            validate_public_snapshot.stage_snapshot(source, destination)
            self.assertTrue((destination / "README.md").exists())
            self.assertFalse((destination / ".git").exists())

    def test_generic_worker_contract_accepts_one_typed_receipt_per_action(self):
        envelope = {
            "dispatch": {"id": "dispatch-test"},
            "actions": [{"id": "act-1"}],
        }
        raw = json.dumps({
            "schema_version": 1,
            "dispatch_id": "dispatch-test",
            "receipts": [{
                "action_id": "act-1",
                "status": "completed",
                "summary": "Research memo is ready.",
                "artifact": {"type": "document", "content": "Recommendation."},
            }],
        })
        receipts = worker_contract.parse_worker_output(raw, envelope, "test-model")
        self.assertEqual(receipts[0]["model"], "test-model")
        self.assertEqual(receipts[0]["status"], "completed")

    def test_generic_worker_contract_accepts_one_fenced_object_after_provider_note(self):
        envelope = {
            "dispatch": {"id": "dispatch-1"},
            "actions": [{"id": "action-1"}],
        }
        raw = 'Provider note.\n```json\n{"schema_version":1,"dispatch_id":"dispatch-1","receipts":[{"action_id":"action-1","status":"completed","summary":"Done"}]}\n```'
        receipts = worker_contract.parse_worker_output(raw, envelope)
        self.assertEqual(receipts[0]["status"], "completed")

    def test_generic_worker_contract_rejects_ambiguous_fenced_objects(self):
        envelope = {
            "dispatch": {"id": "dispatch-1"},
            "actions": [{"id": "action-1"}],
        }
        raw = '```json\n{}\n```\n```json\n{}\n```'
        with self.assertRaisesRegex(ValueError, "exactly one JSON object"):
            worker_contract.parse_worker_output(raw, envelope)

    def test_generic_worker_contract_rejects_approval_without_exact_payload(self):
        envelope = {"dispatch": {"id": "dispatch-test"}, "actions": [{"id": "act-1"}]}
        raw = json.dumps({
            "schema_version": 1,
            "dispatch_id": "dispatch-test",
            "receipts": [{"action_id": "act-1", "status": "awaiting_approval", "summary": "Ready."}],
        })
        with self.assertRaises(ValueError):
            worker_contract.parse_worker_output(raw, envelope)

    def test_fresh_background_agent_uses_generic_adapter_but_legacy_settings_remain_private(self):
        fresh = settings.default_settings("test-model")
        fresh["background_agent"] = "codex"
        self.assertIn("scripts/public_worker.py", settings.background_agent_command(fresh))
        self.assertIn("scripts/codex_worker.py", settings.background_agent_command({"background_agent": "codex"}))


if __name__ == "__main__":
    unittest.main()
