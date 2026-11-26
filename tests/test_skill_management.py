import io
import json
import shutil
import stat
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_manage.cli import main
from agent_manage.adapters.openclaw.local import LocalRunner
from agent_manage.models import AddSkillRequest
from agent_manage.adapters.openclaw.orchestrator import InstanceManagerV2
from agent_manage.response import TYPE_CODE_CONFLICT, TYPE_CODE_INVALID_ARGUMENT, TYPE_CODE_NOT_FOUND


class SkillManagementTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "custom-workspace"
        self.workspace.mkdir()
        (self.workspace / "SOUL.md").write_text("keep soul\n")
        self.config_path = self.root / "environment" / "openclaw.json"
        self.config_path.parent.mkdir()
        self.config_path.write_text(json.dumps({
            "agents": {"list": [{"id": "demo", "workspace": str(self.workspace)}]},
            "unchanged": True,
        }))
        self.config_before = self.config_path.read_bytes()
        self.source = self.root / "weather"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("---\nname: weather\n---\nWeather instructions\n")
        (self.source / "scripts").mkdir()
        (self.source / "scripts" / "run.sh").write_text("#!/bin/sh\necho weather\n")
        (self.source / "scripts" / "run.sh").chmod(0o755)
        self.manager = InstanceManagerV2(LocalRunner(), config_path=str(self.config_path))

    def test_agent_install_uses_configured_nonempty_workspace_and_keeps_other_files(self):
        result = self.manager.add_skill(AddSkillRequest(agent_name="demo", skill_dir=str(self.source)))
        destination = self.workspace / "skills" / "weather"
        self.assertEqual(result["scope"], "agent")
        self.assertEqual(result["destination"], str(destination))
        self.assertEqual((destination / "SKILL.md").read_bytes(), (self.source / "SKILL.md").read_bytes())
        self.assertTrue((destination / "scripts" / "run.sh").stat().st_mode & stat.S_IXUSR)
        self.assertEqual((self.workspace / "SOUL.md").read_text(), "keep soul\n")
        self.assertEqual(self.config_path.read_bytes(), self.config_before)
        self.assertFalse((self.config_path.parent / "skills").exists())
        self.assertFalse(result["activation_verified"])
        self.assertFalse(result["gateway_restarted"])

    def test_common_install_uses_custom_environment_and_does_not_touch_workspace(self):
        result = self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source)))
        self.assertEqual(result["scope"], "common")
        self.assertEqual(result["destination"], str(self.config_path.parent / "skills" / "weather"))
        self.assertFalse((self.workspace / "skills").exists())
        self.assertEqual(self.config_path.read_bytes(), self.config_before)
        self.assertTrue((self.source / "SKILL.md").is_file())

    def test_readonly_shared_skill_install_remains_updatable(self):
        self.source.chmod(0o555)
        (self.source / "SKILL.md").chmod(0o444)
        self.addCleanup(self.source.chmod, 0o755)
        for install in (
            lambda: self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source))),
            lambda: self.manager._install_common_skills([self.source]),
        ):
            install()
            destination = self.config_path.parent / "skills/weather"
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((destination / "SKILL.md").stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((destination / "scripts/run.sh").stat().st_mode), 0o755)
            self.assertEqual(destination.stat().st_uid, self.config_path.parent.stat().st_uid)
            with tempfile.NamedTemporaryFile(dir=destination):
                pass
            # Simulate an installation left by the previous release before
            # testing the provisioning merge path on the next iteration.
            destination.chmod(0o555)
            (destination / "SKILL.md").chmod(0o444)
        self.manager._install_common_skills([self.source])
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o555)

    def test_replace_legacy_readonly_shared_skill(self):
        self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source)))
        destination = self.config_path.parent / "skills/weather"
        (destination / "SKILL.md").chmod(0o444)
        (destination / "scripts").chmod(0o555)
        destination.chmod(0o555)
        self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source), replace=True))
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        self.assertFalse(list(destination.parent.glob(".*.install-*")))

    def test_existing_skill_requires_replace_and_replacement_removes_stale_files(self):
        request = AddSkillRequest(common=True, skill_dir=str(self.source))
        self.manager.add_skill(request)
        destination = self.config_path.parent / "skills" / "weather"
        (destination / "obsolete.txt").write_text("old")
        (self.source / "SKILL.md").write_text("new instructions\n")
        with self.assertRaises(FileExistsError):
            self.manager.add_skill(request)
        self.assertNotEqual((destination / "SKILL.md").read_text(), "new instructions\n")
        request.replace = True
        result = self.manager.add_skill(request)
        self.assertTrue(result["replaced"])
        self.assertFalse((destination / "obsolete.txt").exists())
        self.assertEqual((destination / "SKILL.md").read_text(), "new instructions\n")
        self.assertEqual([p.name for p in destination.parent.iterdir()], ["weather"])

    def test_failed_copy_or_swap_preserves_previous_skill(self):
        request = AddSkillRequest(common=True, skill_dir=str(self.source), replace=True)
        self.manager.add_skill(request)
        destination = self.config_path.parent / "skills" / "weather"
        original = (destination / "SKILL.md").read_bytes()
        (self.source / "SKILL.md").write_text("updated\n")
        real_replace = Path.replace

        def fail_install(path, target):
            if path.name == "skill":
                raise OSError("simulated swap failure")
            return real_replace(path, target)

        with patch("agent_manage.adapters.openclaw.skill_management.shutil.copytree", side_effect=OSError("copy failure")):
            with self.assertRaises(OSError):
                self.manager.add_skill(request)
        with patch.object(Path, "replace", fail_install):
            with self.assertRaises(OSError):
                self.manager.add_skill(request)
        self.assertEqual((destination / "SKILL.md").read_bytes(), original)
        self.assertEqual([p.name for p in destination.parent.iterdir()], ["weather"])

    def test_zip_supports_root_or_one_wrapper_directory(self):
        for prefix, expected_name in [("", "archive"), ("weather/", "weather")]:
            with self.subTest(prefix=prefix):
                archive_path = self.root / "archive.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(prefix + "SKILL.md", "zip instructions\n")
                    archive.writestr(prefix + "references/help.md", "help\n")
                    executable = zipfile.ZipInfo(prefix + "scripts/run.sh")
                    executable.create_system = 3
                    executable.external_attr = (stat.S_IFREG | 0o755) << 16
                    archive.writestr(executable, "#!/bin/sh\necho weather\n")
                result = self.manager.add_skill(AddSkillRequest(common=True, skill_zip=str(archive_path)))
                self.assertEqual(result["skill_name"], expected_name)
                self.assertEqual(result["source"], str(archive_path))
                destination = Path(result["destination"])
                self.assertEqual((destination / "references/help.md").read_text(), "help\n")
                self.assertTrue((destination / "scripts/run.sh").stat().st_mode & stat.S_IXUSR)

    def test_zip_rejects_traversal_links_and_multiple_skills(self):
        for kind in ["traversal", "link", "multiple"]:
            with self.subTest(kind=kind):
                archive_path = self.root / "unsafe.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("SKILL.md" if kind != "multiple" else "one/SKILL.md", "instructions")
                    if kind == "traversal":
                        archive.writestr("../escaped.txt", "escape")
                    elif kind == "link":
                        link = zipfile.ZipInfo("linked")
                        link.create_system = 3
                        link.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(link, "/tmp/outside")
                    else:
                        archive.writestr("two/SKILL.md", "two")
                with self.assertRaises(ValueError):
                    self.manager.add_skill(AddSkillRequest(common=True, skill_zip=str(archive_path)))
                self.assertFalse((self.config_path.parent / "skills").exists())
                self.assertFalse((self.root / "escaped.txt").exists())

    def test_invalid_sources_names_and_destination_links_are_rejected(self):
        (self.source / "linked").symlink_to(self.workspace / "SOUL.md")
        with self.assertRaises(ValueError):
            self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source)))
        (self.source / "linked").unlink()
        with self.assertRaises(ValueError):
            self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source), skill_name="../outside"))
        (self.source / "SKILL.md").unlink()
        with self.assertRaises(ValueError):
            self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source)))
        (self.source / "SKILL.md").write_text("instructions")
        outside = self.root / "outside"
        outside.mkdir()
        (self.config_path.parent / "skills").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager.add_skill(AddSkillRequest(common=True, skill_dir=str(self.source)))
        self.assertEqual(list(outside.iterdir()), [])

    def test_unknown_agent_or_missing_workspace_does_not_create_directories(self):
        with self.assertRaises(FileNotFoundError):
            self.manager.add_skill(AddSkillRequest(agent_name="missing", skill_dir=str(self.source)))
        shutil.rmtree(self.workspace)
        with self.assertRaises(FileNotFoundError):
            self.manager.add_skill(AddSkillRequest(agent_name="demo", skill_dir=str(self.source)))
        self.assertFalse(self.workspace.exists())

    def test_main_uses_configured_defaults_workspace(self):
        self.config_path.write_text(json.dumps({"agents": {"defaults": {"workspace": str(self.workspace)}}}))
        result = self.manager.add_skill(AddSkillRequest(agent_name="main", skill_dir=str(self.source)))
        self.assertEqual(result["destination"], str(self.workspace / "skills" / "weather"))

    def test_dry_run_validates_sources_without_changing_environment(self):
        self.manager.runner.dry_run = True
        for common in [True, False]:
            result = self.manager.add_skill(AddSkillRequest(
                common=common, agent_name=None if common else "demo", skill_dir=str(self.source),
            ))
            self.assertTrue(result["skipped"])
        self.assertFalse((self.workspace / "skills").exists())
        self.assertFalse((self.config_path.parent / "skills").exists())
        self.assertEqual(self.config_path.read_bytes(), self.config_before)

    def test_cli_installs_both_scopes_and_returns_structured_errors(self):
        cases = [
            (["--agent", "demo"], 0, 1),
            (["--common", "--skill-name", "shared-weather"], 0, 1),
            (["--agent", "demo"], 1, TYPE_CODE_CONFLICT),
            (["--agent", "missing"], 1, TYPE_CODE_NOT_FOUND),
            ([], 1, TYPE_CODE_INVALID_ARGUMENT),
            (["--agent", "demo", "--common"], 1, TYPE_CODE_INVALID_ARGUMENT),
            (["--common", "--skill-zip", str(self.root / "weather.zip")], 1, TYPE_CODE_INVALID_ARGUMENT),
        ]
        for scope, expected_exit, expected_type in cases:
            with self.subTest(scope=scope):
                output = io.StringIO()
                with redirect_stdout(output):
                    exit_code = main([
                        "--config-path", str(self.config_path), "add-skill",
                        "--skill-dir", str(self.source), *scope,
                    ])
                payload = json.loads(output.getvalue())
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(payload["typeCode"], expected_type)


if __name__ == "__main__":
    unittest.main()
