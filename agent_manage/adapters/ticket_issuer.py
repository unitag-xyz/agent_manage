"""Optional Dola/standalone issuer integration; no Dola URL is hard coded."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from ..core.errors import APIError


class TicketIssuer:
    def __init__(self, authority, config):
        self.authority = authority
        self.url = config["url"]
        url = urlparse(self.url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise ValueError("Ticket issuer must be an administrator-configured HTTPS endpoint")
        self.issuer = config["issuer"]
        self.credential_path = Path(config["credentialFile"])
        if self.credential_path.is_symlink() or self.credential_path.stat().st_mode & 0o077:
            raise ValueError("Ticket issuer credential must be a private regular file")
        self.session = None

    async def redeem(self, ticket):
        if not isinstance(ticket, str) or not 16 <= len(ticket) <= 8192:
            raise APIError("UNAUTHENTICATED", "Invalid ticket")
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5), trust_env=False)
        try:
            # Trusted issuer atomically consumes the single-use ticket. Never retry
            # an uncertain redemption or follow a redirect carrying service credentials.
            async with self.session.post(self.url, json={"ticket": ticket, "audience": self.authority.audience},
                headers={"Authorization": "Bearer " + self.credential_path.read_text().strip()}, allow_redirects=False) as response:
                if response.status != 200:
                    raise APIError("UNAUTHENTICATED", "Ticket issuer rejected authorization")
                raw = bytearray()
                async for part in response.content.iter_chunked(8192):
                    raw.extend(part)
                    if len(raw) > 32768:
                        raise APIError("UNAUTHENTICATED", "Ticket issuer response exceeds budget")
                grant = json.loads(raw)
            if grant.get("issuer") != self.issuer:
                raise APIError("UNAUTHENTICATED", "Ticket issuer identity mismatch")
            return self.authority.import_verified_grant(grant)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError) as exc:
            raise APIError("UNAUTHENTICATED", "Cannot validate ticket with the trusted issuer") from exc

    async def close(self):
        if self.session:
            await self.session.close()
