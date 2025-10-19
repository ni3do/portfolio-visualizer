#!/usr/bin/env python3

import re
from pathlib import Path

CONFIG_PATH = Path.home() / ".codex" / "config.toml"
REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS_DIR = REPO_ROOT / "secrets"


def build_mcp_block() -> str:
    env_lines = [
        'PORTFOLIO_DB_HOST = "localhost"',
        'PORTFOLIO_DB_PORT = "5432"',
        'PORTFOLIO_DB_NAME = "portfolio"',
        f'PORTFOLIO_DB_USER_FILE = "{(SECRETS_DIR / "postgres_app_user").resolve()}"',
        f'PORTFOLIO_DB_PASSWORD_FILE = "{(SECRETS_DIR / "postgres_app_password").resolve()}"',
    ]
    block_lines = [
        '[mcp_servers."portfolio-db"]',
        'command = "uv"',
        'args = ["run", "portfolio-mcp"]',
        "",
        '[mcp_servers."portfolio-db".env]',
        *env_lines,
    ]
    return "\n".join(block_lines)


def remove_section(content: str, header: str) -> str:
    pattern = re.compile(rf"\[{re.escape(header)}\][\s\S]*?(?=\n\[|\Z)", re.MULTILINE)
    return pattern.sub("", content)


def ensure_block(content: str) -> str:
    updated = content
    for header in (
        'mcp_servers."portfolio-db"',
        'mcp_servers."portfolio-db".env',
    ):
        updated = remove_section(updated, header)

    updated = updated.rstrip()
    mcp_block = build_mcp_block()
    if updated:
        updated = f"{updated}\n\n{mcp_block}\n"
    else:
        updated = f"{mcp_block}\n"
    return updated


def main() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = ""
    if CONFIG_PATH.exists():
        content = CONFIG_PATH.read_text(encoding="utf-8")

    updated = ensure_block(content)
    CONFIG_PATH.write_text(updated, encoding="utf-8")
    print(f"Updated {CONFIG_PATH}")


if __name__ == "__main__":
    main()
