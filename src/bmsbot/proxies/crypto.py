from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode()) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, value: str) -> str:
        if not self._fernet:
            raise ValueError("BMS_PROXY_ENCRYPTION_KEY is not configured")
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        if not self._fernet:
            raise ValueError("BMS_PROXY_ENCRYPTION_KEY is not configured")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Could not decrypt proxy credentials") from exc


@dataclass(frozen=True)
class ParsedProxyURL:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def credentials(self) -> str | None:
        if self.username is None:
            return None
        return f"{self.username}:{self.password or ''}"


def parse_proxy_url(value: str) -> ParsedProxyURL:
    raw = value.strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("Unsupported proxy scheme")
    if not parsed.hostname:
        raise ValueError("Proxy host is missing")
    try:
        port = parsed.port or (1080 if parsed.scheme.startswith("socks") else 80)
    except ValueError as exc:
        raise ValueError("Proxy port is invalid") from exc
    scheme = "socks5" if parsed.scheme.startswith("socks5") else "http"
    return ParsedProxyURL(
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
    )


def build_proxy_url(scheme: str, host: str, port: int, credentials: str | None) -> str:
    if credentials:
        username, _, password = credentials.partition(":")
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return f"{scheme}://{auth}{host}:{port}"
    return f"{scheme}://{host}:{port}"
