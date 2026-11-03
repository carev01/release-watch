"""URL normalisation: one key per page, however the URL is spelled."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Signed or re-minted tokens (Azure SAS, Document360) and tracking params never identify a page.
DROPPED_PARAMS = {"st", "se", "sig", "sv", "sp", "sr"}


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in DROPPED_PARAMS and not k.lower().startswith("utm_")
    )
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def section_key(url: str, selector: str) -> str:
    """Key for one section of a multi-feed page (e.g. Keepit's #updates and #pmc)."""
    return f"{normalize_url(url)}#selector={selector}"
