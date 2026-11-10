"""Local-only owner password hashing and emergency OAuth grant revocation."""
import argparse
import getpass
import sqlite3

from services.mcp_auth import MCPConfig, password_hash


def main():
    parser = argparse.ArgumentParser(description="8848 私人 MCP 管理（请在项目根目录用 python -m scripts.mcp_admin 运行）")
    parser.add_argument("action", choices=["password-hash", "revoke"])
    args = parser.parse_args()
    if args.action == "password-hash":
        password = getpass.getpass("设置账户授权密码（至少16字符，不是客户端密钥）：")
        if len(password) < 16 or len(password) > 1024:
            parser.error("密码长度须为16–1024字符")
        if password != getpass.getpass("再次输入："):
            parser.error("两次输入不一致")
        print("MCP_OWNER_PASSWORD_HASH=" + password_hash(password))
    else:
        settings = MCPConfig.from_env()
        # mode=rw refuses a typo/missing file instead of creating an empty auth store.
        conn = sqlite3.connect(settings.auth_db.as_uri() + "?mode=rw", uri=True)
        try:
            with conn:
                conn.execute("DELETE FROM grants")
            print("全部授权请求、授权码、访问和刷新令牌已撤销；客户端需重新授权。")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
