"""Scoped tickets and leases for grants delegated by the trusted server."""
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass

from .errors import APIError
from .store import canonical, now_ms


def token_hash(token):
    if not isinstance(token, str) or not 16 <= len(token) <= 8192:
        raise APIError("UNAUTHENTICATED", "Invalid authorization")
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Lease:
    token: str
    context: dict

    def public(self):
        return {key: self.context[key] for key in ("leaseId", "authzVersion", "expiresAt")}


class LeaseAuthority:
    """Enforce delegated scopes and the lifetime of local tickets and leases."""
    def __init__(self, store, audience):
        self.store, self.audience = store, audience

    def issue_ticket(self, *, issuer, subject, runtime_ids, actions, agent_ids=None,
                     tenant_id=None, ttl_seconds=60, lease_seconds=300, authz_version=1):
        if not issuer or not subject or not runtime_ids or not actions:
            raise ValueError("An explicit principal and scope are required")
        if agent_ids is not None and not agent_ids:
            raise ValueError("An empty Agent scope cannot grant complete runtime access")
        if not 1 <= ttl_seconds <= 60 or not 1 <= lease_seconds <= 300:
            raise ValueError("Tickets live at most 60s and leases at most 300s")
        context = {"issuer": issuer, "subject": subject, "tenantId": tenant_id,
                   "audience": self.audience, "runtimeIds": list(runtime_ids),
                   "actions": list(actions), "agentIds": list(agent_ids or ["*"]),
                   "authzVersion": authz_version, "leaseSeconds": lease_seconds}
        ticket = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM tickets WHERE expires<?", (now_ms(),))
            db.execute("INSERT INTO tickets VALUES(?,?,?)", (token_hash(ticket), now_ms() + ttl_seconds * 1000, canonical(context)))
        return ticket

    def issue_delegated_ticket(self, grant):
        """Accept a grant only from the OS-protected Server API transport."""
        required = {"issuer", "subject", "audience", "runtimeIds", "agentIds", "actions", "authzVersion"}
        optional = {"tenantId", "ttlSeconds", "leaseSeconds"}
        if not isinstance(grant, dict) or not required <= grant.keys() or grant.keys() - required - optional:
            raise APIError("INVALID_ARGUMENT", "A complete server authorization grant is required")
        if grant["audience"] != self.audience:
            raise APIError("FORBIDDEN", "Grant audience does not match this Manager")
        principals = [grant["issuer"], grant["subject"]]
        if grant.get("tenantId") is not None:
            principals.append(grant["tenantId"])
        if not all(isinstance(value, str) and 0 < len(value) <= 256 for value in principals):
            raise APIError("INVALID_ARGUMENT", "Invalid grant principal")
        for key in ("runtimeIds", "agentIds", "actions"):
            scope = grant[key]
            if not isinstance(scope, list) or not 1 <= len(scope) <= 256 or not all(
                isinstance(value, str) and 0 < len(value) <= 128 for value in scope
            ):
                raise APIError("INVALID_ARGUMENT", "Invalid grant scope", details={"field": key})
        version = grant["authzVersion"]
        ttl = grant.get("ttlSeconds", 60)
        lease = grant.get("leaseSeconds", 300)
        if type(version) is not int or version < 0 or type(ttl) is not int or not 1 <= ttl <= 60 or type(lease) is not int or not 1 <= lease <= 300:
            raise APIError("INVALID_ARGUMENT", "Invalid grant authorization version or lifetime")
        return self.issue_ticket(
            issuer=grant["issuer"], subject=grant["subject"], tenant_id=grant.get("tenantId"),
            runtime_ids=grant["runtimeIds"], agent_ids=grant["agentIds"], actions=grant["actions"],
            authz_version=version, ttl_seconds=ttl, lease_seconds=lease,
        )

    def redeem(self, ticket):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tickets WHERE hash=?", (token_hash(ticket),)).fetchone()
            if row is None or row["expires"] <= now_ms():
                raise APIError("UNAUTHENTICATED", "Ticket expired or already redeemed")
            context = json.loads(row["context"])
            if context["audience"] != self.audience:
                raise APIError("UNAUTHENTICATED", "Ticket audience mismatch")
            db.execute("DELETE FROM tickets WHERE hash=?", (token_hash(ticket),))
            context.update(leaseId=uuid.uuid4().hex, expiresAt=now_ms() + context.pop("leaseSeconds") * 1000)
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO leases VALUES(?,?,?,?,0)", (token_hash(token), context["leaseId"], context["expiresAt"], canonical(context)))
        return Lease(token, context)

    def authenticate(self, token):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM leases WHERE hash=?", (token_hash(token),)).fetchone()
        if row is None or row["revoked"] or row["expires"] <= now_ms():
            raise APIError("UNAUTHENTICATED", "Authorization expired or revoked")
        context = json.loads(row["context"])
        if context["audience"] != self.audience:
            raise APIError("UNAUTHENTICATED", "Authorization audience mismatch")
        return Lease(token, context)

    def revoke(self, lease_id):
        with self.store.connect() as db:
            db.execute("UPDATE leases SET revoked=1 WHERE id=?", (lease_id,))

    def authorize(self, lease, method, runtime=None, agent=None, *, runtime_wide=False):
        fresh = self.authenticate(lease.token)
        context = fresh.context
        if method not in context["actions"] and "*" not in context["actions"]:
            raise APIError("FORBIDDEN", "Action is outside the authorization scope")
        if runtime and runtime not in context["runtimeIds"] and "*" not in context["runtimeIds"]:
            raise APIError("FORBIDDEN", "Runtime is outside the authorization scope")
        if runtime_wide and "*" not in context["agentIds"]:
            raise APIError("FORBIDDEN", "Action requires authorization for the complete runtime")
        if agent and agent not in context["agentIds"] and "*" not in context["agentIds"]:
            raise APIError("FORBIDDEN", "Agent is outside the authorization scope")
        return fresh
