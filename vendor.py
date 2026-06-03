"""Offline MAC -> manufacturer lookup, plus randomized-MAC detection."""
import gzip
import os

_OUI = {}
_LOADED = False


def _load():
    global _LOADED
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oui.csv.gz")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                pfx, _, name = line.partition("\t")
                if name:
                    _OUI[pfx.strip()] = name.strip()
    except FileNotFoundError:
        pass
    _LOADED = True


def is_randomized(mac):
    """True if the locally-administered bit is set (private/randomized MAC)."""
    try:
        first = int(mac.split(":")[0], 16)
    except (ValueError, IndexError):
        return False
    return bool(first & 0x02)


def lookup(mac):
    """Return a manufacturer name, 'Private (randomized)', or '' if unknown."""
    if not _LOADED:
        _load()
    if not mac:
        return ""
    if is_randomized(mac):
        return "Private (randomized)"
    prefix = mac.replace(":", "").replace("-", "").upper()[:6]
    return _OUI.get(prefix, "")
