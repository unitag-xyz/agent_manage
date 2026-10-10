"""Model catalog loading, routing, filtering, and OpenClaw configuration."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from .models import SetModelRequest
from .settings import (
    catalog_url_for_shop,
    gateway_for_base_url,
    model_base_url_for_shop,
    same_url_host,
    shop_from_model_base_url,
)


class ModelManagementMixin:
    """Manage model catalogs and the model-related OpenClaw config surface."""

    def set_model(self, request: SetModelRequest) -> Dict[str, object]:
        supported_model_refs = self._supported_model_refs_from_config()
        if request.model_ref not in supported_model_refs:
            allowed = ", ".join(sorted(supported_model_refs))
            raise ValueError(f"Unsupported model '{request.model_ref}'. Allowed: {allowed}")

        set_result = self.runner.run(
            [self.bin, "models", "set", request.model_ref],
            timeout=self.OPENCLAW_COMMAND_TIMEOUT_SECONDS,
        )
        restart_result = self._restart_gateway_service()

        return {
            "ok": True,
            "model_ref": request.model_ref,
            "steps": [
                self._command_step("models.set", set_result),
            ],
            "gateway_restart": self._build_step_payload("gateway.restart", restart_result),
        }

    def get_current_model(self) -> Dict[str, object]:
        config = self._load_config()
        defaults = config.get("agents", {}).get("defaults", {})
        configured_default_model = (
            defaults.get("model", {}).get("primary")
            if isinstance(defaults.get("model"), dict)
            else None
        )
        agent_overrides = []
        for agent in config.get("agents", {}).get("list", []):
            if not isinstance(agent, dict):
                continue
            agent_id = agent.get("id")
            if agent_id == "main":
                continue
            model = agent.get("model", {})
            if isinstance(model, dict) and model.get("primary"):
                agent_overrides.append(
                    {
                        "agent_id": agent_id,
                        "model": model.get("primary"),
                    }
                )
        return {
            "ok": True,
            "current_model": configured_default_model,
            "configured_default_model": configured_default_model,
            "agent_overrides": agent_overrides,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
        }

    def get_supported_models(self) -> Dict[str, object]:
        config = self._load_config()
        if self._codex_models_active():
            state = self._codex_read_state()
            models = [{"id": row["id"], "provider": "openai", "model_ref": "openai/" + row["id"],
                       "definition": row} for row in state["models"]]
            return {"ok": True, "provider": "openai", "providers": ["openai"],
                    "current_model": self._configured_default_model_from_config(config),
                    "supported_model_refs": [row["model_ref"] for row in models], "models": models,
                    "config_path": str(self.config_path), "config_exists": self.config_path.exists()}
        defaults = config.get("agents", {}).get("defaults", {})
        current_model = (
            defaults.get("model", {}).get("primary")
            if isinstance(defaults.get("model"), dict)
            else None
        )
        models = self._supported_models_from_config(config)

        return {
            "ok": True,
            "provider": self.MANAGED_MODEL_PROVIDER,
            "providers": self._model_provider_keys(config.get("models")),
            "current_model": current_model,
            "supported_model_refs": [item["model_ref"] for item in models],
            "models": models,
            "config_path": str(self.config_path),
            "config_exists": self.config_path.exists(),
        }

    def update_model_catalog(self) -> Dict[str, object]:
        if self._codex_models_active():
            return {"ok": True, "skipped": True, "reason": "codex_login_active", "restart_required": False}
        config = self._load_config()
        current_model = self._configured_default_model_from_config(config)
        model_key = self._configured_model_api_key_from_config(config)
        configured_base_url = self._configured_model_base_url_from_config(config, current_model)
        gateway = self._model_gateway_for_base_url(configured_base_url)
        ai_shop = self._ai_shop_for_model_base_url(configured_base_url)
        catalog_url = self._catalog_url_for_ai_shop(gateway["catalog_url"], ai_shop)
        steps: List[Dict[str, object]] = []

        fetched_models = self._run_timed_step(
            steps,
            "models.fetch_catalog",
            lambda: self._fetch_supported_gateway_models(catalog_url),
        )
        configure_result = self._run_timed_step(
            steps,
            "config.configure_models",
            lambda: self._configure_config_models(
                model_key=model_key,
                supported_models=fetched_models["models"],
                primary_model=current_model,
                base_url=gateway["base_url"],
                models_config=fetched_models.get("models_config"),
                ai_shop=ai_shop,
                official_image_model_available=bool(fetched_models.get("official_image_model_available")),
            ),
        )

        return {
            "ok": True,
            "provider": self.MANAGED_MODEL_PROVIDER,
            "providers": configure_result["providers"],
            "current_model_before": current_model,
            "current_model_after": configure_result["primary_model"],
            "supported_model_refs": configure_result["managed_models"],
            "steps": steps,
            "config_path": str(self.config_path),
        }

    def _configure_config_models(
        self,
        model_key: str,
        supported_models: List[Dict[str, object]],
        primary_model: Optional[str] = None,
        base_url: Optional[str] = None,
        models_config: Optional[Dict[str, object]] = None,
        ai_shop: Optional[str] = None,
        official_image_model_available: bool = False,
    ) -> Dict[str, object]:
        if self._codex_models_active():
            raise FileExistsError("Log out of Codex before reconfiguring model providers")
        config_path = self.config_path
        resolved_base_url = self._model_base_url_for_ai_shop(
            base_url or self.MODEL_GATEWAYS[self.DEFAULT_MODEL_ENV]["base_url"],
            ai_shop,
        )
        image_base_url = self._image_model_base_url(
            selected_base_url=resolved_base_url,
            official_image_model_available=official_image_model_available,
        )
        managed_model_refs = [item["model_ref"] for item in supported_models]
        resolved_primary_model = self._select_primary_model_ref(
            supported_models,
            preferred_model_ref=primary_model,
        )
        if self.runner.dry_run:
            return {
                "skipped": True,
                "config_path": str(config_path),
                "base_url": resolved_base_url,
                "primary_model": resolved_primary_model,
                "managed_models": managed_model_refs,
                "providers": self._model_provider_keys(models_config),
                "image_model": self.IMAGE_MODEL_REF,
            }

        config = self._load_config()
        if not isinstance(config, dict):
            raise ValueError(f"Config must be a JSON object: {config_path}")

        agents = config.setdefault("agents", {})
        defaults = agents.setdefault("defaults", {})
        defaults["models"] = {model_ref: {} for model_ref in managed_model_refs}
        defaults["model"] = {"primary": resolved_primary_model}
        defaults["imageGenerationModel"] = {
            "primary": self.IMAGE_MODEL_REF,
            "timeoutMs": 180000,
        }
        # npm stable OpenClaw 2026.7.1 uses the capability-specific
        # imageGenerationModel key. Remove the newer mediaModels shape if a
        # previous agent_manage run wrote it; stable rejects that whole key.
        defaults.pop("mediaModels", None)

        config["models"] = self._models_config_with_api_key(
            model_key=model_key,
            models_config=models_config,
            fallback_base_url=resolved_base_url,
            supported_models=supported_models,
            ai_shop=ai_shop,
            image_base_url=image_base_url,
        )

        self._write_config(
            config,
            note="configure models for create_instance",
            changed_paths=[
                "agents.defaults.models",
                "agents.defaults.model",
                "agents.defaults.imageGenerationModel",
                "agents.defaults.mediaModels",
                "models",
            ],
            extra={
                "primary_model": resolved_primary_model,
                "managed_models": managed_model_refs,
                "base_url": resolved_base_url,
                "providers": self._model_provider_keys(config["models"]),
                "image_model": self.IMAGE_MODEL_REF,
            },
        )
        return {
            "config_path": str(config_path),
            "base_url": resolved_base_url,
            "primary_model": resolved_primary_model,
            "managed_models": managed_model_refs,
            "providers": self._model_provider_keys(config["models"]),
            "image_model": self.IMAGE_MODEL_REF,
        }

    def _models_config_with_api_key(
        self,
        *,
        model_key: str,
        models_config: Optional[Dict[str, object]],
        fallback_base_url: str,
        supported_models: List[Dict[str, object]],
        ai_shop: Optional[str] = None,
        image_base_url: Optional[str] = None,
    ) -> Dict[str, object]:
        if models_config is None:
            resolved = {
                "mode": "merge",
                "providers": {
                    self.MANAGED_MODEL_PROVIDER: {
                        "baseUrl": fallback_base_url,
                        "api": "openai-completions",
                        "apiKey": model_key,
                        "models": [
                            sanitized_definition
                            for item in supported_models
                            if isinstance(item.get("definition"), dict)
                            for sanitized_definition in [
                                self._sanitize_openclaw_model_definition(item["definition"])
                            ]
                            if sanitized_definition
                        ],
                    }
                },
            }
        else:
            resolved = self._sanitize_openclaw_models_config(models_config)
        providers = resolved.get("providers")
        if not isinstance(providers, dict):
            raise ValueError("Model config missing providers")
        for provider_key, provider in providers.items():
            if not isinstance(provider, dict):
                raise ValueError(f"Model provider config must be an object: {provider_key}")
            provider["apiKey"] = model_key
            provider["baseUrl"] = fallback_base_url
            if provider_key == self.OPENAI_MODEL_PROVIDER:
                provider["api"] = "openai-responses"

        image_provider = providers.setdefault(
            self.IMAGE_MODEL_PROVIDER,
            {
                "baseUrl": fallback_base_url,
                "api": "openai-responses",
                "models": [],
            },
        )
        if not isinstance(image_provider, dict):
            raise ValueError("OpenAI image provider config must be an object")
        image_provider["baseUrl"] = image_base_url or fallback_base_url
        image_provider["apiKey"] = model_key
        image_provider["api"] = "openai-responses"
        definitions = image_provider.setdefault("models", [])
        if not isinstance(definitions, list):
            raise ValueError("OpenAI image provider models must be a list")
        if not any(
            isinstance(item, dict) and item.get("id") == self.IMAGE_MODEL_ID
            for item in definitions
        ):
            definitions.append(
                {
                    "id": self.IMAGE_MODEL_ID,
                    "name": "GPT Image 2",
                    "input": ["text", "image"],
                }
            )
        if "mode" not in resolved:
            resolved["mode"] = "merge"
        return resolved

    def _image_model_base_url(
        self,
        *,
        selected_base_url: str,
        official_image_model_available: bool,
    ) -> str:
        if official_image_model_available:
            return selected_base_url
        for gateway in self.MODEL_GATEWAYS.values():
            if self._same_url_host(gateway["base_url"], selected_base_url):
                return gateway["base_url"]
        raise ValueError(f"Unsupported image fallback baseUrl '{selected_base_url}'")

    def _model_provider_keys(self, models_config: Optional[Dict[str, object]]) -> List[str]:
        if not isinstance(models_config, dict):
            return [self.MANAGED_MODEL_PROVIDER]
        providers = models_config.get("providers")
        if not isinstance(providers, dict):
            return []
        return [str(key) for key in providers.keys()]

    def _fetch_supported_gateway_models(self, catalog_url: Optional[str] = None) -> Dict[str, object]:
        resolved_catalog_url = catalog_url or self.MODEL_CATALOG_URL
        self.runner.log(f"models: fetch catalog {resolved_catalog_url}")
        request = Request(
            resolved_catalog_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "agent_manage/1.0",
            },
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise RuntimeError(f"Failed to fetch model catalog: {exc}") from exc

        content = payload.get("content")
        if isinstance(content, dict):
            models_config = content.get("models")
            if isinstance(models_config, dict) and isinstance(models_config.get("providers"), dict):
                return self._normalize_provider_catalog_models(
                    models_config=models_config,
                    source_url=resolved_catalog_url,
                )

        raise ValueError("Model catalog response missing content.models.providers")

    def _normalize_provider_catalog_models(
        self,
        *,
        models_config: Dict[str, object],
        source_url: str,
    ) -> Dict[str, object]:
        official_image_model_available = self._catalog_has_official_openai_image_model(
            models_config
        )
        chat_models_config = self._filter_catalog_models_config(
            models_config,
            allowed_categories={"chat"},
        )
        openclaw_models_config = self._sanitize_openclaw_models_config(chat_models_config)
        providers = openclaw_models_config.get("providers")
        if not isinstance(providers, dict):
            raise ValueError("Model catalog response missing providers")

        models = self._normalized_catalog_model_entries(providers)
        if not models:
            raise ValueError("Model catalog did not contain any chat models")

        models.sort(key=self._supported_model_sort_key)
        return {
            "source_url": source_url,
            "model_count": len(models),
            "models": models,
            "primary_model": self._select_primary_model_ref(models),
            "models_config": openclaw_models_config,
            "official_image_model_available": official_image_model_available,
        }

    def _catalog_has_official_openai_image_model(
        self,
        models_config: Dict[str, object],
    ) -> bool:
        providers = models_config.get("providers")
        if not isinstance(providers, dict):
            return False
        provider = providers.get(self.IMAGE_MODEL_PROVIDER)
        if not isinstance(provider, dict):
            return False
        definitions = provider.get("models")
        if not isinstance(definitions, list):
            return False
        return any(
            isinstance(item, dict)
            and str(item.get("id") or "").strip().removeprefix("openai/") == self.IMAGE_MODEL_ID
            for item in definitions
        )

    def _normalized_catalog_model_entries(
        self,
        providers: Dict[str, object],
    ) -> List[Dict[str, object]]:
        models: List[Dict[str, object]] = []
        for provider_key, provider_config in providers.items():
            provider_name = str(provider_key).strip()
            if not provider_name or not isinstance(provider_config, dict):
                continue
            definitions = provider_config.get("models")
            if not isinstance(definitions, list):
                continue
            for definition in definitions:
                if not isinstance(definition, dict):
                    continue
                model_id = definition.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                models.append(
                    {
                        "id": model_id,
                        "provider": provider_name,
                        "model_ref": self._model_ref(provider_name, model_id),
                        "definition": definition,
                    }
                )
        return models

    def _filter_catalog_models_config(
        self,
        models_config: Dict[str, object],
        *,
        allowed_categories: set[str],
    ) -> Dict[str, object]:
        filtered = deepcopy(models_config)
        providers = filtered.get("providers")
        if not isinstance(providers, dict):
            raise ValueError("Model catalog response missing providers")

        filtered_providers: Dict[str, object] = {}
        for provider_key, provider_config in providers.items():
            if not isinstance(provider_config, dict):
                continue
            definitions = provider_config.get("models")
            if not isinstance(definitions, list):
                continue
            kept_definitions = [
                definition
                for definition in definitions
                if isinstance(definition, dict)
                and self._catalog_model_category(definition) in allowed_categories
            ]
            if not kept_definitions:
                continue
            filtered_provider = deepcopy(provider_config)
            filtered_provider["models"] = kept_definitions
            filtered_providers[str(provider_key)] = filtered_provider
        filtered["providers"] = filtered_providers
        return filtered

    def _catalog_model_category(self, definition: Dict[str, object]) -> str:
        model_id = str(definition.get("id") or "").strip()
        if model_id.removeprefix("openai/") == self.IMAGE_MODEL_ID:
            return "image"
        category = definition.get("modelCategory")
        if category is None:
            return "chat"
        return str(category).strip().lower()

    def _sanitize_openclaw_models_config(self, models_config: Dict[str, object]) -> Dict[str, object]:
        providers = models_config.get("providers")
        if not isinstance(providers, dict):
            raise ValueError("Model catalog response missing providers")

        sanitized: Dict[str, object] = {}
        if "mode" in models_config:
            sanitized["mode"] = deepcopy(models_config["mode"])

        sanitized_providers: Dict[str, Dict[str, object]] = {}
        for provider_key, provider_config in providers.items():
            provider_name = str(provider_key).strip()
            if not provider_name or not isinstance(provider_config, dict):
                continue

            sanitized_provider: Dict[str, object] = {}
            for key in self.OPENCLAW_PROVIDER_CONFIG_KEYS:
                if key == "models":
                    continue
                if key in provider_config:
                    sanitized_provider[key] = deepcopy(provider_config[key])

            definitions = provider_config.get("models")
            if isinstance(definitions, list):
                sanitized_definitions = []
                for definition in definitions:
                    if not isinstance(definition, dict):
                        continue
                    sanitized_definition = self._sanitize_openclaw_model_definition(
                        definition
                    )
                    if sanitized_definition:
                        sanitized_definitions.append(sanitized_definition)
                sanitized_provider["models"] = sanitized_definitions

            sanitized_providers[provider_name] = sanitized_provider

        sanitized["providers"] = sanitized_providers
        return sanitized

    def _model_ref(self, provider_name: str, model_id: str) -> str:
        """Build a selectable ref without changing the catalog's model id."""

        prefix = f"{provider_name}/"
        return model_id if model_id.startswith(prefix) else f"{provider_name}/{model_id}"

    def _model_id_for_matching(self, model_id: str) -> str:
        """Return a comparison-only id while preserving the catalog value elsewhere."""

        return model_id.rsplit("/", 1)[-1]

    def _sanitize_openclaw_model_definition(self, definition: Dict[str, object]) -> Dict[str, object]:
        sanitized = {
            key: deepcopy(definition[key])
            for key in self.OPENCLAW_MODEL_DEFINITION_KEYS
            if key in definition
        }
        if "name" not in sanitized and isinstance(definition.get("displayName"), str):
            sanitized["name"] = definition["displayName"]

        if "input" in sanitized:
            model_input = sanitized["input"]
            supported_input = (
                [
                    input_type
                    for input_type in model_input
                    if input_type in self.OPENCLAW_MODEL_INPUT_TYPES
                ]
                if isinstance(model_input, list)
                else []
            )
            sanitized["input"] = supported_input or ["text"]

        cost = sanitized.get("cost")
        if isinstance(cost, dict):
            sanitized["cost"] = {
                key: deepcopy(cost[key])
                for key in self.OPENCLAW_MODEL_COST_KEYS
                if key in cost
            }
        return sanitized

    def _model_gateway_for_env(self, model_env: Optional[str]) -> Dict[str, str]:
        resolved_env = (model_env or self.DEFAULT_MODEL_ENV).strip()
        gateway = self.MODEL_GATEWAYS.get(resolved_env)
        if gateway is None:
            allowed = ", ".join(sorted(self.MODEL_GATEWAYS))
            raise ValueError(f"Unsupported model_env '{resolved_env}'. Allowed: {allowed}")
        return gateway

    def _catalog_url_for_ai_shop(self, catalog_url: str, ai_shop: Optional[str]) -> str:
        return catalog_url_for_shop(catalog_url, ai_shop)

    def _model_base_url_for_ai_shop(self, base_url: str, ai_shop: Optional[str]) -> str:
        return model_base_url_for_shop(base_url, ai_shop)

    def _model_gateway_for_base_url(self, base_url: str) -> Dict[str, str]:
        return gateway_for_base_url(base_url)

    def _ai_shop_for_model_base_url(self, base_url: str) -> Optional[str]:
        return shop_from_model_base_url(base_url)

    def _same_url_host(self, left: str, right: str) -> bool:
        return same_url_host(left, right)

    def _select_primary_model_ref(
        self,
        supported_models: List[Dict[str, object]],
        preferred_model_ref: Optional[str] = None,
    ) -> str:
        supported_refs = {item["model_ref"] for item in supported_models}
        if preferred_model_ref and preferred_model_ref in supported_refs:
            return preferred_model_ref
        for model_id in self.PREFERRED_PRIMARY_MODEL_IDS:
            for item in supported_models:
                if str(item["id"]) == model_id:
                    return item["model_ref"]
            for item in supported_models:
                if self._model_id_for_matching(str(item["id"])) == model_id:
                    return item["model_ref"]
        return supported_models[0]["model_ref"]

    def _supported_model_sort_key(self, item: Dict[str, object]) -> tuple[int, int, str]:
        raw_model_id = str(item["id"])
        model_id = self._model_id_for_matching(raw_model_id)
        try:
            index = self.PREFERRED_PRIMARY_MODEL_IDS.index(model_id)
        except ValueError:
            index = len(self.PREFERRED_PRIMARY_MODEL_IDS)
        match_rank = 0 if raw_model_id == model_id else 1
        return (index, match_rank, raw_model_id)

    def _supported_models_from_config(self, config: Dict[str, object]) -> List[Dict[str, object]]:
        providers = config.get("models", {}).get("providers", {})
        if not isinstance(providers, dict):
            providers = {}
        excluded_media_refs = self._configured_media_model_refs(config)
        models: List[Dict[str, object]] = []
        for provider_key, provider in providers.items():
            provider_name = str(provider_key).strip()
            if not provider_name or not isinstance(provider, dict):
                continue
            definitions = provider.get("models", [])
            if not isinstance(definitions, list):
                continue
            for item in definitions:
                if not isinstance(item, dict):
                    continue
                model_id = item.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                model_ref = self._model_ref(provider_name, model_id)
                if model_ref in excluded_media_refs:
                    continue
                models.append(
                    {
                        "id": model_id,
                        "provider": provider_name,
                        "model_ref": model_ref,
                        "definition": item,
                    }
                )
        models.sort(key=self._supported_model_sort_key)
        return models

    def _configured_media_model_refs(self, config: Dict[str, object]) -> set[str]:
        defaults = config.get("agents", {}).get("defaults", {})
        if not isinstance(defaults, dict):
            return set()

        model_configs: List[object] = [
            defaults.get("imageGenerationModel"),
            defaults.get("videoGenerationModel"),
            defaults.get("musicGenerationModel"),
            defaults.get("voiceModel"),
        ]
        # Continue to understand beta/newer configs when listing models, even
        # though this stable-targeted writer no longer emits mediaModels.
        media_models = defaults.get("mediaModels", {})
        if isinstance(media_models, dict):
            model_configs.extend(media_models.values())

        refs: set[str] = set()
        for model_config in model_configs:
            if isinstance(model_config, str) and model_config.strip():
                refs.add(model_config.strip())
                continue
            if not isinstance(model_config, dict):
                continue
            primary = model_config.get("primary")
            if isinstance(primary, str) and primary.strip():
                refs.add(primary.strip())
            fallbacks = model_config.get("fallbacks")
            if isinstance(fallbacks, list):
                refs.update(
                    item.strip()
                    for item in fallbacks
                    if isinstance(item, str) and item.strip()
                )
        return refs

    def _supported_model_refs_from_config(self) -> List[str]:
        config = self._load_config()
        supported_refs = [item["model_ref"] for item in self._supported_models_from_config(config)]
        if not supported_refs:
            raise ValueError("No supported models configured")
        return supported_refs

    def _configured_default_model_from_config(self, config: Dict[str, object]) -> Optional[str]:
        defaults = config.get("agents", {}).get("defaults", {})
        model = defaults.get("model", {}) if isinstance(defaults, dict) else {}
        if isinstance(model, dict):
            primary = model.get("primary")
            if isinstance(primary, str) and primary.strip():
                return primary.strip()
        return None

    def _configured_model_api_key_from_config(self, config: Dict[str, object]) -> str:
        providers = config.get("models", {}).get("providers", {})
        if isinstance(providers, dict):
            for provider in providers.values():
                api_key = provider.get("apiKey") if isinstance(provider, dict) else None
                if isinstance(api_key, str) and api_key.strip():
                    return api_key.strip()
        raise ValueError("Configured model apiKey not found")

    def _configured_model_base_url_from_config(
        self,
        config: Dict[str, object],
        preferred_model_ref: Optional[str] = None,
    ) -> str:
        providers = config.get("models", {}).get("providers", {})
        if isinstance(providers, dict):
            preferred_provider = (preferred_model_ref or "").partition("/")[0]
            provider_names = [
                *([preferred_provider] if preferred_provider and preferred_provider != self.IMAGE_MODEL_PROVIDER else []),
                *(name for name in providers if name != self.IMAGE_MODEL_PROVIDER),
                *providers,
            ]
            for provider_name in dict.fromkeys(provider_names):
                provider = providers.get(provider_name)
                base_url = provider.get("baseUrl") if isinstance(provider, dict) else None
                if isinstance(base_url, str) and base_url.strip():
                    return base_url.strip()
        return self.MODEL_GATEWAYS[self.DEFAULT_MODEL_ENV]["base_url"]
