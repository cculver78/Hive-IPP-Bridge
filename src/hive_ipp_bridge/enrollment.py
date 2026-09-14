"""Claim PaperCut Hive credentials from a setup link.

Developed by Edge Case Software — https://edgecasesoftware.dev
"""

from __future__ import annotations

import base64
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass


CLAIM_PATH = "/print-client/secure/printclient-gateway/claim-printclient-token/v2"
TOKEN_PARAMETERS = ("t", "token", "authToken", "auth-token")


class EnrollmentError(RuntimeError):
    """A safe, user-facing enrollment failure."""


@dataclass(frozen=True)
class SetupLink:
    auth_token: str
    service_base: str
    organization_id: str | None = None


@dataclass(frozen=True)
class Credentials:
    jwt: str
    client_id: str
    organization_id: str


def _region_from_token(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    for access in claims.get("pcProductAccesses", []):
        if isinstance(access, dict) and access.get("Product") == "hive":
            return access.get("DataCenter")
    return None


def _service_base(hostname: str, scheme: str = "https") -> str:
    hostname = hostname.lower()
    if hostname in {"localhost", "127.0.0.1"} or hostname.startswith(("localhost:", "127.0.0.1:")):
        return f"{scheme}://{hostname}"
    if hostname == "hive.papercut.com":
        return "https://pmitc.papercut.com"
    suffix = ".hive.papercut.com"
    if hostname.endswith(suffix):
        region = hostname.removesuffix(suffix)
        if region and "." not in region:
            return f"https://{region}.pmitc.papercut.com"
    raise EnrollmentError("the setup link is not for a recognized PaperCut Hive host")


def parse_setup_link(value: str) -> SetupLink:
    value = value.strip()
    if not value:
        raise EnrollmentError("the setup link was empty")

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme:
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise EnrollmentError("the setup link is not a valid URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise EnrollmentError("the setup link must use HTTPS")
        query = urllib.parse.parse_qs(parsed.query)
        auth_token = next((query[name][0] for name in TOKEN_PARAMETERS if query.get(name)), None)
        if not auth_token:
            raise EnrollmentError("the setup link does not contain an auth token")
        organization_id = query.get("orgId", [None])[0]
        return SetupLink(auth_token, _service_base(parsed.netloc, parsed.scheme), organization_id)

    region = _region_from_token(value)
    host = "hive.papercut.com" if region in {None, "us", "us-genesis"} else f"{region}.hive.papercut.com"
    return SetupLink(value, _service_base(host))


def claim(value: str, *, timeout: float = 15) -> Credentials:
    setup = parse_setup_link(value)
    client_id = str(uuid.uuid4())
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {setup.auth_token}",
        "Client-Id": client_id,
        "Content-Type": "application/json",
        "X-Correlation-ID": f"LINUX-PRINT-CLIENT|{secrets.token_urlsafe(15)[:20]}",
    }
    if setup.organization_id:
        headers["X-PMITC-OrgId"] = setup.organization_id
    request = urllib.request.Request(
        setup.service_base + CLAIM_PATH,
        data=json.dumps({"appInfo": "Hive IPP Bridge"}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in {400, 401, 403, 404, 409, 410}:
            raise EnrollmentError("PaperCut rejected the setup link; request a new link") from None
        raise EnrollmentError(f"PaperCut enrollment returned HTTP {error.code}") from None
    except urllib.error.URLError as error:
        raise EnrollmentError(f"could not reach PaperCut: {error.reason}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise EnrollmentError("PaperCut returned an invalid enrollment response") from None

    token = result.get("token")
    organization_id = result.get("orgId")
    if not isinstance(token, str) or not token or not isinstance(organization_id, str) or not organization_id:
        raise EnrollmentError("PaperCut returned an incomplete enrollment response")
    return Credentials(token, client_id, organization_id)
