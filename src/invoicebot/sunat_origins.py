"""The SUNAT origins on which SOL credentials may be typed (R-9.10).

The list is hard-coded in source on purpose: ``config/sunat_sol.yaml`` cannot
widen it, so a changed config file never moves the credentials off SUNAT.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: The SOL fetcher's name and the name of its secrets view (R-5.7, R-9.10).
SUNAT_SOL = "sunat_sol"

#: The exact ``scheme://host`` origins SOL signs in and queries on (R-9.10).
SUNAT_ORIGINS = frozenset({"https://api-seguridad.sunat.gob.pe", "https://e-menu.sunat.gob.pe"})

_ALLOWED_PORTS = (None, 443)


def is_sunat_origin(url: str) -> bool:
    """True only for an ``https`` URL on one of :data:`SUNAT_ORIGINS` (R-9.10).

    A look-alike host, a plain-http URL, userinfo, or any port other than none
    or 443 is refused. Anything ``urlsplit`` cannot parse is refused too, so
    the guard never raises.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return False
    if port not in _ALLOWED_PORTS:
        return False
    hostname = parts.hostname
    return hostname is not None and f"https://{hostname.lower()}" in SUNAT_ORIGINS
