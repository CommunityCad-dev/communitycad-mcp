"""Committed fixtures must not carry real hosts, accounts or tokens."""

import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
ALLOWED_HOSTS = {"api.example.test", "example.test"}
TOKENISH = re.compile(r"\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9_\-]{40,}|eyJ[A-Za-z0-9_\-]+\.)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def test_fixtures_are_scrubbed():
    files = list(FIXTURES.rglob("*.json"))
    assert files
    for f in files:
        text = f.read_text()
        for host in re.findall(r"https?://([^/\s\"?:]+)", text):
            assert host in ALLOWED_HOSTS, f"{f.name}: host {host}"
        assert not TOKENISH.search(text), f"{f.name}: token-like string"
        assert not EMAIL.search(text), f"{f.name}: email address"
        assert not IP.search(text), f"{f.name}: IP address"
        assert "localhost" not in text, f.name
        for m in re.findall(r'"username": "([^"]*)"', text):
            assert m.startswith("example-user"), f"{f.name}: username {m}"
