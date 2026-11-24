import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import re
from urllib.parse import urlencode, urlsplit

import httpx

from .models import Launcher


LINK_TTL = 600
STEAM_OPENID = "https://steamcommunity.com/openid/login"
VK_ID_ORIGIN = "https://id.vk.com"
STEAM_ID = re.compile(r"https://steamcommunity\.com/openid/id/(765[0-9]{14})")
OPENID_NS = "http://specs.openid.net/auth/2.0"
REQUIRED_SIGNATURE_FIELDS = {
    "assoc_handle", "op_endpoint", "claimed_id", "identity", "return_to", "response_nonce",
}


class LinkError(Exception):
    pass


@dataclass(frozen=True)
class OAuthProvider:
    authorize_url: str
    token_url: str
    scope: str
    profile_url: str | None
    identity_field: str


OAUTH_PROVIDERS = {
    Launcher.EPIC: OAuthProvider(
        "https://www.epicgames.com/id/authorize",
        "https://api.epicgames.dev/epic/oauth/v2/token",
        "basic_profile", None, "account_id",
    ),
    Launcher.RIOT: OAuthProvider(
        "https://auth.riotgames.com/authorize", "https://auth.riotgames.com/token",
        "openid", "https://americas.api.riotgames.com/riot/account/v1/accounts/me", "puuid",
    ),
    Launcher.BATTLENET: OAuthProvider(
        "https://oauth.battle.net/authorize", "https://oauth.battle.net/token",
        "openid", "https://oauth.battle.net/userinfo", "sub",
    ),
}


def configured_oauth():
    result = {}
    for launcher in OAUTH_PROVIDERS:
        prefix = f"GUP_{launcher.value.upper()}_OAUTH_"
        client_id = os.environ.get(prefix + "CLIENT_ID")
        secret = os.environ.get(prefix + "CLIENT_SECRET")
        if client_id and secret:
            result[launcher] = (client_id, secret)
    vk_app = os.environ.get("GUP_VKPLAY_VK_ID_CLIENT_ID", "")
    if re.fullmatch(r"[1-9][0-9]{0,19}", vk_app):
        result[Launcher.VKPLAY] = (vk_app, None)
    return result


def public_origin(value):
    if not value:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
        return f"https://{url.netloc}"
    except ValueError:
        raise ValueError("GUP_PUBLIC_ORIGIN must be a bare HTTPS origin") from None


def steam_return_to(callback_url, state):
    return callback_url + "?" + urlencode({"state": state})


def steam_authorize(origin, callback_url, state):
    identifier = OPENID_NS + "/identifier_select"
    return STEAM_OPENID + "?" + urlencode({
        "openid.ns": OPENID_NS, "openid.mode": "checkid_setup",
        "openid.return_to": steam_return_to(callback_url, state),
        "openid.realm": origin + "/", "openid.claimed_id": identifier, "openid.identity": identifier,
    })


def verify_steam_identity(client, query, callback_url, state, now):
    response = {key: value for key, value in query.items() if key.startswith("openid.")}
    identity = STEAM_ID.fullmatch(response.get("openid.claimed_id", ""))
    signed = set(response.get("openid.signed", "").split(","))
    if (
        not identity
        or response.get("openid.identity") != identity[0]
        or response.get("openid.op_endpoint") != STEAM_OPENID
        or response.get("openid.ns") != OPENID_NS
        or response.get("openid.mode") != "id_res"
        or response.get("openid.return_to") != steam_return_to(callback_url, state)
        or not REQUIRED_SIGNATURE_FIELDS.issubset(signed)
        or not response.get("openid.sig")
        or not response.get("openid.assoc_handle")
    ):
        raise LinkError("Invalid Steam response")
    nonce = response.get("openid.response_nonce", "")
    try:
        issued = datetime.strptime(nonce[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if not -60 <= now - issued.timestamp() <= LINK_TTL:
            raise ValueError
    except ValueError:
        raise LinkError("Expired Steam response") from None
    response["openid.mode"] = "check_authentication"
    confirmation = client.post(STEAM_OPENID, data=response)
    confirmation.raise_for_status()
    lines = confirmation.text.splitlines()
    if [line for line in lines if line.startswith("is_valid:")] != ["is_valid:true"]:
        raise LinkError("Steam did not verify the response")
    return identity[1]


def oauth_authorize(launcher, client_id, callback_url, state):
    provider = OAUTH_PROVIDERS[launcher]
    return provider.authorize_url + "?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": callback_url,
        "scope": provider.scope, "state": state,
    })


def vk_authorize(client_id, callback_url, state, verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return VK_ID_ORIGIN + "/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": callback_url,
        "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
    })


def verify_oauth_identity(client, launcher, credentials, query, callback_url):
    provider = OAUTH_PROVIDERS[launcher]
    code = query.get("code")
    if not code or len(code) > 2048:
        raise LinkError("Invalid authorization code")
    access = client.post(provider.token_url, auth=credentials, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": callback_url,
    })
    access.raise_for_status()
    payload = access.json()
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", token):
        raise LinkError("Invalid provider token response")
    if provider.profile_url:
        profile = client.get(provider.profile_url, headers={"Authorization": f"Bearer {token}"})
        profile.raise_for_status()
        payload = profile.json()
    identity = payload.get(provider.identity_field) if isinstance(payload, dict) else None
    if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", identity):
        raise LinkError("Invalid provider identity")
    return identity


def verify_vk_identity(client, client_id, query, callback_url, state, verifier):
    code, device = query.get("code"), query.get("device_id")
    if not code or len(code) > 2048 or not device or len(device) > 256 or not verifier:
        raise LinkError("Invalid VK ID response")
    access = client.post(VK_ID_ORIGIN + "/oauth2/auth", params={
        "grant_type": "authorization_code", "client_id": client_id, "redirect_uri": callback_url,
        "code_verifier": verifier, "state": state, "device_id": device,
    }, data={"code": code})
    access.raise_for_status()
    payload = access.json()
    if (not isinstance(payload, dict) or payload.get("state") != state
            or not isinstance(payload.get("access_token"), str) or not payload["access_token"]):
        raise LinkError("Invalid VK ID token response")
    subject = payload.get("user_id")
    if type(subject) is not int or subject <= 0:
        raise LinkError("Invalid VK ID")
    return str(subject)


def verified_identity(launcher, credentials, query, callback_url, state, now, verifier):
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=4), follow_redirects=False, trust_env=False) as client:
            if launcher == Launcher.STEAM:
                return verify_steam_identity(client, query, callback_url, state, now)
            if launcher == Launcher.VKPLAY:
                return verify_vk_identity(client, credentials[0], query, callback_url, state, verifier)
            return verify_oauth_identity(client, launcher, credentials, query, callback_url)
    except (httpx.HTTPError, ValueError):
        raise LinkError("Could not verify the provider response") from None
