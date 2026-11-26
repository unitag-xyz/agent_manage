"""Reversible model configuration and environment-wide Codex OAuth."""

from __future__ import annotations

import json
import re
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from subprocess import TimeoutExpired

from .file_io import atomic_write_bytes as _atomic_bytes, write_private_json as _private_json
from .codex_auth import CodexAuthMixin


class CodexManagementMixin(CodexAuthMixin):
    CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
    CODEX_API = "openai-chatgpt-responses"
    # Official Codex models as of 2026-10-10. This is a configuration list,
    # not a claim about the signed-in account's model permissions.
    # https://learn.chatgpt.com/docs/models
    CODEX_DEFAULT_MODEL = "gpt-6.1-sol"
    CODEX_CONTEXT_WINDOW = 258_000
    CODEX_IMAGE_MODEL = "openai/gpt-image-2"
    CODEX_MODELS = (
        ("gpt-6.1-sol", "GPT-6.1 Sol"),
        ("gpt-6-astra", "GPT-6 Astra"),
        ("gpt-6-sol", "GPT-6 Sol"),
        ("gpt-6-luna", "GPT-6 Luna"),
    )

    def _codex_state_path(self):
        return self.config_path.parent / "agent-manage/codex-login.json"

    def _codex_read_state(self):
        path = self._codex_state_path()
        if path.is_symlink():
            raise ValueError("Codex model restore record must not be a link")
        state = json.loads(path.read_text()) if path.exists() else {}
        if state and state.get("config_path") != str(self.config_path):
            raise ValueError("Codex model restore record belongs to another environment")
        return state

    def _codex_models_active(self):
        return bool(self._codex_read_state().get("restore"))

    def _codex_chat_fields(self, target):
        for parts in (("model",), ("utilityModel",), ("heartbeat", "model"),
                      ("subagents", "model"), ("compaction", "model")):
            parent = target
            for part in parts[:-1]:
                parent = parent.get(part) if isinstance(parent, dict) else None
            if not isinstance(parent, dict) or (len(parts) > 1 and parts[-1] not in parent):
                continue
            if parts == ("utilityModel",) and "utilityModel" not in parent:
                continue
            yield parent, parts

    def _codex_switch_chat_fields(self, target, model):
        saved = []
        for parent, parts in self._codex_chat_fields(target):
            saved.append({"path": list(parts), "present": parts[-1] in parent,
                          "value": deepcopy(parent.get(parts[-1]))})
            parent[parts[-1]] = {"primary": model, "fallbacks": []} if parts == ("model",) else model
        return saved

    def _codex_restore_chat_fields(self, target, fields):
        for field in fields:
            parent = target
            for part in field["path"][:-1]:
                parent = parent.setdefault(part, {})
            if field["present"]:
                parent[field["path"][-1]] = field["value"]
            else:
                parent.pop(field["path"][-1], None)

    def _codex_model_definitions(self):
        return [self._codex_chat_model(model_id, name, ["text", "image"])
                for model_id, name in self.CODEX_MODELS]

    def _codex_chat_model(self, model_id, name, inputs):
        return {"id": model_id, "name": name, "api": self.CODEX_API,
                "baseUrl": self.CODEX_BASE_URL, "agentRuntime": {"id": "openclaw"},
                "input": inputs, "reasoning": True, "contextWindow": self.CODEX_CONTEXT_WINDOW}

    def _codex_configured_chat_models(self, config):
        provider = config.get("models", {}).get("providers", {}).get("openai", {})
        media_refs = self._configured_media_model_refs(config) | {self.CODEX_IMAGE_MODEL}
        return [row for row in provider.get("models", [])
                if row.get("api") == self.CODEX_API
                and "openai/" + row["id"] not in media_refs]

    def _refresh_codex_model_candidate(self, config, state):
        current = self._configured_default_model_from_config(config)
        status = {"scope": "environment", "source": "codex", "skipped": True,
                  "current_model_before": current, "current_model_after": current}
        if state.get("status") != "active":
            return config, {**status, "reason": "codex_login_incomplete"}
        if self.runner.dry_run:
            return config, {**status, "reason": "codex_catalog_dry_run"}
        previous = self._codex_configured_chat_models(config)
        if not previous:
            return config, {**status, "reason": "codex_catalog_not_configured"}
        try:
            catalog = self._codex_bridge("list-models")
        except TimeoutExpired:
            catalog = {"catalog_error": {"code": "timeout"}}
        except (OSError, RuntimeError, ValueError):
            catalog = {"catalog_error": {"code": "bridge_failed"}}
        if not isinstance(catalog, dict):
            catalog = {"catalog_error": {"code": "invalid_response"}}
        error = catalog.get("catalog_error")
        if isinstance(error, dict):
            code = error.get("code")
            if code not in ("sdk_unavailable", "oauth_unavailable", "network_error", "timeout", "http_error",
                            "invalid_response", "response_too_large", "bridge_failed"):
                code = "bridge_failed"
            details = {"code": code}
            http_status = error.get("http_status")
            if code == "http_error" and type(http_status) is int and 300 <= http_status <= 599:
                details["http_status"] = http_status
            return config, {**status, "reason": "codex_catalog_unavailable", "catalog_error": details}
        rows = catalog.get("models")
        models = {}
        media_refs = self._configured_media_model_refs(config) | {self.CODEX_IMAGE_MODEL}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id")
            if not isinstance(model_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", model_id):
                continue
            ref = "openai/" + model_id
            if ref in media_refs or ref in models:
                continue
            inputs = row.get("input")
            if isinstance(inputs, list) and inputs and "text" not in inputs:
                continue
            modalities = ["text"]
            if not isinstance(inputs, list) or not inputs or "image" in inputs or "vision" in inputs:
                modalities.append("image")
            name = row.get("name")
            name = " ".join(name.split())[:160] if isinstance(name, str) else ""
            models[ref] = self._codex_chat_model(model_id, name or model_id, modalities)
        if not models:
            return config, {**status, "reason": "codex_catalog_empty"}

        candidate = deepcopy(config)
        defaults = candidate["agents"]["defaults"]
        targets = [defaults, *candidate["agents"].get("list", [])]
        selected = set()
        for target in targets:
            for parent, parts in self._codex_chat_fields(target):
                value = parent.get(parts[-1])
                if isinstance(value, dict):
                    fallbacks = value.get("fallbacks")
                    values = [value.get("primary"), *(fallbacks if isinstance(fallbacks, list) else [])]
                else:
                    values = [value]
                selected.update(ref for ref in values if isinstance(ref, str))
        old_refs = {"openai/" + row["id"] for row in previous}
        retained = []
        for row in previous:
            ref = "openai/" + row["id"]
            if ref in selected and ref not in models:
                models[ref] = deepcopy(row)
                retained.append(ref)
        provider = candidate["models"]["providers"]["openai"]
        other_models = [row for row in provider["models"] if "openai/" + row["id"] not in (old_refs | models.keys())]
        provider["models"] = [*models.values(), *other_models]
        for target in targets:
            allowlist = target.get("models")
            if isinstance(allowlist, dict):
                if target is defaults or old_refs <= allowlist.keys():
                    for ref in models:
                        allowlist.setdefault(ref, {})
                for ref in old_refs - models.keys():
                    allowlist.pop(ref, None)
        return candidate, {**status, "skipped": False, "model_count": len(models),
                           "supported_model_refs": list(models), "retained_model_refs": retained}

    def _codex_validate(self, config):
        with tempfile.TemporaryDirectory(prefix="agent-manage-codex-config-") as directory:
            path = Path(directory) / "openclaw.json"
            _private_json(path, config)
            return self.runner.run([self.bin, "config", "validate"], timeout=self.OPENCLAW_COMMAND_TIMEOUT_SECONDS,
                                   env_overrides={"OPENCLAW_CONFIG_PATH": str(path), "OPENCLAW_STATE_DIR": str(self.config_path.parent)})

    def _codex_result(self, state, *, preview=False):
        return {"ok": True, "status": "preview" if preview else "configured", "scope": "environment",
                "mode": "models_only", "skipped": preview, "model": state["selected_model"],
                "models": state["models"], "supported_model_refs": ["openai/" + row["id"] for row in state["models"]],
                "provider_mode": state["provider_mode"], "switched_agents": state["switched_agents"],
                "media": {"image_generation_switched": bool(state.get("restore", {}).get("media")),
                          "image_model": self.CODEX_IMAGE_MODEL if state.get("restore", {}).get("media") else None,
                          "audio_switched": False, "audio_reason": "server_openclaw_requires_api_key"},
                "restart_required": False, "gateway_restarted": False, "activation_verified": False}

    def _codex_configure_models(self):
        with self._refresh_lock(self.config_path.parent / "agent-manage"):
            return self._codex_configure_models_locked()

    def _codex_configure_models_locked(self):
        selected = self.CODEX_DEFAULT_MODEL
        state = self._codex_read_state()
        previous_state = self._codex_state_path().read_bytes() if state else None
        original = self.config_path.read_bytes()
        original_mode = stat.S_IMODE(self.config_path.stat().st_mode)
        config = self._load_config()
        if state.get("restore"):
            if state["status"] != "active":
                raise FileExistsError("Codex model switching was interrupted; run codex-logout to restore")
            state["selected_model"] = self._load_config()["agents"]["defaults"]["model"]["primary"]
            models = deepcopy(self._codex_configured_chat_models(config) or state["models"])
            for model in models:
                model["contextWindow"] = self.CODEX_CONTEXT_WINDOW
            candidate = deepcopy(config)
            if state["restore"].get("media") and state["restore"].get("catalog"):
                # Update model metadata without rebuilding the catalog restore backup.
                refs = {model["id"] for model in models}
                for model in candidate.get("models", {}).get("providers", {}).get("openai", {}).get("models", []):
                    if model.get("id") in refs:
                        model["contextWindow"] = self.CODEX_CONTEXT_WINDOW
                if candidate == config and models == state["models"]:
                    return self._codex_result(state, preview=self.runner.dry_run)
            else:
                # Upgrade an already active login without replacing its original restore record.
                if not state["restore"].get("media"):
                    state["restore"]["media"] = self._codex_configure_media(candidate, state["restore"].get("provider"))
                self._codex_clean_catalog(candidate, state["restore"], models)
            state["models"] = models
            state["status"] = "applying"
        else:
            models = self._codex_model_definitions()
            candidate, restore, hybrid = self._codex_model_candidate(config, models, selected)
            state = {"config_path": str(self.config_path), "status": "applying", "restore": restore,
                     "selected_model": "openai/" + selected, "models": models,
                     "switched_agents": [agent["id"] for agent in restore["agents"]],
                     "provider_mode": "mixed_api_and_codex" if hybrid else "codex"}
        if self.runner.dry_run:
            return self._codex_result(state, preview=True)
        self._codex_validate(candidate)
        if self.config_path.read_bytes() != original:
            raise RuntimeError("Config changed during Codex model switch; retry")
        # Persist the original models first, so an interrupted write is recoverable.
        _private_json(self._codex_state_path(), state)
        try:
            if self.config_path.read_bytes() != original:
                raise RuntimeError("Config changed during Codex model switch; retry")
            _private_json(self.config_path, candidate)
            state["status"] = "active"
            _private_json(self._codex_state_path(), state)
        except Exception:
            encoded = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
            if self.config_path.read_bytes() == encoded:
                _atomic_bytes(self.config_path, original, original_mode)
            if previous_state is None:
                self._codex_state_path().unlink()
            else:
                _atomic_bytes(self._codex_state_path(), previous_state)
            raise
        return self._codex_result(state)

    def _codex_model_candidate(self, config, models, selected):
        candidate = deepcopy(config)
        providers = candidate.setdefault("models", {}).setdefault("providers", {})
        old_provider = deepcopy(providers.get("openai"))
        refs = {"openai/" + row["id"] for row in models}
        hybrid = bool(old_provider and old_provider.get("apiKey") is not None and old_provider.get("api") != self.CODEX_API)
        if hybrid:
            provider = deepcopy(old_provider)
            provider.update(auth="oauth", models=models)
        else:
            if old_provider and old_provider.get("api") != self.CODEX_API:
                raise FileExistsError("Existing OpenAI API provider cannot be safely switched while preserving media routes")
            provider = {**(old_provider or {}), "baseUrl": self.CODEX_BASE_URL, "api": self.CODEX_API,
                        "auth": "oauth", "agentRuntime": {"id": "openclaw"}, "models": models}
            provider.pop("apiKey", None)
        providers["openai"] = provider
        defaults = candidate.setdefault("agents", {}).setdefault("defaults", {})
        old_allowlist = deepcopy(defaults.get("models"))
        defaults["models"] = {ref: {} for ref in sorted(refs)}
        changed_agents = []
        for agent in candidate["agents"].get("list", []):
            changed_agents.append({"id": agent["id"], "models_present": "models" in agent,
                                   "models": deepcopy(agent.get("models")),
                                   "chat_fields": self._codex_switch_chat_fields(agent, "openai/" + selected)})
            if "models" in agent:
                agent["models"] = {ref: {} for ref in sorted(refs)}
        restore = {"provider_present": old_provider is not None, "provider": old_provider,
                   "allowlist": old_allowlist, "added_refs": sorted(refs), "agents": changed_agents,
                   "default_fields": self._codex_switch_chat_fields(defaults, "openai/" + selected)}
        restore["media"] = self._codex_configure_media(candidate, old_provider)
        self._codex_clean_catalog(candidate, restore, models)
        return candidate, restore, hybrid

    def _codex_clean_catalog(self, config, restore, models):
        catalog = config["models"]
        providers = catalog["providers"]
        # Non-OpenAI providers were untouched by previous login versions. Rebuild
        # the complete original catalog with the first login's OpenAI backup.
        original_providers = deepcopy(providers)
        if restore["provider_present"]:
            original_providers["openai"] = deepcopy(restore["provider"])
        else:
            original_providers.pop("openai", None)
        restore["catalog"] = {"providers": original_providers,
                              "mode_present": "mode" in catalog, "mode": catalog.get("mode")}
        media = config.get("tools", {}).get("media", {})
        audio = media.get("audio", {})
        entries = list(audio.get("models", [])) + [entry for entry in media.get("models", [])
                    if not entry.get("capabilities") or "audio" in entry["capabilities"]]
        audio_providers = {entry.get("provider") for entry in entries if entry.get("type", "provider") == "provider"}
        tts_provider = config.get("messages", {}).get("tts", {}).get("provider")
        if tts_provider:
            audio_providers.add(tts_provider)
        openai = providers["openai"]
        openai["models"] = deepcopy(models) + [{"id": "gpt-image-2", "name": "GPT Image 2", "api": self.CODEX_API,
                                               "baseUrl": self.CODEX_BASE_URL, "agentRuntime": {"id": "openclaw"},
                                               "input": ["text", "image"]}]
        # Retain only explicitly used audio connections, without chat/image catalogs.
        catalog["providers"] = {provider: {**deepcopy(value), "models": []}
                                for provider, value in providers.items()
                                if provider != "openai" and provider in audio_providers}
        catalog["providers"]["openai"] = openai
        catalog["mode"] = "replace"
        restore["catalog"]["applied_providers"] = list(catalog["providers"])
        config["agents"]["defaults"]["models"] = {"openai/" + row["id"]: {} for row in openai["models"]}
        for agent in config["agents"].get("list", []):
            if "models" in agent:
                agent["models"] = {"openai/" + row["id"]: {} for row in models}

    def _codex_configure_media(self, config, old_provider):
        defaults = config["agents"]["defaults"]
        saved = {"image_present": "imageGenerationModel" in defaults,
                 "image": deepcopy(defaults.get("imageGenerationModel")),
                 "media_present": "media" in config.get("tools", {}),
                 "media": deepcopy(config.get("tools", {}).get("media")),
                 "tools_present": "tools" in config}
        image = defaults.get("imageGenerationModel")
        defaults["imageGenerationModel"] = {**(image if isinstance(image, dict) else {}),
                                            "primary": self.CODEX_IMAGE_MODEL, "fallbacks": []}
        defaults.setdefault("models", {})[self.CODEX_IMAGE_MODEL] = {}
        provider = config["models"]["providers"]["openai"]
        # The image plugin chooses subscription transport from the provider defaults,
        # even when a retained API key is also needed by the old audio plugin.
        provider.update(baseUrl=self.CODEX_BASE_URL, api=self.CODEX_API, auth="oauth", agentRuntime={"id": "openclaw"})
        # Old server auth rejects OAuth for audio/transcriptions. Keep existing
        # explicit audio routes and pin OpenAI entries to their original endpoint.
        if old_provider and old_provider.get("api") != self.CODEX_API:
            media = config.setdefault("tools", {}).setdefault("media", {})
            audio = media.get("audio", {})
            audio_entries = list(audio.get("models", []))
            shared_entries = [entry for entry in media.get("models", [])
                              if not entry.get("capabilities") or "audio" in entry["capabilities"]]
            if not audio_entries and not shared_entries and old_provider.get("apiKey") is not None:
                audio = media.setdefault("audio", {})
                audio["models"] = [{"provider": "openai", "model": "gpt-4o-transcribe"}]
                audio_entries = audio["models"]
            for entry in audio_entries + shared_entries:
                if entry.get("type", "provider") == "provider" and entry.get("provider") == "openai":
                    entry.setdefault("baseUrl", audio.get("baseUrl") or old_provider.get("baseUrl") or "https://api.openai.com/v1")
        saved["applied_image"] = deepcopy(defaults["imageGenerationModel"])
        saved["applied_media"] = deepcopy(config.get("tools", {}).get("media"))
        return saved

    def _codex_restore_media_value(self, before, applied, current):
        # Restore managed values while preserving later user edits such as image timeouts.
        if current == applied:
            return deepcopy(before)
        if isinstance(applied, dict) and isinstance(current, dict):
            if before is None:
                before = {}
            elif isinstance(before, str):
                before = {"primary": before}
        if all(isinstance(value, dict) for value in (before, applied, current)):
            result = deepcopy(current)
            for key in before.keys() | applied.keys():
                if key not in before:
                    if key in current:
                        restored = self._codex_restore_media_value(None, applied[key], current[key])
                        if restored is None or restored == {}:
                            result.pop(key)
                        else:
                            result[key] = restored
                elif key not in applied:
                    if key not in current:
                        result[key] = deepcopy(before[key])
                elif key in current:
                    result[key] = self._codex_restore_media_value(before[key], applied[key], current[key])
            return result
        return deepcopy(current)

    def _codex_restore_models(self):
        with self._refresh_lock(self.config_path.parent / "agent-manage"):
            return self._codex_restore_models_locked()

    def _codex_restore_models_locked(self):
        state = self._codex_read_state()
        result = {"ok": True, "scope": "environment", "status": "restored", "mode": "models_only",
                  "skipped": self.runner.dry_run, "models_restored": False,
                  "restart_required": False, "gateway_restarted": False, "activation_verified": False}
        if not state or self.runner.dry_run:
            return result
        original = self.config_path.read_bytes()
        config = self._load_config()
        restore = state["restore"]
        codex_refs = set(restore["added_refs"]) | {
            "openai/" + row["id"] for row in self._codex_configured_chat_models(config)
        }
        providers = config.setdefault("models", {}).setdefault("providers", {})
        if restore.get("catalog"):
            catalog = restore["catalog"]
            additions = {provider: value for provider, value in providers.items()
                         if provider not in catalog["providers"] and provider not in catalog["applied_providers"]}
            config["models"]["providers"] = {**additions, **deepcopy(catalog["providers"])}
            if catalog["mode_present"]:
                config["models"]["mode"] = catalog["mode"]
            else:
                config["models"].pop("mode", None)
        elif restore["provider_present"]:
            providers["openai"] = restore["provider"]
        else:
            providers.pop("openai", None)
        defaults = config.setdefault("agents", {}).setdefault("defaults", {})
        if restore["allowlist"] is None:
            defaults.pop("models", None)
        else:
            defaults["models"] = restore["allowlist"]
        self._codex_restore_chat_fields(defaults, restore["default_fields"])
        media = restore.get("media")
        if media:
            for parent, key, present, before, applied in (
                (defaults, "imageGenerationModel", media["image_present"], media["image"], media["applied_image"]),
                (config.get("tools", {}), "media", media["media_present"], media["media"], media["applied_media"]),
            ):
                if key not in parent:
                    continue
                if not present and parent[key] == applied:
                    parent.pop(key)
                else:
                    parent[key] = self._codex_restore_media_value(before, applied, parent[key])
            if not config.get("tools") and not media["tools_present"]:
                config.pop("tools", None)
        for saved in restore["agents"]:
            agent = next((item for item in config["agents"].get("list", []) if item.get("id") == saved["id"]), None)
            if agent is not None:
                self._codex_restore_chat_fields(agent, saved["chat_fields"])
                if saved["models_present"]:
                    agent["models"] = saved["models"]
                else:
                    agent.pop("models", None)
        # New agents inherit restored defaults rather than dangling Codex selectors.
        saved_ids = {saved["id"] for saved in restore["agents"]}
        for agent in config["agents"].get("list", []):
            if agent.get("id") in saved_ids:
                continue
            for field in self._codex_switch_chat_fields(deepcopy(agent), state["selected_model"]):
                value = field["value"]
                selected = value.get("primary") if isinstance(value, dict) else value
                if selected in codex_refs:
                    self._codex_restore_chat_fields(agent, [{**field, "present": False}])
                elif isinstance(value, dict) and isinstance(value.get("fallbacks"), list):
                    cleaned = {**value, "fallbacks": [ref for ref in value["fallbacks"] if ref not in codex_refs]}
                    self._codex_restore_chat_fields(agent, [{**field, "value": cleaned}])
            if "models" in agent:
                agent["models"] = {key: value for key, value in agent["models"].items() if key not in codex_refs}
                if not agent["models"]:
                    agent.pop("models")
        self._codex_validate(config)
        if self.config_path.read_bytes() != original:
            raise RuntimeError("Config changed during Codex model restore; retry")
        state["status"] = "restoring"
        _private_json(self._codex_state_path(), state)
        _private_json(self.config_path, config)
        self._codex_state_path().unlink()
        result.update(models_restored=True, restored_agents=[agent["id"] for agent in restore["agents"]])
        return result
