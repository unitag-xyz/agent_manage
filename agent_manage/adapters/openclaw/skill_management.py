"""Install a single Skill into an agent workspace or the shared environment."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Dict

from ...models import AddSkillRequest
from ...skill_permissions import prepare_shared_skill_path
from ...template_safety import validate_safe_name, validate_template_archive


class SkillManagementMixin:
    def add_skill(self, request: AddSkillRequest) -> Dict[str, object]:
        if (request.agent_name is not None) == request.common:
            raise ValueError("Choose exactly one of agent_name or common")
        if bool(request.skill_dir) == bool(request.skill_zip):
            raise ValueError("Choose exactly one of skill_dir or skill_zip")

        agent_name = None
        if request.common:
            target_root = self.config_path.parent / "skills"
        else:
            agent_name = validate_safe_name(request.agent_name, "agent_name")
            target_root = self._skill_agent_workspace(agent_name) / "skills"

        source_path = Path(request.skill_dir or request.skill_zip).expanduser()
        if source_path.is_symlink():
            raise ValueError(f"Skill source links are not allowed: {source_path}")
        source_path = source_path.resolve()
        if request.skill_dir:
            return self._install_single_skill(
                source_path, source_path.name, source_path, target_root, agent_name, request,
            )

        if not source_path.is_file():
            raise FileNotFoundError(f"Skill ZIP not found: {source_path}")
        if not zipfile.is_zipfile(source_path):
            raise ValueError(f"Skill source must be a ZIP archive: {source_path}")
        validate_template_archive(source_path)
        with tempfile.TemporaryDirectory(prefix="agent-manage-skill-") as temporary:
            extracted = Path(temporary)
            with zipfile.ZipFile(source_path) as archive:
                archive.extractall(extracted)
                # zipfile does not restore Unix executable bits by itself.
                for member in archive.infolist():
                    permissions = (member.external_attr >> 16) & 0o777
                    if permissions and not member.is_dir():
                        (extracted / member.filename).chmod(permissions)
            if (extracted / "SKILL.md").is_file():
                source = extracted
                default_name = source_path.stem
            else:
                children = list(extracted.iterdir())
                if len(children) != 1 or not children[0].is_dir():
                    raise ValueError("Skill ZIP must contain SKILL.md at root or one skill directory")
                source = children[0]
                default_name = source.name
            return self._install_single_skill(
                source, default_name, source_path, target_root, agent_name, request,
            )

    def _skill_agent_workspace(self, agent_name: str) -> Path:
        config = self._load_config()
        agents = config.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("Config agents must be an object")
        entries = agents.get("list", [])
        if not isinstance(entries, list):
            raise ValueError("Config agents.list must be an array")
        agent = next(
            (item for item in entries if isinstance(item, dict) and item.get("id") == agent_name),
            None,
        )
        # OpenClaw also supports a single implicit main agent with only defaults.
        if agent is None and not (agent_name == "main" and not entries):
            raise FileNotFoundError(f"Agent not found: {agent_name}")
        workspace_value = agent.get("workspace") if agent is not None else None
        if not workspace_value and agent_name == "main":
            defaults = agents.get("defaults", {})
            if isinstance(defaults, dict):
                workspace_value = defaults.get("workspace")
        if not isinstance(workspace_value, str) or not workspace_value.strip():
            raise ValueError(f"Agent '{agent_name}' has no configured workspace")
        workspace = Path(workspace_value).expanduser()
        if not workspace.is_absolute():
            raise ValueError(f"Agent '{agent_name}' workspace must be absolute or start with ~")
        workspace = workspace.resolve()
        if not workspace.is_dir():
            raise FileNotFoundError(f"Agent workspace not found: {workspace}")
        return workspace

    def _install_single_skill(
        self,
        source: Path,
        default_name: str,
        original_source: Path,
        target_root: Path,
        agent_name: str | None,
        request: AddSkillRequest,
    ) -> Dict[str, object]:
        if not source.is_dir():
            raise FileNotFoundError(f"Skill directory not found: {source}")
        if not (source / "SKILL.md").is_file():
            raise ValueError(f"Skill directory must contain SKILL.md: {original_source}")
        for item in source.rglob("*"):
            if item.is_symlink() or not (item.is_dir() or item.is_file()):
                raise ValueError(f"Skill links and special files are not allowed: {item}")

        name = validate_safe_name(
            request.skill_name if request.skill_name is not None else default_name,
            "skill_name",
        )
        destination = target_root / name
        if target_root.is_symlink() or destination.is_symlink():
            raise ValueError(f"Skill destination links are not allowed: {destination}")
        if target_root.exists() and not target_root.is_dir():
            raise ValueError(f"Skills root must be a directory: {target_root}")
        resolved_destination = destination.resolve()
        if resolved_destination.is_relative_to(source) or source.is_relative_to(resolved_destination):
            raise ValueError("Skill source and destination must not overlap")
        existed = destination.exists()
        if existed and not request.replace:
            raise FileExistsError(f"Skill already exists: {destination}; use --replace to replace it")
        if existed and not destination.is_dir():
            raise ValueError(f"Skill destination must be a directory: {destination}")

        result = {
            "ok": True,
            "scope": "common" if request.common else "agent",
            "agent_name": agent_name,
            "skill_name": name,
            "source": str(original_source),
            "destination": str(destination),
            "config_path": str(self.config_path),
            "replaced": existed,
            "skipped": self.runner.dry_run,
            "gateway_restarted": False,
            "activation_verified": False,
        }
        if self.runner.dry_run:
            return result

        # Stage the full directory before swapping it, so failed copies cannot
        # corrupt an existing Skill and replacement removes stale files.
        target_root.mkdir(parents=True, exist_ok=True)
        if request.common:
            prepare_shared_skill_path(target_root, self.config_path.parent, recursive=False)
        staging = Path(tempfile.mkdtemp(prefix=f".{name}.install-", dir=str(target_root)))
        backup = staging / "previous"
        try:
            staged_skill = staging / "skill"
            shutil.copytree(source, staged_skill)
            if request.common:
                prepare_shared_skill_path(staged_skill, self.config_path.parent)
            if existed:
                if request.common:
                    prepare_shared_skill_path(destination, self.config_path.parent)
                destination.replace(backup)
            try:
                staged_skill.replace(destination)
            except Exception:
                if backup.exists():
                    backup.replace(destination)
                raise
            if backup.exists():
                if request.common:
                    prepare_shared_skill_path(backup, self.config_path.parent)
                shutil.rmtree(backup)
        finally:
            if backup.exists():
                self.runner.log(f"skill: retained previous version at {backup}")
            else:
                shutil.rmtree(staging)
        self.runner.log(f"skill: installed {name} at {destination}")
        return result
