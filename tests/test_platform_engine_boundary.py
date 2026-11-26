import tempfile
import unittest
from pathlib import Path

from agent_manage.platform_runtime import PlatformRuntimePool


class PlatformEngineBoundaryTests(unittest.TestCase):
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
