import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_manage.core.errors import APIError
from agent_manage.core.service import Service
from agent_manage.platform_runtime import PlatformRuntimePool


class PlatformEngineBoundaryTests(unittest.TestCase):
    def test_generic_context_does_not_add_openclaw_routing_variables(self):
        from agent_manage.core.runtime import RuntimeContext
        root = Path("/fixture")
        context = RuntimeContext("test", "test", root / "config", root, executable="fixture", home=root)
        self.assertEqual(context.environment()["HOME"], str(root))
        self.assertFalse(any(key.startswith("OPENCLAW_") for key in context.environment()))

    def test_legacy_profile_and_explicit_openclaw_register_identically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            profile = {
                "instanceIdentify": "alpha",
                "bindingId": "a" * 64,
                "mountRoot": str(root / "instance"),
                "gatewayOrigin": "https://alpha.example",
            }
            legacy = PlatformRuntimePool(root, "test", profile)
            explicit = PlatformRuntimePool(root, "test", {**profile, "engine": "openclaw"})
            self.assertEqual(legacy.registration(), explicit.registration())
            self.assertEqual(explicit.registration()["engine"], "openclaw")
            self.assertEqual(explicit.registration()["hostBinding"]["bindingId"], "a" * 64)
            for unsupported in ("dsh", "", None, []):
                with self.subTest(engine=unsupported), self.assertRaises(ValueError):
                    PlatformRuntimePool(root, "test", {**profile, "engine": unsupported})


class NativeRunOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_another_adapter_cannot_skip_ownership_via_openclaw_source(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory).resolve() / "manager")
            try:
                service.adapters["one"] = SimpleNamespace(
                    instance_chat=True,
                    resolve_scope=lambda method, params: "agent-one", close=AsyncMock())
                lease = service.auth.redeem(service.auth.issue_ticket(
                    issuer="test", subject="owner", runtime_ids=["one"], agent_ids=["agent-one"],
                    actions=["runs.cancel"]))
                run_id = service.store.object("run", "one", "observed", {"source": "openclaw", "threadId": "thread"})
                with self.assertRaises(APIError) as denied:
                    service.authorize(lease, "runs.cancel", {"runtimeId": "one", "runId": run_id})
                self.assertEqual(denied.exception.code, "FORBIDDEN")
            finally:
                await service.close()
