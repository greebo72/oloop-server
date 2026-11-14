"""base64url as the API uses it: no padding, one spelling for one value."""
import base64
import re

_ALPHABET = re.compile(r"^[A-Za-z0-9_-]*$")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    """Decodes `text`, or raises ValueError when it is not base64url without
    padding in its shortest form (a changed last character that decodes to the
    same bytes is refused, too)."""
    if not _ALPHABET.match(text) or len(text) % 4 == 1:
        raise ValueError("not base64url without padding")

    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url_encode(data) != text:
        raise ValueError("not base64url in its shortest form")
    return data
