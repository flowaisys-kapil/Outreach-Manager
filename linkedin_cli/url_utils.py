import re
from urllib.parse import unquote


def url_to_public_id(url: str | None) -> str | None:
    """Extract public identifier from a LinkedIn profile URL."""
    if not url or not isinstance(url, str):
        return None
    clean_url = url.split("?")[0].rstrip("/")
    if not clean_url:
        return None
    match = re.search(r"/in/([^/]+)", clean_url)
    if match:
        pid = match.group(1).strip()
        return unquote(pid) if pid else None
    return None


def public_id_to_url(public_id: str | None) -> str:
    """Convert a LinkedIn public identifier to a canonical profile URL."""
    if not public_id:
        return ""
    clean_id = public_id.strip().strip("/")
    if not clean_id:
        return ""
    if clean_id.startswith("http"):
        return clean_id
    return f"https://www.linkedin.com/in/{clean_id}/"
