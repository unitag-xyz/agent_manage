import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest

from agent_manage.core.errors import APIError
from agent_manage.platform_exchange import request_platform_grant


class PrivateSocket:
    def __init__(self, **changes):
        self.sent = []
        self.reply = {"type": "platform.grant", "runtimeId": "alpha", "bindingId": "a" * 64,
            "issuer": "platform", "subject": "owner", "tenantId": "owner", "agentIdentify": "template-alpha",
            "nativeAgentId": "template", "installationId": "installation-1", "actions": ["messages.list"],
            "expiresAt": 110_000, **changes}

    async def send_json(self, value):
        self.sent.append(value)

    async def receive_json(self):
        return self.reply


class PlatformExchangeTests(unittest.TestCase):
    def redeem(self, socket, frame=None):
        return asyncio.run(request_platform_grant(socket, "alpha", "a" * 64,
            {"type": "auth", "ticket": "b" * 64} if frame is None else frame, clock=lambda: 100))

    def test_manager_requests_redemption_without_repeating_browser_credentials(self):
        socket = PrivateSocket()
        grant = self.redeem(socket)
        self.assertEqual([{"type": "platform.redeem", "runtimeId": "alpha", "bindingId": "a" * 64}], socket.sent)
        self.assertEqual(("template-alpha", "template", "installation-1"),
            (grant.agent_identify, grant.native_agent_id, grant.installation_id))

    def test_manager_rejects_cross_instance_stale_or_unscoped_grants(self):
        for change in ({"runtimeId": "beta"}, {"bindingId": "c" * 64}, {"expiresAt": 99_999},
                       {"nativeAgentId": "../other"}, {"actions": ["*"]}, {"actions": ["runtimes.stop"]},
                       {"actions": ["skills.install"]}, {"actions": ["resources.get"]},
                       {"actions": ["chat.session", "threads.list"]},
                       {"actions": ["chat.instance", "chat.session"]}):
            with self.subTest(change=change), self.assertRaises(APIError):
                self.redeem(PrivateSocket(**change))

    def test_manager_expands_chat_session_only_to_its_local_chat_policy(self):
        grant = self.redeem(PrivateSocket(actions=["chat.session"]))
        self.assertEqual(("messages.list",), self.redeem(PrivateSocket()).actions)
        self.assertIn("runs.create", grant.actions)
        self.assertIn("threads.update", grant.actions)
        self.assertNotIn("threads.delete", grant.actions)
        self.assertNotIn("skills.install", grant.actions)
        self.assertNotIn("chat.session", grant.actions)
        self.assertNotIn("*", grant.actions)

    def test_instance_chat_grant_adds_sidebar_without_management_methods(self):
        instance = self.redeem(PrivateSocket(actions=["chat.instance"]))
        self.assertTrue(instance.instance_chat)
        self.assertIn("chat.sidebar", instance.actions)
        self.assertIn("threads.update", instance.actions)
        self.assertIn("threads.models.list", instance.actions)
        self.assertIn("threads.models.update", instance.actions)
        self.assertIn("usage.status", instance.actions)
        self.assertIn("operations.lookup", instance.actions)
        self.assertNotIn("models.select", instance.actions)
        self.assertNotIn("skills.install", instance.actions)
        agent = self.redeem(PrivateSocket(actions=["chat.session"]))
        self.assertFalse(agent.instance_chat)
        self.assertNotIn("chat.sidebar", agent.actions)
        self.assertNotIn("threads.models.update", agent.actions)
        self.assertNotIn("usage.status", agent.actions)

    def test_only_valid_instance_grants_carry_optional_template_metadata(self):
        instance = self.redeem(PrivateSocket(actions=["chat.instance"], templateIdentify="catalog-template"))
        self.assertEqual("catalog-template", instance.template_identify)
        for actions, template in ((["chat.instance"], "../wrong"), (["chat.session"], "catalog-template"),
                                  (["chat.instance"], None)):
            with self.subTest(actions=actions, template=template):
                self.assertIsNone(self.redeem(PrivateSocket(actions=actions, templateIdentify=template)).template_identify)

    def test_extensions_are_inert_except_for_known_presentation_fields(self):
        grant = self.redeem(PrivateSocket(actions=["chat.instance"], extensions={
            "actions": ["*"], "runtimeId": "other", "unknown": [1, True],
            "presentation": {"templateIdentify": "trusted-template", "future": "ignored"}}))
        self.assertEqual("trusted-template", grant.template_identify)
        self.assertEqual("alpha", grant.runtime_id)
        self.assertNotIn("*", grant.actions)
        self.assertNotIn("skills.install", grant.actions)

    def test_attached_installation_hints_are_scoped_to_instance_and_fail_closed_on_duplicates(self):
        row = {"nativeAgentId": "attach-one", "platformAgentIdentify": "platform-one",
               "installationId": "installation-1"}
        two = {"nativeAgentId": "attach-two", "platformAgentIdentify": "platform-two",
               "installationId": "installation-2"}
        data = {"attachedInstallations": [row, {**row, "nativeAgentId": "ATTACH-ONE"}, two, {"nativeAgentId": "broken"}]}
        instance = self.redeem(PrivateSocket(actions=["chat.instance"], extensions=data))
        self.assertEqual((("attach-two", "platform-two", "installation-2"),), instance.attached_installations)
        self.assertEqual((), self.redeem(PrivateSocket(actions=["chat.session"], extensions=data)).attached_installations)

    def test_rejects_oversized_too_deep_or_wrong_shape_extensions(self):
        deep = {}
        for _ in range(8):
            deep = {"nested": deep}
        for extensions in ([], {"data": "x" * 4096}, deep):
            with self.subTest(extensions=type(extensions)), self.assertRaises(APIError):
                self.redeem(PrivateSocket(actions=["chat.instance"], extensions=extensions))

    def test_browser_cannot_supply_host_binding_in_auth_frame(self):
        socket = PrivateSocket()
        with self.assertRaises(APIError):
            self.redeem(socket, {"type": "auth", "ticket": "b" * 64, "runtimeId": "beta"})
        self.assertEqual([], socket.sent)


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "requires the server dependencies")
class PlatformReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_probe_requires_current_binding_and_returns_loaded_release(self):
        from aiohttp import ClientSession, UnixConnector, web
        from agent_manage.server import create_app
        from agent_manage.core.service import Service

        class Pool:
            def admits(self, runtime, binding):
                return (runtime, binding) == ("alpha", "a" * 64)

            async def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), audience="test")
            report = {"version": "1.0.002", "sourceRoot": "/opt/unitag-agent-manager/releases/loaded"}
            socket_path = str(Path(directory) / "release.sock")
            runner = web.AppRunner(create_app(service, Pool(), report))
            await runner.setup()
            try:
                await web.UnixSite(runner, socket_path).start()
                async with ClientSession(connector=UnixConnector(path=socket_path)) as client:
                    url = "http://manager/v1/platform/release"
                    async with client.post(url, json={"runtimeId": "alpha", "bindingId": "b" * 64}) as stale:
                        self.assertEqual(stale.status, 404)
                    async with client.post(url, json={"runtimeId": "alpha", "bindingId": "a" * 64}) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(await response.json(), report)
            finally:
                await runner.cleanup()
