"""Optional v1 HTTP/WSS host: python -m agent_manage.server --config server.json.

The existing agent-manage CLI entry point and protocol remain unchanged.
"""
import argparse
import asyncio
import base64
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import web, WSMsgType

from .core.errors import APIError
from .core.service import Service
from .core.store import canonical
from .adapters.registry import create_runtime_adapter

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
SERVICE = web.AppKey("service", Service)


def build_service(config):
    service = Service(Path(config["stateDirectory"]).expanduser().resolve(), audience=config.get("audience", "agent-manager"))
    if config.get("ticketIssuer"):
        from .adapters.ticket_issuer import TicketIssuer
        service.ticket_issuer = TicketIssuer(service.auth, config["ticketIssuer"])
    for runtime in config.get("runtimes", []):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", runtime["id"]):
            raise ValueError("Invalid runtime id")
        environment = runtime.get("environment", {})
        if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()):
            raise ValueError("Runtime environment must contain string values")
        service.register(create_runtime_adapter(runtime, service.store, service.read_workers, service.publish))
    return service


def create_app(service, *, allowed_origins=()):
    origins = frozenset(allowed_origins)
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
        origin = request.headers.get("Origin")
        if origin and origin not in origins:
            return web.json_response({"ok": False, "error": {"code": "FORBIDDEN", "message": "Origin is not allowed"}}, status=403)
        try:
            response = await handler(request)
        except APIError as exc:
            response = web.json_response({"ok": False, "error": exc.payload()}, status=401 if exc.code == "UNAUTHENTICATED" else 403 if exc.code == "FORBIDDEN" else 400)
        except (json.JSONDecodeError, TypeError, KeyError):
            response = web.json_response({"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "Invalid request body"}}, status=400)
        if not response.prepared:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
        return response

    app = web.Application(middlewares=[boundary], client_max_size=MAX_REQUEST)
    app[SERVICE] = service

    def bearer(request):
        value = request.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            raise APIError("UNAUTHENTICATED", "A scoped bearer lease is required")
        return service.auth.authenticate(value[7:])

    async def options(request):
        return web.Response(headers={"Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, Range", "Access-Control-Max-Age": "300"})

    async def redeem(request):
        body = await request.json()
        lease = await service.redeem(body["ticket"])
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
            if request.headers.get("Origin"):
                headers.update({"Access-Control-Allow-Origin": request.headers["Origin"], "Vary": "Origin"})
            response = web.Response(body=encoded, content_type="application/json", headers=headers)
            await response.prepare(request)
            await asyncio.wait_for(response.write_eof(), 10)
            return response

    async def websocket(request):
        async with admitted(websocket_slots):
            return await websocket_session(request)

    async def websocket_session(request):
        if request.query:
            raise APIError("INVALID_ARGUMENT", "WebSocket credentials belong in the first frame, not the URL")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_REQUEST, compress=False)
        await ws.prepare(request)
        lease = None
        subscriptions = set()
        queue = asyncio.Queue(maxsize=64)
        queued_bytes = 0
        generation = 0
        in_flight = set()
        send_lock = asyncio.Lock()

        async def send(value, expected_generation=None, authorization=None):
            data = canonical(value)
            if len(data.encode()) > MAX_RESPONSE:
                raise APIError("RESPONSE_TOO_LARGE", "WebSocket response exceeds budget")
            async with send_lock:
                if expected_generation is not None and expected_generation != generation:
                    return
                if authorization is not None:
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
            if not (scope in subscriptions or event["threadId"] is None and any(item[0] == event["runtimeId"] for item in subscriptions)):
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
                    service.auth.authenticate(lease.token)
                    continue
                if item_generation != generation:
                    continue
                current = service.auth.authenticate(lease.token)
                if event.get("threadId"):
                    service.authorize(current, "events.subscribe", {"runtimeId": event["runtimeId"], "threadId": event["threadId"]})
                await send({**event, "authContext": current.public()}, item_generation, current)

        async def handle(frame, captured_lease, captured_generation):
            try:
                method, params = service.validate(frame)
                service.authorize(captured_lease, method, params)
                scope = (params.get("runtimeId"), params.get("threadId"))
                if method == "events.subscribe":
                    if len(subscriptions) >= 32:
                        raise APIError("BUSY", "Subscription limit reached")
                    result = await service.adapter(scope[0]).invoke(method, params)
                    service.authorize(captured_lease, method, params)
                    if captured_generation == generation:
                        subscriptions.add(scope)
                    response = {"requestId": frame["requestId"], "ok": True, "result": result, "authContext": captured_lease.public()}
                elif method == "events.unsubscribe":
                    subscriptions.discard(scope)
                    response = {"requestId": frame["requestId"], "ok": True, "result": {"unsubscribed": True}, "authContext": captured_lease.public()}
                else:
                    response = await service.dispatch(captured_lease, frame)
                # No old-lease response may cross an auth.refresh boundary.
                if captured_generation == generation:
                    await send({"type": "response", **response}, captured_generation, captured_lease if response.get("ok") else None)
            except Exception as exc:
                if captured_generation == generation:
                    await send({"type": "response", **service.error_response(captured_lease, frame.get("requestId"), service.translate_error(exc))}, captured_generation)

        pump_task = None
        try:
            frame = await asyncio.wait_for(ws.receive_json(), 10)
            if not isinstance(frame, dict) or frame.get("type") != "auth":
                raise APIError("UNAUTHENTICATED", "The first WebSocket frame must be auth")
            lease = await service.redeem(frame.get("ticket"))
            await send({"type": "hello", "apiVersion": "1.0", "epoch": service.epoch,
                        "eventReplay": False, "authContext": lease.public(), "leaseToken": lease.token})
            service.listeners.add(listen)
            pump_task = asyncio.create_task(pump())
            pump_task.add_done_callback(lambda task: asyncio.create_task(ws.close(code=4001)) if not task.cancelled() and task.exception() else None)
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                frame = json.loads(message.data)
                if not isinstance(frame, dict):
                    raise APIError("INVALID_ARGUMENT", "Frame must be an object")
                if frame.get("type") == "auth.refresh":
                    new_lease = await service.redeem(frame.get("ticket"))
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
            error = service.translate_error(exc)
            if not ws.closed:
                await send({"type": "error", "error": error.payload()})
        finally:
            service.listeners.discard(listen)
            if pump_task:
                pump_task.cancel()
                await asyncio.gather(pump_task, return_exceptions=True)
            for task in in_flight:
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
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
        if request.headers.get("Origin"):
            headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
            headers["Vary"] = "Origin"
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

    async def cleanup(app):
        await service.close()

    app.router.add_post("/v1/auth/redeem", redeem)
    app.router.add_post("/v1/rpc", rpc)
    app.router.add_get("/v1/ws", websocket)
    app.router.add_get("/v1/resources/{identifier}", resource)
    app.router.add_route("OPTIONS", "/v1/{tail:.*}", options)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app


def main():
    parser = argparse.ArgumentParser(description="Agent Manager v1 service (separate from the legacy CLI)")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    host = config.get("listenHost", "127.0.0.1")
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("Bind to loopback and use an authenticated TLS reverse proxy for remote access")
    web.run_app(create_app(build_service(config), allowed_origins=config.get("allowedOrigins", [])),
                host=host, port=config.get("listenPort", 18900), access_log=None)


if __name__ == "__main__":
    main()
