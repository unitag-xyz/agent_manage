"""High-level create and add-agent workflows for OpenClaw instances."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter
from typing import Dict, List

from .channel_management import ChannelManagementMixin
from .activation import FlyActivationMixin
from .gateway_management import GatewayManagementMixin
from .manager_core import ManagerCore
from .model_management import ModelManagementMixin
from .models import AddAgentRequest, AddAgentsRequest, CreateInstanceRequest
from .template_download import replace_template_archive
from .provisioning import ProvisioningMixin
from .settings import normalize_image_quality, normalize_public_base_url, normalize_shop
from .skill_management import SkillManagementMixin
from .refresh_management import RefreshManagementMixin
from .codex_management import CodexManagementMixin


class InstanceManagerV2(
    FlyActivationMixin,
    ChannelManagementMixin,
    ModelManagementMixin,
    GatewayManagementMixin,
    SkillManagementMixin,
    RefreshManagementMixin,
    CodexManagementMixin,
    ProvisioningMixin,
    ManagerCore,
):
    """Coordinate instance creation across the domain-specific manager mixins."""

    def create_instance(self, request: CreateInstanceRequest) -> Dict[str, object]:
        """Compatibility workflow: register missing agents, then configure the instance."""
        return self._execute_instance(request, require_prebuilt_agents=False)

    def configure_instance(self, request: CreateInstanceRequest) -> Dict[str, object]:
        """Configure runtime settings for agents already supplied by the image."""
        return self._configure_existing_instance(request)

    def _configure_existing_instance(self, request: CreateInstanceRequest) -> Dict[str, object]:
        execution_started_at = perf_counter()
        steps: List[Dict[str, object]] = []
        if not request.model_key.strip():
            raise ValueError("model_key is required")

        model_gateway = self._model_gateway_for_env(request.model_env)
        ai_shop = normalize_shop(request.ai_shop)
        image_quality = normalize_image_quality(request.image_quality)
        public_base_url = normalize_public_base_url(request.base_url)
        config_snapshot = self._snapshot_config_file()

        try:
            config = self._load_config()
            configured_agents = [
                item for item in self._extract_agent_list(config)
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                and item["id"].strip() and item["id"] != "main"
            ]
            if not configured_agents:
                raise ValueError("No prebuilt agents are configured")

            agent_names = [str(item["id"]).strip() for item in configured_agents]
            workspaces: List[Path] = []
            for item in configured_agents:
                workspace = item.get("workspace")
                if not isinstance(workspace, str) or not workspace.strip():
                    raise ValueError(f"Prebuilt agent '{item['id']}' has no workspace")
                workspaces.append(Path(workspace).expanduser().resolve())
            fetched_models = self._run_timed_step(
                steps,
                "models.fetch_catalog",
                lambda: self._fetch_supported_gateway_models(
                    self._catalog_url_for_ai_shop(model_gateway["catalog_url"], ai_shop)
                ),
            )
            existing_primary_model = self._configured_default_model_from_config(config)
            existing_gateway_token = self._configured_gateway_token()
            self._run_timed_step(
                steps,
                "config.configure_models",
                lambda: self._configure_config_models(
                    model_key=request.model_key.strip(),
                    supported_models=fetched_models["models"],
                    primary_model=existing_primary_model,
                    base_url=model_gateway["base_url"],
                    models_config=fetched_models.get("models_config"),
                    ai_shop=ai_shop,
                    official_image_model_available=bool(fetched_models.get("official_image_model_available")),
                ),
            )

            container_gateway_token = self._container_gateway_token()
            gateway_token = container_gateway_token or existing_gateway_token
            if gateway_token is None:
                gateway_token = self._generate_gateway_token()
            self._run_timed_step(
                steps,
                "config.configure_gateway_auth" if container_gateway_token or existing_gateway_token is None else "config.preserve_gateway_auth",
                (lambda: self._configure_gateway_auth(gateway_token))
                if container_gateway_token or existing_gateway_token is None
                else self._preserve_gateway_auth,
            )
            self._run_timed_step(
                steps,
                "config.configure_tools",
                lambda: self._configure_config_tools(agent_names),
            )
            self._run_timed_step(
                steps,
                "workspace.configure_image_generation",
                lambda: self._configure_workspace_defaults(
                    workspaces, quality=image_quality, base_url=public_base_url
                ),
            )
            return {
                "ok": True,
                "mode": "configured",
                "agent_names": agent_names,
                "workspaces": [str(path) for path in workspaces],
                "model_env": request.model_env,
                "ai_shop": ai_shop,
                "image_model": self.IMAGE_MODEL_REF,
                "image_quality": image_quality,
                "base_url": public_base_url,
                "gateway_token": gateway_token,
                "gateway_token_preserved": container_gateway_token is not None or existing_gateway_token is not None,
                "config_path": str(self.config_path),
                "restart_required": True,
                "steps": steps,
                "total_elapsed_ms": self._elapsed_ms(execution_started_at),
            }
        except Exception as exc:
            rollback_steps: List[Dict[str, object]] = []
            if request.rollback_on_fail and config_snapshot is not None:
                rollback_steps.append(self._safe_restore_config_snapshot(config_snapshot))
            raise self._create_instance_failure(
                exc, steps, rollback_steps, execution_started_at
            ) from exc

    def add_agent(
        self,
        *,
        template_name: str,
        agent_name: str | None = None,
        workspace_root: str = "~/data",
        model: str | None = None,
        base_url: str | None = None,
        template_zip_url: str | None = None,
        template_zip_sha256: str | None = None,
    ) -> Dict[str, object]:
        """Register a template's primary and manifest-declared agents."""
        resolved_agent_name = (agent_name or template_name).strip()
        return self.add_agents(
            AddAgentsRequest(
                agents=[
                    AddAgentRequest(
                        agent_name=resolved_agent_name,
                        template_name=template_name,
                        model=model,
                        template_zip_url=template_zip_url,
                        template_zip_sha256=template_zip_sha256,
                    )
                ],
                workspace_root=workspace_root,
                base_url=base_url,
            )
        )

    def _execute_instance(
        self,
        request: CreateInstanceRequest,
        *,
        require_prebuilt_agents: bool,
    ) -> Dict[str, object]:
        execution_started_at = perf_counter()
        steps: List[Dict[str, object]] = []
        if request.local:
            return self._create_local_instance(
                request,
                execution_started_at,
                steps,
                require_prebuilt_agents=require_prebuilt_agents,
            )

        try:
            self._run_timed_step(
                steps,
                "request.prepare",
                lambda: self._create_instance_preparation_result(request, local=False),
            )
        except Exception as exc:
            raise self._create_instance_failure(
                exc, steps, [], execution_started_at
            ) from exc

        if not request.template_name.strip():
            raise ValueError("template_name is required")
        if not request.model_key.strip():
            raise ValueError("model_key is required")
        model_gateway = self._model_gateway_for_env(request.model_env)
        ai_shop = normalize_shop(request.ai_shop)
        image_quality = normalize_image_quality(request.image_quality)
        public_base_url = normalize_public_base_url(request.base_url)
        model_key = request.model_key.strip()
        agent_name = self.resolve_agent_name(request)
        workspace = self.default_workspace(agent_name, request.workspace_root)
        archive_path = self.resolve_archive_path(request)
        template_dir = self.resolve_template_dir(request)
        created_agent = False
        created_template_dir = False
        created_workspace = False
        additional_provisions: List[Dict[str, object]] = []
        config_snapshot = self._snapshot_config_file()

        try:
            provision_result = self._provision_agent_from_template(
                steps=steps,
                template_name=request.template_name,
                agent_name=agent_name,
                archive_path=archive_path,
                template_dir=template_dir,
                workspace=workspace,
                model=request.model,
                rollback_on_fail=request.rollback_on_fail,
                step_scope=None,
                require_existing_agent=require_prebuilt_agents,
            )
            created_agent = bool(provision_result["created_agent"])
            created_template_dir = bool(provision_result["created_template_dir"])
            created_workspace = bool(provision_result["created_workspace"])
            reconcile_existing = not created_agent and not self.runner.dry_run
            existing_primary_model = None
            existing_gateway_token = None
            if reconcile_existing:
                existing_config = self._load_config()
                existing_primary_model = self._configured_default_model_from_config(
                    existing_config
                )
                existing_gateway_token = self._configured_gateway_token()

            additional_specs = self._run_timed_step(
                steps,
                "agents.resolve_additional",
                lambda: self._multi_agent_specs_from_template(
                    template_dir=template_dir,
                    manifest=self._load_template_manifest(template_dir),
                    primary_agent_name=agent_name,
                    workspace_root=request.workspace_root,
                    fallback_model=request.model,
                ),
            )
            additional_agents: List[Dict[str, object]] = []
            for spec in additional_specs:
                spec_result = self._provision_agent_from_prepared_template(
                    steps=steps,
                    template_name=str(spec["template_name"]),
                    agent_name=str(spec["agent_name"]),
                    template_dir=Path(str(spec["template_dir"])),
                    workspace=Path(str(spec["workspace"])),
                    model=spec["model"] if isinstance(spec.get("model"), str) else None,
                    rollback_on_fail=request.rollback_on_fail,
                    step_scope=str(spec["agent_name"]),
                    require_existing_agent=require_prebuilt_agents,
                )
                additional_provisions.append(
                    {
                        "agent_name": spec["agent_name"],
                        "workspace": spec["workspace"],
                        "created_agent": spec_result["created_agent"],
                        "created_workspace": spec_result["created_workspace"],
                    }
                )
                additional_agents.append(
                    {
                        "agent_name": spec["agent_name"],
                        "template_name": spec["template_name"],
                        "source": spec["source"],
                        "workspace": str(spec["workspace"]),
                        "model": spec.get("model"),
                    }
                )

            fetched_models = self._run_timed_step(
                steps,
                "models.fetch_catalog",
                lambda: self._fetch_supported_gateway_models(
                    self._catalog_url_for_ai_shop(
                        model_gateway["catalog_url"],
                        ai_shop,
                    )
                ),
            )

            self._run_timed_step(
                steps,
                "config.configure_models",
                lambda: self._configure_config_models(
                    model_key=model_key,
                    supported_models=fetched_models["models"],
                    primary_model=existing_primary_model,
                    base_url=model_gateway["base_url"],
                    models_config=fetched_models.get("models_config"),
                    ai_shop=ai_shop,
                    official_image_model_available=bool(fetched_models.get("official_image_model_available")),
                ),
            )

            container_gateway_token = self._container_gateway_token()
            gateway_token_preserved = (
                container_gateway_token is not None or existing_gateway_token is not None
            )
            if container_gateway_token is not None:
                gateway_token = container_gateway_token
                self._run_timed_step(
                    steps,
                    "config.configure_gateway_auth",
                    lambda: self._configure_gateway_auth(gateway_token),
                )
            elif existing_gateway_token is not None:
                gateway_token = existing_gateway_token
                self._run_timed_step(
                    steps,
                    "config.preserve_gateway_auth",
                    self._preserve_gateway_auth,
                )
            else:
                gateway_token = self._generate_gateway_token()
                self._run_timed_step(
                    steps,
                    "config.configure_gateway_auth",
                    lambda: self._configure_gateway_auth(gateway_token),
                )

            self._run_timed_step(
                steps,
                "config.configure_tools",
                lambda: self._configure_config_tools(
                    [agent_name, *[str(item["agent_name"]) for item in additional_agents]]
                ),
            )

            self._run_timed_step(
                steps,
                "workspace.configure_image_generation",
                lambda: self._configure_workspace_defaults(
                    [workspace, *[Path(str(item["workspace"])) for item in additional_agents]],
                    quality=image_quality,
                    base_url=public_base_url,
                ),
            )

            return {
                "ok": True,
                "mode": (
                    "configured"
                    if require_prebuilt_agents
                    else ("reconciled" if reconcile_existing else "created")
                ),
                "template_name": request.template_name,
                "agent_name": agent_name,
                "additional_agents": additional_agents,
                "model_env": request.model_env,
                "ai_shop": ai_shop,
                "image_model": self.IMAGE_MODEL_REF,
                "image_quality": image_quality,
                "base_url": public_base_url,
                "gateway_token": gateway_token,
                "gateway_token_preserved": gateway_token_preserved,
                "workspace": str(workspace),
                "archive_path": str(archive_path),
                "template_dir": str(template_dir) if template_dir else None,
                "restart_required": True,
                "steps": steps,
                "total_elapsed_ms": self._elapsed_ms(execution_started_at),
            }
        except Exception as exc:
            payload = self._embedded_error_payload(exc)
            if payload.get("rollback") and not (created_agent or created_workspace or additional_provisions):
                payload["total_elapsed_ms"] = self._elapsed_ms(execution_started_at)
                raise RuntimeError(json.dumps(payload, ensure_ascii=False)) from exc
            rollback_steps: List[Dict[str, object]] = []
            if request.rollback_on_fail:
                for item in reversed(additional_provisions):
                    item_workspace = Path(str(item["workspace"]))
                    if item.get("created_workspace"):
                        self._run_timed_rollback_step(rollback_steps, lambda: self._safe_purge_workspace(item_workspace))
                    if item.get("created_agent"):
                        self._run_timed_rollback_step(rollback_steps, lambda: self._safe_delete_agent(str(item["agent_name"])))
                if created_workspace:
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_purge_workspace(workspace))
                if created_agent:
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_delete_agent(agent_name))
                if created_template_dir and template_dir.exists():
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_purge_template_dir(template_dir))
                if config_snapshot is not None:
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_restore_config_snapshot(config_snapshot))
            raise self._create_instance_failure(
                exc, steps, rollback_steps, execution_started_at
            ) from exc

    def _create_local_instance(
        self,
        request: CreateInstanceRequest,
        execution_started_at: float,
        steps: List[Dict[str, object]],
        *,
        require_prebuilt_agents: bool,
    ) -> Dict[str, object]:
        try:
            self._run_timed_step(
                steps,
                "request.prepare",
                lambda: self._create_instance_preparation_result(request, local=True),
            )
        except Exception as exc:
            raise self._create_instance_failure(
                exc, steps, [], execution_started_at
            ) from exc
        if not request.model_key.strip():
            raise ValueError("model_key is required")
        if not request.agent_zip and not request.template_name.strip():
            raise ValueError("template_name or agent_zip is required")

        model_gateway = self._model_gateway_for_env(request.model_env)
        ai_shop = normalize_shop(request.ai_shop)
        image_quality = normalize_image_quality(request.image_quality)
        public_base_url = normalize_public_base_url(request.base_url)
        model_key = request.model_key.strip()
        agent_name = self.resolve_agent_name(request)
        workspace_root = request.workspace_root or self.LOCAL_WORKSPACE_ROOT
        workspace = self.default_workspace(agent_name, workspace_root)
        archive_path = self.resolve_archive_path(request)
        template_dir = self.resolve_template_dir(request)
        created_agent = False
        created_template_dir = False
        created_workspace = False
        additional_provisions: List[Dict[str, object]] = []
        config_snapshot = self._snapshot_config_file()

        try:
            provision_result = self._provision_agent_from_template(
                steps=steps,
                template_name=agent_name,
                agent_name=agent_name,
                archive_path=archive_path,
                template_dir=template_dir,
                workspace=workspace,
                model=request.model,
                rollback_on_fail=request.rollback_on_fail,
                step_scope=None,
                require_existing_agent=require_prebuilt_agents,
            )
            created_agent = bool(provision_result["created_agent"])
            created_template_dir = bool(provision_result["created_template_dir"])
            created_workspace = bool(provision_result["created_workspace"])

            additional_specs = self._run_timed_step(
                steps,
                "agents.resolve_additional",
                lambda: self._multi_agent_specs_from_template(
                    template_dir=template_dir,
                    manifest=self._load_template_manifest(template_dir),
                    primary_agent_name=agent_name,
                    workspace_root=workspace_root,
                    fallback_model=request.model,
                ),
            )
            additional_agents: List[Dict[str, object]] = []
            for spec in additional_specs:
                spec_result = self._provision_agent_from_prepared_template(
                    steps=steps,
                    template_name=str(spec["template_name"]),
                    agent_name=str(spec["agent_name"]),
                    template_dir=Path(str(spec["template_dir"])),
                    workspace=Path(str(spec["workspace"])),
                    model=spec["model"] if isinstance(spec.get("model"), str) else None,
                    rollback_on_fail=request.rollback_on_fail,
                    step_scope=str(spec["agent_name"]),
                    require_existing_agent=require_prebuilt_agents,
                )
                additional_provisions.append(
                    {
                        "agent_name": spec["agent_name"],
                        "workspace": spec["workspace"],
                        "created_agent": spec_result["created_agent"],
                        "created_workspace": spec_result["created_workspace"],
                    }
                )
                additional_agents.append(
                    {
                        "agent_name": spec["agent_name"],
                        "template_name": spec["template_name"],
                        "source": spec["source"],
                        "workspace": str(spec["workspace"]),
                        "model": spec.get("model"),
                    }
                )

            fetched_models = self._run_timed_step(
                steps,
                "models.fetch_catalog",
                lambda: self._fetch_supported_gateway_models(
                    self._catalog_url_for_ai_shop(
                        model_gateway["catalog_url"],
                        ai_shop,
                    )
                ),
            )

            self._run_timed_step(
                steps,
                "config.configure_models",
                lambda: self._configure_config_models(
                    model_key=model_key,
                    supported_models=fetched_models["models"],
                    primary_model=request.model,
                    base_url=model_gateway["base_url"],
                    models_config=fetched_models.get("models_config"),
                    ai_shop=ai_shop,
                    official_image_model_available=bool(fetched_models.get("official_image_model_available")),
                ),
            )

            gateway_auth_result = self._run_timed_step(
                steps,
                "config.preserve_gateway_auth",
                self._preserve_gateway_auth,
            )

            self._run_timed_step(
                steps,
                "config.configure_tools",
                lambda: self._configure_config_tools(
                    [agent_name, *[str(item["agent_name"]) for item in additional_agents]]
                ),
            )

            self._run_timed_step(
                steps,
                "workspace.configure_image_generation",
                lambda: self._configure_workspace_defaults(
                    [workspace, *[Path(str(item["workspace"])) for item in additional_agents]],
                    quality=image_quality,
                    base_url=public_base_url,
                ),
            )

            return {
                "ok": True,
                "mode": "local",
                "template_name": agent_name,
                "agent_name": agent_name,
                "additional_agents": additional_agents,
                "model_env": request.model_env,
                "ai_shop": ai_shop,
                "image_model": self.IMAGE_MODEL_REF,
                "image_quality": image_quality,
                "base_url": public_base_url,
                "gateway_auth": gateway_auth_result,
                "workspace": str(workspace),
                "workspace_root": str(Path(workspace_root).expanduser().resolve()),
                "archive_path": str(archive_path),
                "template_dir": str(template_dir) if template_dir else None,
                "config_path": str(self.config_path),
                "restart_required": True,
                "steps": steps,
                "total_elapsed_ms": self._elapsed_ms(execution_started_at),
            }
        except Exception as exc:
            payload = self._embedded_error_payload(exc)
            if payload.get("rollback") and not (created_agent or created_workspace or additional_provisions):
                payload["total_elapsed_ms"] = self._elapsed_ms(execution_started_at)
                raise RuntimeError(json.dumps(payload, ensure_ascii=False)) from exc
            rollback_steps: List[Dict[str, object]] = []
            if request.rollback_on_fail:
                for item in reversed(additional_provisions):
                    item_workspace = Path(str(item["workspace"]))
                    if item.get("created_workspace"):
                        self._run_timed_rollback_step(rollback_steps, lambda: self._safe_purge_workspace(item_workspace))
                    if item.get("created_agent"):
                        self._run_timed_rollback_step(rollback_steps, lambda: self._safe_delete_agent(str(item["agent_name"])))
                if created_workspace:
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_purge_workspace(workspace))
                if created_agent:
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_delete_agent(agent_name))
                if created_template_dir and template_dir.exists():
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_purge_template_dir(template_dir))
                if config_snapshot is not None:
                    self._run_timed_rollback_step(rollback_steps, lambda: self._safe_restore_config_snapshot(config_snapshot))
            raise self._create_instance_failure(
                exc, steps, rollback_steps, execution_started_at
            ) from exc

    def _create_instance_preparation_result(
        self,
        request: CreateInstanceRequest,
        *,
        local: bool,
    ) -> Dict[str, object]:
        if not request.model_key.strip():
            raise ValueError("model_key is required")
        if local:
            if not request.agent_zip and not request.template_name.strip():
                raise ValueError("template_name or agent_zip is required")
        elif not request.template_name.strip():
            raise ValueError("template_name is required")

        self._model_gateway_for_env(request.model_env)
        ai_shop = normalize_shop(request.ai_shop)
        image_quality = normalize_image_quality(request.image_quality)
        agent_name = self.resolve_agent_name(request)
        workspace_root = (
            request.workspace_root or self.LOCAL_WORKSPACE_ROOT
            if local
            else request.workspace_root
        )
        workspace = self.default_workspace(agent_name, workspace_root)
        return {
            "local": local,
            "agent_name": agent_name,
            "model_env": request.model_env,
            "ai_shop": ai_shop,
            "image_quality": image_quality,
            "workspace": str(workspace),
            "archive_path": str(self.resolve_archive_path(request)),
            "template_dir": str(self.resolve_template_dir(request)),
        }

    def _create_instance_failure(
        self,
        exc: Exception,
        steps: List[Dict[str, object]],
        rollback_steps: List[Dict[str, object]],
        execution_started_at: float,
    ) -> Exception:
        payload = json.dumps(
            {
                "error": str(exc),
                "details": self._error_details(exc),
                "steps": steps,
                "rollback": rollback_steps,
                "total_elapsed_ms": self._elapsed_ms(execution_started_at),
            },
            ensure_ascii=False,
        )
        return ValueError(payload) if isinstance(exc, ValueError) else RuntimeError(payload)

    def add_agents(self, request: AddAgentsRequest) -> Dict[str, object]:
        if not request.agents:
            raise ValueError("agents is required")
        public_base_url = normalize_public_base_url(request.base_url)

        steps: List[Dict[str, object]] = []
        agent_results: List[Dict[str, object]] = []
        seen_agent_names = set()
        completed_provisions: List[Dict[str, object]] = []
        config_existed_before = self.config_path.exists()
        config_snapshot = self._snapshot_config_file()

        try:
            with ExitStack() as archive_replacements:
                for spec in request.agents:
                    agent_name = spec.agent_name.strip()
                    if not agent_name:
                        raise ValueError("agent_name is required")
                    if agent_name in seen_agent_names:
                        raise ValueError(f"Duplicate agent_name in batch: {agent_name}")
                    seen_agent_names.add(agent_name)

                    template_name = self.resolve_add_agent_template_name(spec)
                    workspace = self.resolve_add_agent_workspace(spec, request.workspace_root)
                    archive_path = self.template_root / f"{template_name}.zip"
                    template_dir = self.template_root / template_name

                    archive_replacements.enter_context(
                        replace_template_archive(
                            url=spec.template_zip_url,
                            expected_sha256=spec.template_zip_sha256,
                            destination=archive_path,
                        )
                    )
                    provision_result = self._provision_agent_from_template(
                        steps=steps,
                        template_name=template_name,
                        agent_name=agent_name,
                        archive_path=archive_path,
                        template_dir=template_dir,
                        workspace=workspace,
                        model=spec.model,
                        rollback_on_fail=True,
                        step_scope=agent_name,
                        defer_template_dir_commit=True,
                    )
                    completed_provisions.append(
                        {
                            "agent_name": agent_name,
                            "workspace": workspace,
                            "template_dir": template_dir,
                            **provision_result,
                        }
                    )

                    agent_results.append(
                        {
                            "agent_name": agent_name,
                            "template_name": template_name,
                            "workspace": str(workspace),
                            "archive_path": str(archive_path),
                            "template_dir": str(template_dir),
                            "model": spec.model,
                        }
                    )
                    manifest = self._load_template_manifest(template_dir)
                    additional_specs = self._multi_agent_specs_from_template(
                        template_dir=template_dir,
                        manifest=manifest,
                        primary_agent_name=agent_name,
                        workspace_root=request.workspace_root,
                        fallback_model=spec.model,
                    )
                    for additional_spec in additional_specs:
                        additional_agent_name = str(additional_spec["agent_name"])
                        if additional_agent_name in seen_agent_names:
                            raise ValueError(f"Duplicate agent_name in batch: {additional_agent_name}")
                        seen_agent_names.add(additional_agent_name)

                        additional_template_dir = Path(str(additional_spec["template_dir"]))
                        additional_workspace = Path(str(additional_spec["workspace"]))
                        additional_model = (
                            additional_spec["model"]
                            if isinstance(additional_spec.get("model"), str)
                            else None
                        )
                        additional_result = self._provision_agent_from_prepared_template(
                            steps=steps,
                            template_name=str(additional_spec["template_name"]),
                            agent_name=additional_agent_name,
                            template_dir=additional_template_dir,
                            workspace=additional_workspace,
                            model=additional_model,
                            rollback_on_fail=True,
                            step_scope=additional_agent_name,
                        )
                        completed_provisions.append(
                            {
                                "agent_name": additional_agent_name,
                                "workspace": additional_workspace,
                                "template_dir": None,
                                "created_template_dir": False,
                                "preserved_template_dir": None,
                                **additional_result,
                            }
                        )
                        agent_results.append(
                            {
                                "agent_name": additional_agent_name,
                                "template_name": additional_spec["template_name"],
                                "source": additional_spec["source"],
                                "parent_agent_name": agent_name,
                                "workspace": str(additional_workspace),
                                "archive_path": str(archive_path),
                                "template_dir": str(additional_template_dir),
                                "model": additional_model,
                            }
                        )

                step_results = self._index_step_results(steps)
                added_count = 0
                skipped_count = 0
                for item in agent_results:
                    agent_name = item["agent_name"]
                    add_result = step_results.get(self._scoped_step_name("agents.add", agent_name), {})
                    status = "skipped" if add_result.get("skipped") else "added"
                    item["status"] = status
                    item["result"] = {
                        "template_prepare": step_results.get(
                            self._scoped_step_name("template.prepare", agent_name),
                            {},
                        ),
                        "libraries_ensure": step_results.get(
                            self._scoped_step_name("libraries.ensure", agent_name),
                            {},
                        ),
                        "common_skills_install": step_results.get(
                            self._scoped_step_name("common_skills.install", agent_name),
                            {},
                        ),
                        "agents_add": add_result,
                        "workspace_populate": step_results.get(
                            self._scoped_step_name("workspace.populate", agent_name),
                            {},
                        ),
                    }
                    if status == "added":
                        added_count += 1
                    else:
                        skipped_count += 1

                tools_result = self._run_timed_step(
                    steps,
                    "config.configure_tools",
                    lambda: self._configure_config_tools([item["agent_name"] for item in agent_results]),
                )
                workspace_defaults = self._run_timed_step(
                    steps,
                    "workspace.configure_image_generation",
                    lambda: self._configure_workspace_defaults(
                        [Path(str(item["workspace"])) for item in agent_results],
                        quality="low",
                        base_url=public_base_url,
                    ),
                )

                for provision in completed_provisions:
                    preserved_template_dir = provision.get("preserved_template_dir")
                    if isinstance(preserved_template_dir, Path) and preserved_template_dir.exists():
                        self._safe_purge_template_dir(preserved_template_dir)

                return {
                    "ok": True,
                    "requested_count": len(request.agents),
                    "added_count": added_count,
                    "skipped_count": skipped_count,
                    "restart_required": added_count > 0,
                    "post_batch_actions": [],
                    "base_url": public_base_url,
                    "tools_config": tools_result,
                    "workspace_defaults": workspace_defaults,
                    "agents": agent_results,
                    "steps": steps,
                }
        except Exception:
            rollback_steps: List[Dict[str, object]] = []
            for provision in reversed(completed_provisions):
                workspace = Path(str(provision["workspace"]))
                if provision.get("created_workspace"):
                    self._run_timed_rollback_step(
                        rollback_steps,
                        lambda workspace=workspace: self._safe_purge_workspace(workspace),
                    )
                if provision.get("created_agent"):
                    agent_name = str(provision["agent_name"])
                    self._run_timed_rollback_step(
                        rollback_steps,
                        lambda agent_name=agent_name: self._safe_delete_agent(agent_name),
                    )

                template_dir = provision.get("template_dir")
                preserved_template_dir = provision.get("preserved_template_dir")
                if isinstance(template_dir, Path):
                    if template_dir.exists() and (
                        provision.get("created_template_dir")
                        or isinstance(preserved_template_dir, Path)
                    ):
                        self._run_timed_rollback_step(
                            rollback_steps,
                            lambda template_dir=template_dir: self._safe_purge_template_dir(template_dir),
                        )
                    if isinstance(preserved_template_dir, Path) and preserved_template_dir.exists():
                        self._run_timed_rollback_step(
                            rollback_steps,
                            lambda preserved_template_dir=preserved_template_dir, template_dir=template_dir:
                                self._safe_restore_template_dir(preserved_template_dir, template_dir),
                        )

            if config_snapshot is not None:
                self._run_timed_rollback_step(
                    rollback_steps,
                    lambda: self._safe_restore_config_snapshot(config_snapshot),
                )
            elif not config_existed_before and self.config_path.exists():
                try:
                    self.config_path.unlink(missing_ok=True)
                except Exception as rollback_error:
                    self.runner.log(f"failed to remove newly created config during rollback: {rollback_error}")
            raise
