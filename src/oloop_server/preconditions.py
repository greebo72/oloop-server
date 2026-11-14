"""The If-None-Match / If-Match of a write (spec/SERVER.md, "API v1")."""
import re

from fastapi import Request

from oloop_server.errors import ApiError

_REVISION = re.compile(r'^(?:W/)?"?(\d{1,9})"?$')


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def read_precondition(request: Request) -> int | None:
    """None for `If-None-Match: *` (a new item), or the revision of `If-Match`.

    Exactly one of the two has to be there.
    """
    if_none_match = request.headers.get("if-none-match")
    if_match = request.headers.get("if-match")

    if (if_none_match is None) == (if_match is None):
        raise _bad("exactly one of If-None-Match: * and If-Match is required")

    if if_none_match is not None:
        if if_none_match.strip() != "*":
            raise _bad("If-None-Match must be *")
        return None

    match = _REVISION.match(if_match.strip())
    if match is None:
        raise _bad("If-Match must be the revision in quotes")
    return int(match.group(1))
