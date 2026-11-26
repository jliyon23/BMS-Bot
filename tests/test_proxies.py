from __future__ import annotations

from bmsbot.proxies.crypto import CredentialCipher, build_proxy_url, parse_proxy_url
from bmsbot.proxies.sources import normalize_proxy


def test_normalize_public_proxies() -> None:
    assert normalize_proxy("https://8.8.8.8:8080", source="test").url == "http://8.8.8.8:8080"
    assert normalize_proxy("socks5h://8.8.8.8:1080", source="test").url == "socks5://8.8.8.8:1080"
    assert normalize_proxy("not-a-proxy", source="test") is None
    assert normalize_proxy("socks4://8.8.8.8:1080", source="test") is None


def test_private_ips_are_rejected() -> None:
    assert normalize_proxy("http://192.168.1.10:8080", source="test") is None
    assert normalize_proxy("http://127.0.0.1:8080", source="test") is None


def test_proxy_url_round_trip_with_credentials() -> None:
    parsed = parse_proxy_url("http://user:pa%40ss@1.2.3.4:8080")
    assert parsed.host == "1.2.3.4"
    assert parsed.port == 8080
    assert parsed.credentials == "user:pa@ss"
    assert build_proxy_url(parsed.scheme, parsed.host, parsed.port, parsed.credentials) == (
        "http://user:pa%40ss@1.2.3.4:8080"
    )


def test_credential_cipher() -> None:
    from cryptography.fernet import Fernet

    cipher = CredentialCipher(Fernet.generate_key().decode())
    token = cipher.encrypt("user:pass")
    assert token != "user:pass"
    assert cipher.decrypt(token) == "user:pass"
