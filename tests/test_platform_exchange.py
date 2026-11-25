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

    def test_only_verified_instance_chat_gets_an_hour_at_open_and_renewal(self):
        from unittest.mock import patch
        from agent_manage.core.auth import LeaseAuthority
        from agent_manage.core.store import Store
        now = 100_000
        until = now + 3_600_000
        grant = self.redeem(PrivateSocket(actions=["chat.instance"], expiresAt=until))
        for marker, invalid in (("chat.instance", until + 1), ("chat.session", now + 300_001)):
            with self.subTest(marker=marker), self.assertRaises(APIError):
                self.redeem(PrivateSocket(actions=[marker], expiresAt=invalid))
        with tempfile.TemporaryDirectory() as directory:
            authority = LeaseAuthority(Store(Path(directory)), "manager")
            with patch("agent_manage.core.auth.now_ms", return_value=now):
                lease = authority.open_platform_lease(grant, "manager-agent")
                self.assertEqual(until, lease.public()["expiresAt"])
                self.assertEqual(["*"], lease.context["agentIds"])
                with self.assertRaises(APIError):
                    authority.issue_delegated_ticket({"issuer": "platform", "subject": "owner", "audience": "manager",
                        "runtimeIds": ["alpha"], "agentIds": ["*"], "actions": ["messages.list"], "authzVersion": 1,
                        "leaseSeconds": 3600})
            with patch("agent_manage.core.auth.now_ms", return_value=until - 1):
                new_until = until - 1 + 3_600_000
                with self.assertRaises(APIError):
                    authority.renew_platform_expiry(lease, new_until + 1)
                self.assertEqual(lease.token, authority.renew_platform_expiry(lease, new_until).token)
            with patch("agent_manage.core.auth.now_ms", return_value=new_until):
                with self.assertRaises(APIError):
                    authority.authenticate(lease.token)

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

    def test_refresh_cannot_change_actor_runtime_binding_or_permissions(self):
        from dataclasses import replace
        grant = self.redeem(PrivateSocket(actions=["chat.instance"]))
        self.assertTrue(grant.same_scope(replace(grant, agent_identify="another-entry", native_agent_id="other")))
        for field, value in (("subject", "another"), ("issuer", "other"), ("tenant_id", "other"),
                             ("runtime_id", "beta"), ("binding_id", "b"*64), ("instance_chat", False),
                             ("actions", ("messages.list",))):
            self.assertFalse(grant.same_scope(replace(grant, **{field: value})), field)
        narrow = self.redeem(PrivateSocket())
        self.assertFalse(narrow.same_scope(replace(narrow, installation_id="reinstalled")))

    def test_late_confirmation_cannot_extend_an_expired_or_revoked_lease(self):
        from unittest.mock import patch
        from agent_manage.core.auth import LeaseAuthority
        from agent_manage.core.store import Store
        with tempfile.TemporaryDirectory() as directory:
            authority = LeaseAuthority(Store(Path(directory)), "manager")
            for revoked in (False, True):
                with patch("agent_manage.core.auth.now_ms", return_value=1000):
                    token = authority.issue_ticket(issuer="p", subject="owner", runtime_ids=["one"], actions=["runs.get"], lease_seconds=1)
                    lease = authority.redeem(token)
                if revoked:
                    authority.revoke(lease.context["leaseId"])
                with patch("agent_manage.core.auth.now_ms", return_value=1999 if revoked else 2000):
                    with self.assertRaises(APIError):
                        authority.renew_platform_expiry(lease, 5000)
                    with self.assertRaises(APIError):
                        authority.authenticate(lease.token)


@unittest.skipUnless(importlib.util.find_spec("aiohttp"), "requires the server dependencies")
class PlatformReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_requires_current_binding_and_native_connection(self):
        from unittest.mock import AsyncMock, Mock
        from aiohttp import ClientSession, UnixConnector, web
        from agent_manage.core.service import Service
        from agent_manage.platform_runtime import PlatformRuntimePool
        from agent_manage.server import create_app

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / "mount/config/openclaw.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"gateway":{"controlUi":{"allowedOrigins":["https://old.agent.example"]}}}')
            pool = PlatformRuntimePool(root, "test", {"instanceIdentify": "alpha", "bindingId": "a" * 64,
                "mountRoot": str(root / "mount"), "gatewayOriginDomain": "agent.example"})
            connect = AsyncMock(side_effect=[APIError("UPSTREAM_UNAVAILABLE", "Not ready"), None])
            pool.runtime_service = AsyncMock(return_value=Mock(adapter=Mock(return_value=Mock(
                gateway=Mock(connect=connect)))))
            service = Service(root / "service", audience="test")
            runner = web.AppRunner(create_app(service, pool))
            await runner.setup()
            try:
                socket_path = str(root / "ready.sock")
                await web.UnixSite(runner, socket_path).start()
                async with ClientSession(connector=UnixConnector(path=socket_path)) as client:
                    url = "http://manager/v1/platform/readiness"
                    body = {"runtimeId": "alpha", "bindingId": "a" * 64}
                    async with client.post(url, json={**body, "bindingId": "b" * 64}) as response:
                        self.assertEqual(response.status, 404)
                    async with client.post(url, json=body) as response:
                        self.assertEqual(response.status, 503)
                        self.assertEqual((await response.json())["code"], "ORIGIN_MISMATCH")
                    pool.runtime_service.assert_not_awaited()
                    config.write_text('{"gateway":{"controlUi":{"allowedOrigins":["https://alpha.agent.example"]}}}')
                    async with client.post(url, json=body) as response:
                        self.assertEqual(response.status, 503)
                    async with client.post(url, json=body) as response:
                        self.assertEqual(response.status, 204)
                    self.assertEqual(connect.await_count, 2)
            finally:
                await runner.cleanup()

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
