"""Boundaries around the retained 0.x application services, not CLI changes."""
from contextlib import nullcontext
from functools import wraps

from ...core.runtime import RuntimeContext, CURRENT_WRITE_GUARD
from ...core.transactions import runtime_write_lock

WRITES = frozenset({
    "create_instance", "configure_instance", "add_agent", "add_agents", "add_skill",
    "refresh_agent", "add_tg_bot", "delete_tg_bot", "add_feishu_bot", "delete_feishu_bot",
    "add_weixin_bot", "delete_weixin_bot", "set_model", "update_model_catalog",
    "install_agent_service", "start_agent_services", "_codex_configure_models", "_codex_restore_models",
    "activate_instance", "codex_login", "codex_logout",
})


def install_boundaries(cls):
    names = set(WRITES)
    for base in cls.__mro__:
        names.update(name for name, value in vars(base).items()
                     if not name.startswith("_") and callable(value) and not isinstance(value, (staticmethod, classmethod)))
    for name in names:
        method = getattr(cls, name, None)
        if method is None:
            continue

        def wrap(method, name):
            @wraps(method)
            def scoped(self, *args, **kwargs):
                explicit = getattr(self, "runtime_context", None)
                context = explicit or RuntimeContext(str(self.config_path), str(self.config_path.parent),
                                                     self.config_path, self.config_path.parent, self.bin,
                                                     lifecycle="legacy")
                # Fly activation operates on a staged config while keeping the real state directory.
                if context.config_path != self.config_path:
                    from dataclasses import replace
                    context = replace(context, config_path=self.config_path)
                write = name in WRITES and not self.runner.dry_run and hasattr(self.runner, "run")
                with context.activate(), runtime_write_lock(context.state_dir, timeout=0 if name == "activate_instance" else 30) if write else nullcontext():
                    if write:
                        guard = CURRENT_WRITE_GUARD.get()
                        if guard:
                            guard()
                        from .recovery import recover_refreshes
                        recover_refreshes(context.config_path)
                    return method(self, *args, **kwargs)
            return scoped
        setattr(cls, name, wrap(method, name))
    return cls
