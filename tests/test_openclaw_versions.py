import unittest

from agent_manage.adapters.openclaw.version import FAMILIES, implementation
from agent_manage.adapters.versions import select_family


class OpenClawVersionTests(unittest.TestCase):
    def test_compatibility_is_per_capability_and_fail_closed(self):
        for capability in ("runtime", "history", "codex_auth"):
            for version in ("2026.7.1", "2026.7.1-2"):
                self.assertEqual(implementation(version, capability), "v2026_7")
            for version in ("2026.7.0", "2026.8.1", "2026.7.1-beta.1", "unknown"):
                self.assertIsNone(implementation(version, capability))
        self.assertEqual(implementation("2026.7.1", "host_reload"), "v2026_7")
        self.assertIsNone(implementation("2026.7.1-2", "host_reload"))

    def test_ambiguous_rules_never_choose_by_order(self):
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            select_family((*FAMILIES, FAMILIES[0]), "2026.7.1", "history")
