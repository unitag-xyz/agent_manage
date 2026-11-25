"""Optional v1 HTTP/WSS host: python -m agent_manage.server --config server.json.

The existing agent-manage CLI entry point and protocol remain unchanged.
"""
import argparse
import asyncio
import base64
import json
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import web, WSMsgType

from . import __version__
from .core.errors import APIError
from .core.service import MAX_ATTACHMENT_REQUEST, Service
from .core.store import canonical
from .adapters.registry import create_runtime_adapter

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
SERVICE = web.AppKey("service", Service)


def build_service(config):
    obsolete = {"ticketIssuer", "listenHost", "listenPort", "allowedOrigins"} & config.keys()
    if obsolete:
        raise ValueError("Manager v1 uses the private Server API socket; remove: " + ", ".join(sorted(obsolete)))
    service = Service(Path(config["stateDirectory"]).expanduser().resolve(), audience=config.get("audience", "agent-manager"))
    for runtime in config.get("runtimes", []):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", runtime["id"]):
            raise ValueError("Invalid runtime id")
        environment = runtime.get("environment", {})
        if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()):
            raise ValueError("Runtime environment must contain string values")
        service.register(create_runtime_adapter(runtime, service.store, service.read_workers, service.publish))
    return service


def create_app(service, platform_pool=None, platform_release=None):
    from .platform_runtime import SharedPlatformRuntimePool
    shared = isinstance(platform_pool, SharedPlatformRuntimePool)
    http_slots, media_slots, websocket_slots = asyncio.Semaphore(8), asyncio.Semaphore(4), asyncio.Semaphore(64)

    @asynccontextmanager
    async def admitted(slots):
        if slots.locked():
            raise APIError("BUSY", "Transport capacity reached", retryable=True)
        await slots.acquire()
        try:
            yield
        finally:
            slots.release()

    @web.middleware
    async def boundary(request, handler):
        try:
            transport_socket = request.transport.get_extra_info("socket") if request.transport else None
            if transport_socket is None or transport_socket.family != socket.AF_UNIX or "Origin" in request.headers:
                raise APIError("FORBIDDEN", "Manager requires a private Server API connection")
            response = await handler(request)
        except APIError as exc:
            response = web.json_response({"ok": False, "error": exc.payload()}, status=401 if exc.code == "UNAUTHENTICATED" else 403 if exc.code == "FORBIDDEN" else 400)
        except (json.JSONDecodeError, TypeError, KeyError):
            response = web.json_response({"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "Invalid request body"}}, status=400)
        if not response.prepared:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app = web.Application(middlewares=[boundary], client_max_size=MAX_REQUEST)
    app[SERVICE] = service

    def bearer(request):
        value = request.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise APIError("UNAUTHENTICATED", "A scoped bearer lease is required")
        return service.auth.authenticate(value[7:])

    async def issue_ticket(request):
        ticket = service.auth.issue_delegated_ticket(await request.json())
        return web.json_response({"ok": True, "ticket": ticket})

    async def resolve_agent(request):
        async with admitted(http_slots):
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"runtimeId", "agentIdentify"} or any(
                    not isinstance(body[key], str) or not body[key] for key in body):
                raise APIError("INVALID_ARGUMENT", "runtimeId and agentIdentify are required")
            adapter = service.adapter(body["runtimeId"])
            resolver = getattr(adapter, "resolve_agent", None)
            if resolver is None:
                raise APIError("UNSUPPORTED", "Runtime does not expose trusted Agent bindings")
            identifier = await service.read_workers(resolver, body["agentIdentify"])
            row = service.store.get(identifier, kind="agent", runtime=adapter.runtime_id)
            return web.json_response({"ok": True, "result": {"runtimeId": adapter.runtime_id,
                "agentIdentify": body["agentIdentify"], "agentId": identifier,
                "bindingId": adapter.host_binding.binding_id,
                "installationId": row["data"]["installationId"]}})

    async def redeem(request):
        body = await request.json()
        lease = service.auth.redeem(body["ticket"])
        return web.json_response({"ok": True, "leaseToken": lease.token, "authContext": lease.public()})

    async def rpc(request):
        async with admitted(http_slots):
            lease = bearer(request)
            body = await request.json()
            response = await service.dispatch(lease, body)
            encoded = canonical(response).encode()
            if len(encoded) > MAX_RESPONSE:
                raise APIError("RESPONSE_TOO_LARGE", "Response exceeds transport budget; use a smaller page")
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            response = web.Response(body=encoded, content_type="application/json", headers=headers)
            await response.prepare(request)
            await asyncio.wait_for(response.write_eof(), 10)
            return response

    async def websocket(request):
        async with admitted(websocket_slots):
            return await websocket_session(request)

    async def platform_websocket(request):
        async with admitted(websocket_slots):
            return await websocket_session(request, platform=True)

    async def platform_readiness(request):
        async with admitted(http_slots):
            offer = await request.json()
            if (not isinstance(offer, dict) or set(offer) != {"runtimeId", "bindingId"}
                    or not platform_pool.admits(offer["runtimeId"], offer["bindingId"])):
                return web.Response(status=404)
            try:
                probe = (platform_pool.probe_ready(offer["runtimeId"], offer["bindingId"]) if shared
                         else platform_pool.probe_ready())
                await asyncio.wait_for(probe, 3.5)
            except (APIError, OSError, ValueError, asyncio.TimeoutError) as exc:
                code = exc.code if isinstance(exc, APIError) else "NOT_READY"
                return web.json_response({"code": code}, status=503, headers={"Cache-Control": "no-store"})
            return web.Response(status=204)

    async def platform_release_status(request):
        async with admitted(http_slots):
            offer = await request.json()
            if shared and offer == {}:
                return web.json_response(platform_release, headers={"Cache-Control": "no-store"})
            if (not isinstance(offer, dict) or set(offer) != {"runtimeId", "bindingId"}
                    or not platform_pool.admits(offer["runtimeId"], offer["bindingId"])):
                return web.Response(status=404)
            return web.json_response(platform_release, headers={"Cache-Control": "no-store"})

    async def websocket_session(request, service=service, *, platform=False):
        if request.query:
            raise APIError("INVALID_ARGUMENT", "WebSocket credentials belong in the first frame, not the URL")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_ATTACHMENT_REQUEST + 1 if platform else MAX_REQUEST,
                                   compress=False)
        await ws.prepare(request)
        lease = None
        subscriptions = set()
        queue = asyncio.Queue(maxsize=64)
        queued_bytes = 0
        generation = 0
        in_flight = set()
        send_lock = asyncio.Lock()
        sidebar_template = None
        sidebar_installations = ()
        forwarding = True

        def check_binding():
            if platform and shared and not platform_pool.admits(offer["runtimeId"], offer["bindingId"]):
                raise APIError("RUNTIME_MISMATCH", "The Host binding is no longer current")

        async def receive_handshake():
            message = await asyncio.wait_for(ws.receive(), 10)
            if message.type != WSMsgType.TEXT or len(message.data.encode()) > MAX_REQUEST:
                raise APIError("INVALID_ARGUMENT", "Chat handshake exceeds the normal frame budget")
            return json.loads(message.data)

        async def send(value, expected_generation=None, authorization=None):
            data = canonical(value)
            if len(data.encode()) > MAX_RESPONSE:
                raise APIError("RESPONSE_TOO_LARGE", "WebSocket response exceeds budget")
            async with send_lock:
                if not forwarding and value.get("type") != "error":
                    return
                if expected_generation is not None and expected_generation != generation:
                    return
                if authorization is not None:
                    check_binding()
                    service.auth.authenticate(authorization.token)
                await asyncio.wait_for(ws.send_str(data), 10)

        def clear_queue():
            nonlocal queued_bytes
            while not queue.empty():
                queue.get_nowait()
            queued_bytes = 0

        def listen(event):
            nonlocal queued_bytes
            scope = (event["runtimeId"], event["threadId"])
            if event["type"] == "sessions.changed":
                if (event["runtimeId"], None) not in subscriptions:
                    return
            elif not (scope in subscriptions or event["threadId"] is None and any(item[0] == event["runtimeId"] for item in subscriptions)):
                return
            size = len(canonical(event).encode())
            if queue.full() or queued_bytes + size > 1024 * 1024:
                clear_queue()
                subscriptions.clear()
                event = {"type": "resync_required", "reason": "slow_consumer", "epoch": service.epoch}
                size = len(canonical(event).encode())
            queue.put_nowait((generation, event, size))
            queued_bytes += size

        async def pump():
            nonlocal queued_bytes
            while not ws.closed:
                try:
                    item_generation, event, size = await asyncio.wait_for(queue.get(), 1)
                    queued_bytes -= size
                except asyncio.TimeoutError:
                    check_binding()
                    service.auth.authenticate(lease.token)
                    continue
                if item_generation != generation:
                    continue
                current = service.auth.authenticate(lease.token)
                if event["type"] == "sessions.changed":
                    if (event["runtimeId"], None) not in subscriptions:
                        continue
                    service.authorize(current, "sessions.subscribe", {"runtimeId": event["runtimeId"]})
                elif event.get("threadId"):
                    service.authorize(current, "events.subscribe", {"runtimeId": event["runtimeId"], "threadId": event["threadId"]})
                await send({**event, "authContext": current.public()}, item_generation, current)

        async def handle(frame, captured_lease, captured_generation):
            try:
                method, params = service.validate(frame)
                service.authorize(captured_lease, method, params)
                scope = (params.get("runtimeId"), params.get("threadId"))
                if method in ("events.subscribe", "sessions.subscribe"):
                    if len(subscriptions) >= 32:
                        raise APIError("BUSY", "Subscription limit reached")
                    result = await service.adapter(scope[0]).invoke(method, params)
                    service.authorize(captured_lease, method, params)
                    if captured_generation == generation:
                        subscriptions.add(scope)
                    response = {"requestId": frame["requestId"], "ok": True, "result": result, "authContext": captured_lease.public()}
                elif method in ("events.unsubscribe", "sessions.unsubscribe"):
                    subscriptions.discard(scope)
                    response = {"requestId": frame["requestId"], "ok": True, "result": {"unsubscribed": True}, "authContext": captured_lease.public()}
                else:
                    response = await service.dispatch(captured_lease, frame)
                    if method == "chat.sidebar" and sidebar_template and response.get("ok"):
                        agent_id, template_identify = sidebar_template
                        for row in response["result"]["items"]:
                            if row["agentId"] == agent_id:
                                row["templateIdentify"] = template_identify
                                break
                    if method == "chat.sidebar" and sidebar_installations and response.get("ok"):
                        adapter = service.adapter(params["runtimeId"])
                        agents = await adapter.workers(adapter.agents)
                        targets = {native: platform for native, platform, _ in sidebar_installations}
                        resolved = {agent["agentId"]: targets.get(agent["extensions"]["openclaw"]["agentId"])
                                    for agent in agents}
                        for row in response["result"]["items"]:
                            if platform_identify := resolved.get(row["agentId"]):
                                row["platformAgentIdentify"] = platform_identify
                                row["canRefresh"] = True
                # No old-lease response may cross an auth.refresh boundary.
                if captured_generation == generation:
                    await send({"type": "response", **response}, captured_generation, captured_lease if response.get("ok") else None)
            except Exception as exc:
                if captured_generation == generation:
                    await send({"type": "response", **service.error_response(captured_lease, frame.get("requestId"), service.translate_error(exc))}, captured_generation)

        pump_task = None
        native_session = None
        try:
            hello_fields = {}
            if platform:
                from .platform_exchange import request_platform_grant
                offer = await receive_handshake()
                if (not isinstance(offer, dict) or set(offer) != {"type", "runtimeId", "bindingId"}
                        or offer["type"] != "platform.bind" or not platform_pool.admits(offer["runtimeId"], offer["bindingId"])):
                    raise APIError("FORBIDDEN", "The Host offer does not match this Manager sandbox")
            frame = await receive_handshake()
            if not isinstance(frame, dict) or frame.get("type") != "auth":
                raise APIError("UNAUTHENTICATED", "The first WebSocket frame must be auth")
            if platform:
                grant = await request_platform_grant(ws, offer["runtimeId"], offer["bindingId"], frame)
                if shared:
                    session = platform_pool.session(grant)
                    service, lease, agent_id = await session.__aenter__()
                    native_session = session
                else:
                    service, lease, agent_id = await platform_pool.open(grant)
                hello_fields["platformAgentId"] = agent_id
                entry_native = grant.native_agent_id
                hello_fields["seamlessRefresh"] = True
                if grant.template_identify:
                    sidebar_template = (agent_id, grant.template_identify)
                sidebar_installations = grant.attached_installations
            else:
                lease = service.auth.redeem(frame.get("ticket"))
            await send({"type": "hello", "apiVersion": "1.0", "epoch": service.epoch,
                        "eventReplay": False, "authContext": lease.public(), "leaseToken": lease.token, **hello_fields})
            service.listeners.add(listen)
            pump_task = asyncio.create_task(pump())
            pump_task.add_done_callback(lambda task: asyncio.create_task(ws.close(code=4001)) if not task.cancelled() and task.exception() else None)
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                check_binding()
                large = platform and (len(message.data) > MAX_REQUEST or len(message.data.encode()) > MAX_REQUEST)
                frame = json.loads(message.data)
                if not isinstance(frame, dict):
                    raise APIError("INVALID_ARGUMENT", "Frame must be an object")
                if large:
                    params = frame.get("params")
                    if (not grant.instance_chat or frame.get("type") != "request" or frame.get("method") != "runs.create"
                            or not isinstance(params, dict) or not params.get("attachments")):
                        raise APIError("REQUEST_TOO_LARGE", "Only instance Chat attachment sends may exceed the normal frame budget")
                if frame.get("type") == "auth.refresh":
                    if platform:
                        service.auth.authenticate(lease.token)
                        renewed = await request_platform_grant(ws, offer["runtimeId"], offer["bindingId"], frame,
                                                               refresh=True, send=send)
                        check_binding()
                        if not grant.same_scope(renewed):
                            raise APIError("FORBIDDEN", "Refresh changed the authorized scope")
                        lease = service.auth.renew_platform_expiry(lease, renewed.expires_at)
                        # The instance-wide entry Agent may differ; never label it with another Agent's metadata.
                        sidebar_template = ((agent_id, renewed.template_identify)
                                            if renewed.native_agent_id == entry_native and renewed.template_identify else None)
                        sidebar_installations = renewed.attached_installations
                        grant = renewed
                        await send({"type": "auth.refreshed", "authContext": lease.public(),
                                    "resyncRequired": False}, authorization=lease)
                        continue
                    new_lease = service.auth.redeem(frame.get("ticket"))
                    if any(new_lease.context.get(key) != lease.context.get(key) for key in ("issuer", "subject", "tenantId", "audience")):
                        raise APIError("FORBIDDEN", "Refresh cannot change the connection principal")
                    service.auth.revoke(lease.context["leaseId"])
                    lease = new_lease
                    generation += 1
                    clear_queue()
                    subscriptions.clear()
                    await send({"type": "auth.refreshed", "authContext": lease.public(), "leaseToken": lease.token,
                                "resyncRequired": True})
                elif frame.get("type") == "ping":
                    current = service.auth.authenticate(lease.token)
                    await send({"type": "pong", "authContext": current.public()})
                elif frame.get("type") == "request":
                    if len(in_flight) >= 8:
                        await send({"type": "response", **service.error_response(lease, frame.get("requestId"), APIError("BUSY", "Connection request limit reached", retryable=True))})
                        continue
                    task = asyncio.create_task(handle(frame, lease, generation))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                else:
                    raise APIError("INVALID_ARGUMENT", "Unknown WebSocket frame type")
        except Exception as exc:
            forwarding = False
            error = service.translate_error(exc)
            if not ws.closed:
                await send({"type": "error", "error": error.payload()})
        finally:
            forwarding = False
            if platform and lease is not None and native_session is None:
                service.auth.revoke(lease.context["leaseId"])
            service.listeners.discard(listen)
            if pump_task:
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            try:
                if native_session is not None:
                    await native_session.__aexit__(None, None, None)
            finally:
                await ws.close()
        return ws

    async def resource(request):
        async with admitted(media_slots):
            return await resource_stream(request)

    async def resource_stream(request):
        lease = bearer(request)
        params = {"runtimeId": request.query.get("runtimeId"), "resourceId": request.match_info["identifier"]}
        service.authorize(lease, "resources.get", params)
        adapter = service.adapter(params["runtimeId"])
        first = await adapter.resource_chunk(params["resourceId"], offset=0, length=1)
        total = first["totalBytes"]
        start, end = 0, total - 1
        if "Range" in request.headers:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers["Range"])
            if not match or not any(match.groups()):
                return web.Response(status=416, headers={"Content-Range": f"bytes */{total}"})
            if match[1]:
                start, end = int(match[1]), min(int(match[2]), total - 1) if match[2] else total - 1
            else:
                start = max(0, total - int(match[2]))
            if start > end or start >= total:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{total}"})
        service.authorize(lease, "resources.get", params)
        headers = {"Content-Type": first["contentType"], "Content-Length": str(end - start + 1),
                   "Accept-Ranges": "bytes", "ETag": '"' + first["sha256"] + '"', "Cache-Control": "private, no-store",
                   "X-Content-Type-Options": "nosniff", "Content-Disposition": "attachment",
                   "Content-Security-Policy": "sandbox", "X-Agent-Manager-Lease-Id": lease.context["leaseId"],
                   "X-Agent-Manager-Authz-Version": str(lease.context["authzVersion"])}
        if "Range" in request.headers:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        response = web.StreamResponse(status=206 if "Range" in request.headers else 200, headers=headers)
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        try:
            while start <= end:
                service.authorize(lease, "resources.get", params)
                chunk = await adapter.resource_chunk(params["resourceId"], offset=start, length=min(1024 * 1024, end - start + 1))
                service.authorize(lease, "resources.get", params)
                if chunk["sha256"] != first["sha256"] or chunk["totalBytes"] != total:
                    raise ConnectionError("Resource changed while streaming")
                data = base64.b64decode(chunk["data"])
                if not data:
                    raise ConnectionError("Resource ended before declared length")
                await asyncio.wait_for(response.write(data), 10)
                start += len(data)
        except (APIError, ConnectionError, asyncio.TimeoutError):
            # Headers may already be committed. Abort; never append a JSON error to
            # a binary response or claim the declared Content-Length was delivered.
            response.force_close()
            if request.transport:
                request.transport.close()
            return response
        await response.write_eof()
        return response

    async def startup(app):
        await service.start()

    async def reap_bindings():
        while True:
            await asyncio.sleep(2)
            platform_pool.reap()

    reaper = None

    async def start_reaper(app):
        nonlocal reaper
        reaper = asyncio.create_task(reap_bindings())

    async def cleanup(app):
        if reaper is not None:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
        if platform_pool is not None:
            await platform_pool.close()
        await service.close()

    app.router.add_post("/v1/auth/tickets", issue_ticket)
    app.router.add_post("/v1/agents/resolve", resolve_agent)
    app.router.add_post("/v1/auth/redeem", redeem)
    app.router.add_post("/v1/rpc", rpc)
    app.router.add_get("/v1/ws", websocket)
    if platform_pool is not None:
        app.router.add_get("/v1/platform/ws", platform_websocket)
        app.router.add_post("/v1/platform/readiness", platform_readiness)
        if platform_release is not None:
            app.router.add_post("/v1/platform/release", platform_release_status)
    app.router.add_get("/v1/resources/{identifier}", resource)
    app.on_startup.append(startup)
    if shared:
        app.on_startup.append(start_reaper)
    app.on_cleanup.append(cleanup)
    return app


