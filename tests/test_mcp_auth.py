import base64
from dataclasses import replace
import hashlib
import os
import re
import secrets
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from starlette.testclient import TestClient

from mcp_app import create_app
from services.mcp_auth import MCPConfig, OwnerOAuthProvider, SCOPE, password_hash
from services.mcp_data import SnapshotReader
from scripts.mcp_admin import main as admin_main


class OAuthMCPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "a-test-owner-password-only"
        cls.hash = password_hash(cls.password)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = MCPConfig("https://mcp.example.test", "private-client", "s" * 43,
                                ("https://chatgpt.com/connector_platform_oauth_redirect",),
                                self.hash, Path(self.tmp.name) / "auth.db")
        self.reader = SnapshotReader(Path(self.tmp.name) / "business.db")
        self.client = self.open_client()
        self.addCleanup(self.client.__exit__, None, None, None)

    def open_client(self, config=None):
        client = TestClient(create_app(config or self.config, self.reader), base_url=self.config.base_url,
                            follow_redirects=False)
        client.__enter__()
        return client

    def start_authorization(self, **overrides):
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        params = {"client_id": self.config.client_id, "redirect_uri": self.config.redirect_uris[0],
                  "response_type": "code", "state": "opaque-client-state", "scope": SCOPE,
                  "code_challenge": challenge, "code_challenge_method": "S256", "resource": self.config.resource}
        params.update(overrides)
        response = self.client.get("/authorize", params=params)
        return response, verifier

    def form(self, **overrides):
        response, verifier = self.start_authorization(**overrides)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertTrue(response.headers["location"].startswith("/consent?"), response.headers)
        page = self.client.get(response.headers["location"])
        self.assertEqual(page.status_code, 200)
        self.assertIn("Secure", page.headers["set-cookie"])
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("form-action 'self' ", page.headers["content-security-policy"])
        self.assertIn("https://chatgpt.com", page.headers["content-security-policy"])
        fields = dict(re.findall(r'name="(request|csrf)" value="([^"]+)"', page.text))
        return {**fields, "password": self.password, "decision": "allow"}, verifier

    def code(self):
        form, verifier = self.form()
        response = self.client.post("/consent", data=form)
        self.assertEqual(response.status_code, 303, response.text)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(query["state"], ["opaque-client-state"])
        self.assertEqual(query["iss"], [self.config.issuer])
        return query["code"][0], verifier

    def exchange(self, code, verifier, **overrides):
        form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                "client_id": self.config.client_id, "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_uris[0], "resource": self.config.resource}
        form.update(overrides)
        return self.client.post("/token", data=form)

    def tokens(self):
        code, verifier = self.code()
        response = self.exchange(code, verifier)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def rpc(self, method, params=None, token=None, client=None):
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
        if token:
            headers["Authorization"] = "Bearer " + token
        return (client or self.client).post("/mcp", headers=headers,
                                            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_discovery_and_missing_auth_fail_closed(self):
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["issuer"], self.config.issuer)
        self.assertTrue(metadata["authorization_response_iss_parameter_supported"])
        self.assertEqual(metadata["code_challenge_methods_supported"], ["S256"])
        self.assertNotIn("registration_endpoint", metadata)
        self.assertEqual(self.client.post("/register", json={}).status_code, 404)
        response = self.rpc("tools/list")
        self.assertEqual(response.status_code, 401)
        url = re.search(r'resource_metadata="([^"]+)"', response.headers["www-authenticate"])[1]
        resource = self.client.get(url).json()
        self.assertEqual(resource["resource"], self.config.resource)
        self.assertEqual(resource["authorization_servers"], [self.config.issuer])
        self.assertEqual(self.rpc("tools/list", token="bogus").status_code, 401)

    def test_consent_password_csrf_replay_and_deny(self):
        form, _ = self.form()
        self.assertEqual(self.client.post("/consent", data={**form, "csrf": "wrong"}).status_code, 400)
        self.assertEqual(self.client.post("/consent", data={**form, "password": "wrong"}).status_code, 403)
        denied = self.client.post("/consent", data={**form, "decision": "deny", "password": ""})
        self.assertEqual(denied.status_code, 303)
        self.assertIn("error=access_denied", denied.headers["location"])
        self.assertIn("iss=", denied.headers["location"])
        self.assertEqual(self.client.post("/consent", data=form).status_code, 400)

    def test_client_redirect_resource_scope_and_pkce_binding(self):
        wrong, _ = self.start_authorization(redirect_uri="https://evil.test/callback")
        self.assertEqual(wrong.status_code, 400)
        self.assertNotIn("location", wrong.headers)
        for overrides in ({"resource": "https://evil.test/mcp"}, {"scope": "stocks:write"},
                          {"code_challenge_method": "plain"}):
            response, _ = self.start_authorization(**overrides)
            self.assertIn("error=", response.headers["location"])
            self.assertIn("iss=", response.headers["location"])
        code, verifier = self.code()
        self.assertEqual(self.exchange(code, verifier, client_secret="wrong").status_code, 401)
        self.assertEqual(self.exchange(code, "wrong").status_code, 400)
        self.assertEqual(self.exchange(code, verifier, resource="https://evil.test/mcp").status_code, 400)
        self.assertEqual(self.exchange(code, verifier, redirect_uri="https://evil.test/callback").status_code, 400)
        good = self.exchange(code, verifier)
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(self.exchange(code, verifier).status_code, 400)
        raw = self.config.auth_db.read_bytes()
        for secret in (code, good.json()["access_token"], good.json()["refresh_token"], self.password, self.config.client_secret):
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(self.config.auth_db.stat().st_mode & 0o777, 0o600)

    def test_protocol_tool_allowlist_annotations_and_no_web_routes(self):
        token = self.tokens()["access_token"]
        init = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}}, token)
        self.assertEqual(init.status_code, 200, init.text)
        response = self.rpc("tools/list", token=token)
        self.assertEqual(response.status_code, 200, response.text)
        tools = response.json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"get_stock_snapshot", "get_watchlist_snapshot",
                         "get_portfolio_overview", "get_watch_events", "get_service_status"})
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertEqual(tool["_meta"]["securitySchemes"][0]["scopes"], [SCOPE])
            self.assertEqual(tool["securitySchemes"][0]["scopes"], [SCOPE])
            self.assertEqual(tool["outputSchema"]["type"], "object")
        expected = self.reader.envelope({"database_readable": True})
        with patch.object(self.reader, "status", return_value=expected):
            result = self.rpc("tools/call", {"name": "get_service_status", "arguments": {}}, token).json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], expected)
        for path in ("/", "/api/portfolio_overview", "/update_portfolio", "/update_ai_provider", "/docs", "/openapi.json"):
            self.assertEqual(self.client.post(path).status_code, 404)
        unknown = self.rpc("tools/call", {"name": "update_portfolio", "arguments": {}}, token).json()["result"]
        self.assertTrue(unknown["isError"])
        invalid = self.rpc("tools/call", {"name": "get_watch_events", "arguments": {"limit": 100000}}, token).json()["result"]
        self.assertTrue(invalid["isError"])
        # Missing business DB returns sanitized tool error, never an empty/zero fake portfolio.
        error = self.rpc("tools/call", {"name": "get_portfolio_overview", "arguments": {}}, token).json()["result"]
        self.assertTrue(error["isError"])
        self.assertNotIn(self.tmp.name, str(error))

    def test_restart_refresh_rotation_and_replay_revokes_family(self):
        tokens = self.tokens()
        second = self.open_client()
        try:
            self.assertEqual(self.rpc("tools/list", token=tokens["access_token"], client=second).status_code, 200)
            form = {"grant_type": "refresh_token", "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret, "refresh_token": tokens["refresh_token"],
                    "resource": self.config.resource}
            narrowed = second.post("/token", data={**form, "scope": "stocks:write"})
            self.assertEqual(narrowed.status_code, 400)
            refreshed = second.post("/token", data=form)
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            newer = refreshed.json()
            self.assertNotEqual(newer["refresh_token"], tokens["refresh_token"])
            self.assertEqual(self.rpc("tools/list", token=newer["access_token"]).status_code, 200)
            self.assertEqual(second.post("/token", data=form).status_code, 400)
            self.assertEqual(self.rpc("tools/list", token=newer["access_token"]).status_code, 401)
            self.assertEqual(self.rpc("tools/list", token=tokens["access_token"]).status_code, 401)
        finally:
            second.__exit__(None, None, None)

    def test_expiry_resource_binding_revocation_and_config_rotation(self):
        tokens = self.tokens()
        provider = OwnerOAuthProvider(self.config)
        with provider.store.transaction() as conn:
            data = provider.store.get(conn, "access", tokens["access_token"])
            provider.store.put(conn, "access", tokens["access_token"], {**data, "resource": "https://other.test/mcp"}, time.time() + 100)
        self.assertEqual(self.rpc("tools/list", token=tokens["access_token"]).status_code, 401)
        tokens = self.tokens()
        revoked = self.client.post("/revoke", data={"token": tokens["refresh_token"],
                                  "client_id": self.config.client_id, "client_secret": self.config.client_secret})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.rpc("tools/list", token=tokens["access_token"]).status_code, 401)
        tokens = self.tokens()
        rotated = self.open_client(replace(self.config, client_secret="new" * 16))
        try:
            self.assertEqual(self.rpc("tools/list", token=tokens["access_token"], client=rotated).status_code, 401)
        finally:
            rotated.__exit__(None, None, None)
        with provider.store.transaction() as conn:
            conn.execute("UPDATE grants SET expires=?", (time.time() - 1,))
        self.assertEqual(self.rpc("tools/list", token=tokens["access_token"]).status_code, 401)

    def test_host_origin_body_limits_and_login_throttling(self):
        self.assertEqual(self.client.get("/consent", headers={"Host": "evil.test"}).status_code, 421)
        self.assertEqual(self.client.get("/consent", headers={"Origin": "https://evil.test"}).status_code, 403)
        self.assertEqual(self.client.post("/token", content="x" * 17000).status_code, 413)
        form, _ = self.form()
        for _ in range(10):
            self.assertEqual(self.client.post("/consent", data={**form, "password": "wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/consent", data=form).status_code, 429)

    def test_desktop_loopback_callback_with_public_https_issuer(self):
        callback = "http://127.0.0.1:18849/callback"
        self.config = replace(self.config, redirect_uris=(callback, *self.config.redirect_uris))
        desktop = self.open_client()
        self.client = desktop
        try:
            tokens = self.tokens()
            self.assertEqual(self.rpc("tools/list", token=tokens["access_token"]).status_code, 200)
            for unregistered in ("http://127.0.0.1:18850/callback", "http://127.0.0.1:18849/other"):
                response, _ = self.start_authorization(redirect_uri=unregistered)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("location", response.headers)
        finally:
            desktop.__exit__(None, None, None)

    def test_http_callback_exception_is_only_explicit_loopback(self):
        for callback in ("http://evil.test/callback", "http://192.168.1.1:18849/callback",
                         "http://127.0.0.1.evil.test/callback", "http://127.1:18849/callback",
                         "http://127.0.0.1:18849/callback#fragment", "http://user@127.0.0.1:18849/callback"):
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                replace(self.config, redirect_uris=(callback,))

    def test_missing_or_unsafe_configuration_refuses_to_start(self):
        for values in ({"base_url": "http://public.test"}, {"client_secret": "short"},
                       {"redirect_uris": ()}, {"owner_password_hash": "plaintext"},
                       {"redirect_uris": ("https://chatgpt.com/cb#fragment",)}):
            with self.assertRaises(ValueError):
                replace(self.config, **values)
        with self.assertRaises(ValueError):
            create_app(replace(self.config, auth_db=self.reader.db_path), self.reader)

    def test_static_bearer_tokens_work_alongside_oauth_and_keep_tools_read_only(self):
        fixed = ("fixed-token-one-" + "a" * 32, "fixed-token-two-" + "b" * 32)
        client = self.open_client(replace(self.config, bearer_tokens=fixed))
        try:
            for token in fixed:
                result = self.rpc("tools/list", token=token, client=client)
                self.assertEqual(result.status_code, 200, result.text)
                tools = result.json()["result"]["tools"]
                self.assertEqual(len(tools), 5)
                self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
                expected = self.reader.envelope({"database_readable": True})
                with patch.object(self.reader, "status", return_value=expected):
                    result = self.rpc("tools/call", {"name": "get_service_status", "arguments": {}}, token, client).json()["result"]
                self.assertEqual(result["structuredContent"], expected)
            oauth = self.tokens()
            self.assertEqual(self.rpc("tools/list", token=oauth["access_token"], client=client).status_code, 200)
            for token in (None, "wrong-token", self.config.client_secret):
                self.assertEqual(self.rpc("tools/list", token=token, client=client).status_code, 401)
            self.assertEqual(client.get("/mcp", params={"access_token": fixed[0]}).status_code, 401)
            self.assertEqual(client.post("/update_portfolio", headers={"Authorization": "Bearer " + fixed[0]}).status_code, 404)
            # A static key is not an OAuth refresh token and must never enter the grant exchange.
            response = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": fixed[0],
                                   "client_id": self.config.client_id, "client_secret": self.config.client_secret})
            self.assertEqual(response.status_code, 400)
            with patch("scripts.mcp_admin.MCPConfig.from_env", return_value=replace(self.config, bearer_tokens=fixed)), \
                    patch("sys.argv", ["mcp_admin", "revoke"]), patch("builtins.print"):
                admin_main()
            self.assertEqual(self.rpc("tools/list", token=oauth["access_token"], client=client).status_code, 401)
            self.assertEqual(self.rpc("tools/list", token=fixed[0], client=client).status_code, 200)
        finally:
            client.__exit__(None, None, None)

    def test_token_only_configuration_needs_no_password_callback_or_auth_database(self):
        token = "standalone-token-" + "c" * 32
        path = Path(self.tmp.name) / "unused-auth.db"
        config = replace(self.config, client_secret="", owner_password_hash="", redirect_uris=(),
                         bearer_tokens=(token,), auth_db=path)
        client = self.open_client(config)
        try:
            self.assertFalse(path.exists())
            self.assertEqual(self.rpc("tools/list", token=token, client=client).status_code, 200)
            self.assertEqual(self.rpc("tools/list", client=client).status_code, 401)
            for path_ in ("/authorize", "/consent", "/token", "/revoke", "/.well-known/oauth-authorization-server"):
                self.assertEqual(client.get(path_).status_code, 404)
            metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
            self.assertEqual(metadata["resource"], config.resource)
            self.assertNotIn("authorization_servers", metadata)
            tools = self.rpc("tools/list", token=token, client=client).json()["result"]["tools"]
            self.assertTrue(all("securitySchemes" not in tool for tool in tools))
            with patch("scripts.mcp_admin.MCPConfig.from_env", return_value=config), \
                    patch("sys.argv", ["mcp_admin", "revoke"]), patch("builtins.print"):
                admin_main()
            self.assertFalse(path.exists())
        finally:
            client.__exit__(None, None, None)

    def test_static_token_rotation_does_not_invalidate_oauth_or_other_tokens(self):
        first, second = "rotated-token-" + "a" * 32, "retained-token-" + "b" * 32
        oauth = self.tokens()
        before = self.open_client(replace(self.config, bearer_tokens=(first, second)))
        try:
            self.assertEqual(self.rpc("tools/list", token=first, client=before).status_code, 200)
        finally:
            before.__exit__(None, None, None)
        after = self.open_client(replace(self.config, bearer_tokens=(second,)))
        try:
            self.assertEqual(self.rpc("tools/list", token=first, client=after).status_code, 401)
            for token in (second, oauth["access_token"]):
                self.assertEqual(self.rpc("tools/list", token=token, client=after).status_code, 200)
        finally:
            after.__exit__(None, None, None)

    def test_static_tokens_reject_weak_values_reused_oauth_secrets_and_partial_oauth(self):
        for tokens in (("short",), ("Bearer " + "x" * 43,), (self.config.client_secret,), ("x" * 300,)):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                replace(self.config, bearer_tokens=tokens)
        with self.assertRaises(ValueError):
            replace(self.config, client_secret="", owner_password_hash="", redirect_uris=(), bearer_tokens=())
        with self.assertRaises(ValueError):
            replace(self.config, owner_password_hash="", bearer_tokens=("valid-token-" + "z" * 32,))

    def test_token_only_environment_parses_multiple_keys_without_oauth_credentials(self):
        first, second = "environment-key-" + "d" * 32, "environment-key-" + "e" * 32
        with patch.dict(os.environ, {"MCP_PUBLIC_URL": self.config.base_url, "MCP_CLIENT_ID": "8848-chatgpt",
                                     "MCP_CLIENT_SECRET": "", "MCP_OWNER_PASSWORD_HASH": "", "MCP_REDIRECT_URIS": "",
                                     "MCP_AUTH_DB": str(Path(self.tmp.name) / "unused-env-auth.db"),
                                     "MCP_BEARER_TOKENS": f" {first}, , {second}, {first} "}):
            config = MCPConfig.from_env()
            self.assertEqual(config.bearer_tokens, (first, second))
            self.assertFalse(config.oauth_enabled)
            self.assertNotIn(first, repr(config))
            with patch.dict(os.environ, {"MCP_BEARER_TOKENS": ", ,"}), self.assertRaises(ValueError):
                MCPConfig.from_env()


if __name__ == "__main__":
    unittest.main()
