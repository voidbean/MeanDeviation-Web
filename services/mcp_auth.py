"""Single-owner OAuth provider for the SDK: pre-registered client, PKCE and durable grants.

No anonymous registration or external identity service. Only the separate auth DB is writable;
bearer/refresh tokens and authorization codes are stored as SHA-256 digests, never plaintext.
"""
import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken,
    TokenError, construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

SCOPE = "stocks:read"
COOKIE = "__Host-8848-consent"


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${derived.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    if len(password) > 1024:
        return False
    return hmac.compare_digest(password_hash(password, bytes.fromhex(encoded.split("$")[1])), encoded)


@dataclass(frozen=True)
class MCPConfig:
    base_url: str
    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]
    owner_password_hash: str
    auth_db: Path

    def __post_init__(self):
        url = urlsplit(self.base_url)
        local = url.hostname in {"localhost", "127.0.0.1"}
        if (not url.hostname or (url.scheme != "https" and not (local and url.scheme == "http"))
                or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment):
            raise ValueError("MCP_PUBLIC_URL 必须是无路径的 HTTPS 域名（仅本机测试允许 HTTP）")
        if not self.client_id or len(self.client_secret) < 32:
            raise ValueError("必须设置 MCP_CLIENT_ID 和至少32字符的 MCP_CLIENT_SECRET")
        if not re.fullmatch(r"scrypt\$[0-9a-f]{32}\$[0-9a-f]{64}", self.owner_password_hash):
            raise ValueError("请先用 scripts.mcp_admin password-hash 生成 MCP_OWNER_PASSWORD_HASH")
        if not self.redirect_uris:
            raise ValueError("必须设置 MCP_REDIRECT_URIS 为客户端展示的精确 OAuth 回调地址")
        for callback in self.redirect_uris:
            parsed = urlsplit(callback)
            if (not parsed.hostname or parsed.fragment or parsed.query or parsed.username or parsed.password
                    or (parsed.scheme != "https" and not (local and parsed.scheme == "http"
                                                          and parsed.hostname in {"localhost", "127.0.0.1"}))):
                raise ValueError("MCP_REDIRECT_URIS 必须是精确的 HTTPS 地址，不能含查询或片段")

    @classmethod
    def from_env(cls):
        from core.config import _ROOT  # loads only the root .env
        return cls(
            base_url=os.getenv("MCP_PUBLIC_URL", "").rstrip("/"),
            client_id=os.getenv("MCP_CLIENT_ID", "8848-chatgpt"),
            client_secret=os.getenv("MCP_CLIENT_SECRET", ""),
            redirect_uris=tuple(x.strip() for x in os.getenv("MCP_REDIRECT_URIS", "").split(",") if x.strip()),
            owner_password_hash=os.getenv("MCP_OWNER_PASSWORD_HASH", ""),
            auth_db=Path(os.getenv("MCP_AUTH_DB", str(_ROOT / "mcp_auth.db"))).absolute(),
        )

    @property
    def issuer(self):
        # Pydantic normalizes an origin-only URL to a trailing slash; use that exact issuer everywhere.
        return str(AnyHttpUrl(self.base_url))

    @property
    def resource(self):
        return self.base_url.rstrip("/") + "/mcp"

    @property
    def binding(self):
        # Config changes invalidate old grants, including owner-password or client-secret rotation.
        return hashlib.sha256(json.dumps([self.issuer, self.client_id, self.client_secret,
                                          self.redirect_uris, self.owner_password_hash]).encode()).hexdigest()


