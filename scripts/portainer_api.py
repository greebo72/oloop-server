"""What the Portainer scripts of this repo have in common: the connection.

The token is read from the environment, never from an argument (an argument is
visible to every process on the computer), and is never printed. The address
has to be https; http is only accepted for the computer itself, so the token
does not travel in clear text.
"""
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

LOOPBACK = ("localhost", "127.0.0.1", "::1")


class Failure(Exception):
    """Something the user has to know. `code` is the exit status of the script."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def check_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Failure(f"'{url}' is not an address. Use https://host", 2)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Failure("the address must not contain a user, a password, a query or a fragment", 2)
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK:
        raise Failure("the address has to be https: the key would travel in clear text", 2)
    return url.strip().rstrip("/")


class Portainer:
    def __init__(self, url: str, key: str, cacert: str | None = None):
        self.url = url
        self._key = key
        try:
            self._context = ssl.create_default_context(cafile=cacert) if cacert else None
        except (OSError, ssl.SSLError) as error:
            raise Failure(f"PORTAINER_CACERT cannot be used: {error}", 2) from None

    def open(self, method: str, path: str, body: bytes | None = None,
             content_type: str | None = None, timeout: float = 900):
        headers = {"X-API-Key": self._key}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            return urllib.request.urlopen(request, context=self._context, timeout=timeout)
        except urllib.error.HTTPError as error:
            raise Failure(self._explain(error)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise Failure(f"Portainer cannot be reached: {reason}") from None

    def json(self, method: str, path: str, payload=None, timeout: float = 120):
        """A call with a JSON body (if any) and a JSON answer (None when empty)."""
        body = json.dumps(payload).encode() if payload is not None else None
        with self.open(method, path, body, "application/json" if body else None, timeout) as response:
            text = response.read().decode(errors="replace")
        return json.loads(text) if text.strip() else None

    def text(self, method: str, path: str, timeout: float = 60) -> bytes:
        with self.open(method, path, timeout=timeout) as response:
            return response.read()

    @staticmethod
    def _explain(error: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            detail = json.loads(error.read().decode(errors="replace")).get("message", "")
        except (ValueError, AttributeError):
            pass
        hints = {
            401: "the API key is not accepted",
            403: "the user of the API key may not do this",
            404: "not found, is the environment id right?",
        }
        text = hints.get(error.code, f"Portainer answered {error.code}")
        return f"{text}{': ' + detail if detail else ''}"

    def environments(self) -> list[dict]:
        return self.json("GET", "/api/endpoints")


def require_token() -> str:
    key = os.environ.get("PORTAINER_TOKEN", "").strip()
    if not key:
        raise Failure("PORTAINER_TOKEN is not set. Make a token in Portainer "
                      "(My account, Access tokens) and put it in the environment.", 2)
    return key


def connect(url: str | None) -> Portainer:
    key = require_token()
    if not url:
        raise Failure("PORTAINER_URL is not set (or give --url)", 2)
    return Portainer(check_url(url), key, os.environ.get("PORTAINER_CACERT"))


def pick_environment(portainer: Portainer, wanted: str | None) -> int:
    if wanted:
        if not wanted.isdigit():
            raise Failure("the environment id is a number", 2)
        return int(wanted)

    found = portainer.environments()
    if len(found) == 1:
        return int(found[0]["Id"])

    listing = "\n".join(f"  {e['Id']}  {e.get('Name', '')}" for e in found) or "  (none)"
    raise Failure("which environment? Give PORTAINER_ENV_ID or --env-id:\n" + listing, 2)


def reject_unknown(unknown: list[str]) -> None:
    """Names only: an unknown argument may be a key that was typed by mistake."""
    if unknown:
        names = ", ".join(a.split("=", 1)[0] for a in unknown if a.startswith("-"))
        raise Failure(f"unknown option {names or '(an argument)'}. "
                      "The key is only read from PORTAINER_TOKEN.", 2)
