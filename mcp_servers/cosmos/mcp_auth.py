"""Same-tenant delegated authentication for protected MCP deployments."""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

import jwt
import msal
import requests

from cosmos_tools import parse_allowed_containers, parse_fields


class AuthenticationRequired(Exception):
    def __init__(self, claims: str | None = None):
        super().__init__("Sign in to authorize SharePoint access")
        self.claims = claims


class AuthorizationUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("SharePoint authorization is unavailable; retry the request")


@dataclass(frozen=True)
class Caller:
    tenant_id: str
    object_id: str
    access_token: str = field(repr=False)


def canonical_guid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a tenant or application GUID")
    result = UUID(value)
    if result.int == 0:
        raise ValueError("An empty GUID is not allowed")
    return str(result)


def _required(name: str) -> str:
    value = os.getenv(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required for protected SharePoint retrieval")
    return value


@dataclass(frozen=True)
class SecuritySettings:
    containers: tuple[str, ...]
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    public_url: str
    allowed_fields: tuple[str, ...]

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def scope(self) -> str:
        return f"api://{self.client_id}/access_as_user"

    @property
    def metadata_url(self) -> str:
        return self.public_url.rsplit("/", 1)[0] + "/.well-known/oauth-protected-resource"

    def challenge(self, claims: str | None = None) -> str:
        value = f'Bearer resource_metadata="{self.metadata_url}", scope="{self.scope}"'
        if claims is not None:
            value += f', error="insufficient_claims", claims="{claims}"'
        return value


def load_security_settings(allowed_containers: tuple[str, ...]) -> SecuritySettings | None:
    value = os.getenv("COSMOS_SHAREPOINT_PROTECTED_CONTAINERS", "").strip()
    if not value:
        return None
    containers = parse_allowed_containers(value)
    if not set(containers).issubset(allowed_containers):
        raise ValueError("Protected SharePoint containers must be in COSMOS_ALLOWED_CONTAINERS")
    public_url = _required("MCP_PUBLIC_URL").strip().rstrip("/")
    url = urlsplit(public_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not url.path.endswith("/mcp")
            or any(char.isspace() or char in '"\\' for char in public_url)):
        raise ValueError("MCP_PUBLIC_URL must be the HTTPS MCP endpoint without credentials or query parameters")
    fields = parse_fields(os.getenv(
        "COSMOS_SHAREPOINT_ALLOWED_FIELDS", "id,title,body,content,source_ref"
    ))
    if any(name.startswith("_") or name == "distance" for name in fields):
        raise ValueError("Protected result fields must not include internal metadata")
    return SecuritySettings(
        containers, canonical_guid(_required("MCP_ENTRA_TENANT_ID")),
        canonical_guid(_required("MCP_ENTRA_CLIENT_ID")), _required("MCP_ENTRA_CLIENT_SECRET"),
        public_url, fields,
    )


def encode_claims(value: object, *, encoded: bool = False) -> str | None:
    """Only forward a bounded JSON claims object, never an upstream header verbatim."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 12000:
        raise AuthorizationUnavailable()
    try:
        raw = (
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode("utf-8")
            if encoded else value
        )
        claims = json.loads(raw)
        if not isinstance(claims, dict):
            raise ValueError("Invalid claims")
    except (ValueError, UnicodeError):
        raise AuthorizationUnavailable() from None
    return base64.b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode("ascii")


class EntraAuthenticator:
    def __init__(self, settings: SecuritySettings):
        self.settings = settings
        self._keys = jwt.PyJWKClient(
            f"{settings.authority}/discovery/v2.0/keys", timeout=5, lifespan=300,
        )
        self._application: msal.ConfidentialClientApplication | None = None

    def authenticate(self, authorization: str | None) -> Caller:
        parts = (authorization or "").split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 65536:
            raise AuthenticationRequired()
        token = parts[1]
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise AuthenticationRequired()
            key = self._keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.settings.client_id,
                issuer=f"{self.settings.authority}/v2.0",
                options={"require": ["exp", "nbf", "iat", "iss", "aud", "tid", "oid", "scp"]},
            )
            tenant_id = canonical_guid(claims["tid"])
            object_id = canonical_guid(claims["oid"])
            scopes = claims["scp"]
            if (tenant_id != self.settings.tenant_id or claims.get("ver") != "2.0"
                    or not isinstance(scopes, str) or "access_as_user" not in scopes.split()):
                raise AuthenticationRequired()
        except jwt.PyJWKClientConnectionError:
            logging.warning("MCP signing-key service unavailable")
            raise AuthorizationUnavailable() from None
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AuthenticationRequired() from None
        return Caller(tenant_id, object_id, token)

    def graph_token(self, caller: Caller) -> str:
        if caller.tenant_id != self.settings.tenant_id:
            raise AuthenticationRequired()
        try:
            if self._application is None:
                self._application = msal.ConfidentialClientApplication(
                    self.settings.client_id, authority=self.settings.authority,
                    client_credential=self.settings.client_secret,
                    client_capabilities=["CP1"], timeout=5,
                )
            result = self._application.acquire_token_on_behalf_of(
                caller.access_token, scopes=["https://graph.microsoft.com/.default"],
            )
        except (requests.RequestException, ValueError):
            logging.warning("MCP delegated token service unavailable")
            raise AuthorizationUnavailable() from None
        token = result.get("access_token")
        if isinstance(token, str) and token:
            return token
        if result.get("error") in {"invalid_grant", "interaction_required", "consent_required", "login_required"}:
            raise AuthenticationRequired(encode_claims(result.get("claims")))
        logging.warning("MCP delegated token acquisition failed")
        raise AuthorizationUnavailable()