class GrantStore:
    def __init__(self, config: MCPConfig):
        self.config = config
        path = config.auth_db
        if path.is_symlink():
            raise ValueError("MCP_AUTH_DB 不能是符号链接")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        path.chmod(0o600)
        with self.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS grants(
                digest TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
                expires REAL NOT NULL,family TEXT NOT NULL,binding TEXT NOT NULL)""")
            conn.execute("CREATE TABLE IF NOT EXISTS rate_limits(bucket TEXT PRIMARY KEY,slot INTEGER,count INTEGER)")

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.config.auth_db, timeout=5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def digest(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def put(self, conn, kind, key, data, expires, family=""):
        conn.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?,?)",
                     (self.digest(key), kind, json.dumps(data), expires, family, self.config.binding))

    def get(self, conn, kind, key):
        row = conn.execute("SELECT payload FROM grants WHERE digest=? AND kind=? AND expires>? AND binding=?",
                           (self.digest(key), kind, time.time(), self.config.binding)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, conn, key):
        conn.execute("DELETE FROM grants WHERE digest=?", (self.digest(key),))

    def rate_allowed(self, bucket, maximum, window):
        slot = int(time.time() // window)
        with self.transaction() as conn:
            conn.execute("DELETE FROM grants WHERE expires<=?", (time.time(),))
            conn.execute("""INSERT INTO rate_limits VALUES(?,?,1) ON CONFLICT(bucket) DO UPDATE SET
                count=CASE WHEN slot=excluded.slot THEN count+1 ELSE 1 END,slot=excluded.slot""", (bucket, slot))
            return conn.execute("SELECT count FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()[0] <= maximum


class OwnerOAuthProvider:
    def __init__(self, config: MCPConfig):
        self.config = config
        self.store = GrantStore(config)

    async def get_client(self, client_id):
        if client_id != self.config.client_id:
            return None
        return OAuthClientInformationFull(
            client_id=client_id, client_secret=self.config.client_secret, client_name="8848 私人只读客户端",
            redirect_uris=[AnyUrl(uri) for uri in self.config.redirect_uris],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="client_secret_post", scope=SCOPE,
        )

    async def register_client(self, client_info):
        raise NotImplementedError("Only a pre-registered private client is allowed")

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource != self.config.resource:
            raise AuthorizeError("invalid_request", "resource must equal the canonical MCP URL")
        if params.scopes != [SCOPE] or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "stocks:read and a valid S256 challenge are required")
        request_id = secrets.token_urlsafe(32)
        with self.store.transaction() as conn:
            self.store.put(conn, "pending", request_id,
                           {"client_id": client.client_id, **params.model_dump(mode="json")}, time.time() + 300)
        return "/consent?request=" + request_id

    async def consent(self, request: Request):
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            csrf = secrets.token_urlsafe(32)
            with self.store.transaction() as conn:
                pending = self.store.get(conn, "pending", request_id)
                if not pending:
                    return PlainTextResponse("授权请求已过期，请从客户端重新连接。", status_code=400)
                pending["csrf_digest"] = self.store.digest(csrf)
                # Keep the original deadline even when the user reloads the form.
                row = conn.execute("SELECT expires FROM grants WHERE digest=?", (self.store.digest(request_id),)).fetchone()
                self.store.put(conn, "pending", request_id, pending, row[0])
            page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>8848 只读授权</title>
<body><main><h1>授权读取 8848 数据</h1>
<p>此客户端将读取自选股、持仓/现金/盈亏、行情缓存和监控事件。不会修改业务数据，也不会下单。</p>
<p>客户端：{html.escape(pending['client_id'])}<br>回调：{html.escape(pending['redirect_uri'])}</p>
<p>只在你主动连接客户端时授权。客户端密钥不是这里的账户授权密码。</p>
<form method="post" action="/consent">
<input type="hidden" name="request" value="{html.escape(request_id, quote=True)}">
<input type="hidden" name="csrf" value="{csrf}">
<label>账户授权密码 <input type="password" name="password" autocomplete="current-password" maxlength="1024" required></label>
<button name="decision" value="allow">允许只读访问</button>
<button name="decision" value="deny" formnovalidate>拒绝</button></form></main></body></html>"""
            response = HTMLResponse(page)
            response.set_cookie(COOKIE, csrf, secure=True, httponly=True, samesite="lax", max_age=300, path="/")
            return response

        form = await request.form()
        request_id = str(form.get("request", ""))
        csrf = str(form.get("csrf", ""))
        with self.store.transaction() as conn:
            pending = self.store.get(conn, "pending", request_id)
        cookie = request.cookies.get(COOKIE, "")
        if (not pending or not csrf or not cookie or not hmac.compare_digest(csrf.encode(), cookie.encode())
                or not hmac.compare_digest(self.store.digest(csrf), pending.get("csrf_digest", ""))):
            return PlainTextResponse("授权表单无效或已过期，请重新连接。", status_code=400)
        if form.get("decision") not in {"allow", "deny"}:
            return PlainTextResponse("无效的授权选择。", status_code=400)
        if form.get("decision") == "allow":
            if not self.store.rate_allowed("owner-login", 10, 900):
                return PlainTextResponse("授权尝试过多，请15分钟后重试。", status_code=429)
            valid = await asyncio.to_thread(password_matches, str(form.get("password", "")), self.config.owner_password_hash)
            if not valid:
                return PlainTextResponse("授权密码错误，请返回重试。", status_code=403)
        code = secrets.token_urlsafe(32)
        with self.store.transaction() as conn:
            # Atomically consume the browser request: double-submit cannot mint two codes.
            if not self.store.get(conn, "pending", request_id):
                return PlainTextResponse("授权请求已使用或过期。", status_code=400)
            self.store.delete(conn, request_id)
            if form.get("decision") == "allow":
                auth_code = AuthorizationCode(
                    code=code, expires_at=time.time() + 120, subject="owner",
                    **{key: pending[key] for key in ("client_id", "scopes", "code_challenge", "redirect_uri",
                                                    "redirect_uri_provided_explicitly", "resource")},
                )
                self.store.put(conn, "code", code, auth_code.model_dump(mode="json", exclude={"code"}), auth_code.expires_at)
        response = RedirectResponse(construct_redirect_uri(
            pending["redirect_uri"], state=pending["state"], iss=self.config.issuer,
            code=code if form.get("decision") == "allow" else None,
            error="access_denied" if form.get("decision") == "deny" else None,
        ), status_code=303)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        return response

    async def load_authorization_code(self, client, authorization_code):
        with self.store.transaction() as conn:
            data = self.store.get(conn, "code", authorization_code)
        return AuthorizationCode(code=authorization_code, **data) if data and data["client_id"] == client.client_id else None

    def _issue(self, conn, scopes, family=None, refresh_deadline=None):
        now = int(time.time())
        family = family or secrets.token_urlsafe(24)
        refresh_deadline = refresh_deadline or now + 30 * 86400
        access_deadline = min(now + 3600, refresh_deadline)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        common = {"client_id": self.config.client_id, "scopes": scopes, "resource": self.config.resource,
                  "subject": "owner", "family": family}
        self.store.put(conn, "access", access, {**common, "expires_at": access_deadline}, access_deadline, family)
        self.store.put(conn, "refresh", refresh,
                       {**common, "expires_at": refresh_deadline, "used": False}, refresh_deadline, family)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=access_deadline - now,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self.store.transaction() as conn:
            if not self.store.get(conn, "code", authorization_code.code):
                raise TokenError("invalid_grant", "code already used or expired")
            self.store.delete(conn, authorization_code.code)
            return self._issue(conn, authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token):
        with self.store.transaction() as conn:
            data = self.store.get(conn, "refresh", refresh_token)
        return RefreshToken(token=refresh_token, **data) if data and data["client_id"] == client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        replayed = False
        tokens = None
        with self.store.transaction() as conn:
            data = self.store.get(conn, "refresh", refresh_token.token)
            if not data:
                raise TokenError("invalid_grant", "refresh token expired or revoked")
            if data["used"]:
                conn.execute("DELETE FROM grants WHERE family=?", (data["family"],))
                replayed = True
            else:
                data["used"] = True  # retain a tombstone to detect replay and revoke the family
                self.store.put(conn, "refresh", refresh_token.token, data, data["expires_at"], data["family"])
                tokens = self._issue(conn, scopes, data["family"], data["expires_at"])
        if replayed:
            raise TokenError("invalid_grant", "refresh token replay; reconnect the client")
        return tokens

    async def load_access_token(self, token):
        with self.store.transaction() as conn:
            data = self.store.get(conn, "access", token)
        return AccessToken(token=token, **data) if data else None

    async def revoke_token(self, token):
        with self.store.transaction() as conn:
            kind = "refresh" if isinstance(token, RefreshToken) else "access"
            data = self.store.get(conn, kind, token.token)
            if data:
                conn.execute("DELETE FROM grants WHERE family=?", (data["family"],))
