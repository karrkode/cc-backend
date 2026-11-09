"""CC-191: refuse the passwords people pick most often when a password is set (sign-up, reset, change).

Passwords already in use keep working; the check only runs when a new one is chosen.

data/common-passwords.txt holds the 10,000 most common passwords of 8 or more characters (the shortest
we accept), lower-cased, in order. They come from the NCSC's list of the 100,000 passwords seen most often
in breaches, as published in SecLists (github.com/danielmiessler/SecLists, MIT licence, (c) Daniel Miessler).
"""
from functools import cache
from pathlib import Path

LIST = Path(__file__).parent / "data" / "common-passwords.txt"
TOO_COMMON = "That password is too common. Choose one that's harder to guess"


@cache
def _common() -> frozenset[str]:
    return frozenset(line for line in LIST.read_text(encoding="utf-8").splitlines() if line)


def is_common(password: str) -> bool:
    """Case doesn't matter: "Password1" is as easy to guess as "password1"."""
    return password.lower() in _common()