def main():
    parser = argparse.ArgumentParser(description="Agent Manager v1 service (separate from the legacy CLI)")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    service = build_service(config)
    socket_path = service.store.path.with_name("manager.sock")
    platform_pool = None
    platform_release = None
    if "platformSharedHost" in config:
        if "platformSandbox" in config:
            raise ValueError("Shared and single-container Manager modes are mutually exclusive")
        from .platform_runtime import SharedPlatformRuntimePool
        platform_pool = SharedPlatformRuntimePool(service.store.path.parent, service.auth.audience, config["platformSharedHost"])
        if "platformListenSocket" in config:
            from .platform_runtime import shared_listen_path
            socket_path = shared_listen_path(config["platformListenSocket"], service.store.path.parent, platform_pool)
        platform_release = {"version": __version__, "sourceRoot": str(Path(__file__).resolve().parents[1])}
    if "platformSandbox" in config:
        from .platform_runtime import PlatformRuntimePool
        platform_pool = PlatformRuntimePool(service.store.path.parent, service.auth.audience, config["platformSandbox"])
        if "platformListenSocket" in config:
            if config["platformListenSocket"] != "/manager-export/manager.sock" or not platform_pool.gateway_socket:
                raise ValueError("An exported platform socket requires the private native relay")
            socket_path = Path(config["platformListenSocket"])
            # Resolve once, so a later update of "current" cannot relabel this process.
            platform_release = {"version": __version__, "sourceRoot": str(Path(__file__).resolve().parents[1])}
    web.run_app(create_app(service, platform_pool, platform_release), path=str(socket_path), access_log=None)


if __name__ == "__main__":
    main()
