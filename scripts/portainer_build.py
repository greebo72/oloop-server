#!/usr/bin/env python3
"""Builds the image of oloop-server on a Docker host that Portainer manages.

It packs the build context (scripts/build-context.sh), sends it to Portainer,
which hands it to the Docker engine of the environment, and checks that the
image exists afterwards. Python standard library only.

Settings come from the environment, never from an argument (an argument is
visible to every process on the computer):

  PORTAINER_TOKEN     an access token of Portainer (My account, Access tokens).
                      It has the rights of the user that made it.
  PORTAINER_URL       https://portainer.example.org  (or --url)
  PORTAINER_ENV_ID    the environment to build on (or --env-id). Left out, the
                      only environment is taken; with several, they are listed.
  PORTAINER_CACERT    a file with the certificate authority, for a Portainer
                      whose certificate is not signed by a known one. The
                      certificate is always checked.

The address has to be https. http is only accepted for the computer itself.

Exit status: 0 built, 1 the build or the connection failed, 2 wrong use.
"""
import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOPBACK = ("localhost", "127.0.0.1", "::1")


class Failure(Exception):
    """Something the user has to know. `code` is the exit status."""

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
        with self.open("GET", "/api/endpoints") as response:
            return json.load(response)


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


def make_archive() -> Path:
    done = subprocess.run([str(ROOT / "scripts" / "build-context.sh")],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise Failure(f"the build context could not be packed: {done.stderr.strip()}")
    return ROOT / done.stdout.strip().splitlines()[-1]


def default_tag(archive: Path) -> str:
    match = re.match(r"^oloop-server-(.+)\.tar\.gz$", archive.name)
    return f"oloop-server:{match.group(1)}" if match else "oloop-server:latest"


def build(portainer: Portainer, env_id: int, archive: Path, tag: str, pull: bool) -> None:
    query = {"t": tag, "rm": "true", "forcerm": "true"}
    if pull:
        query["pull"] = "true"
    path = f"/api/endpoints/{env_id}/docker/build?{urllib.parse.urlencode(query)}"

    failed = None
    with portainer.open("POST", path, archive.read_bytes(), "application/x-tar") as response:
        for raw in response:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                if message.get("stream"):
                    print(message["stream"], end="", flush=True)
                # Docker answers 200 even when the build fails: the failure is in the stream.
                problem = message.get("error") or (message.get("errorDetail") or {}).get("message")
                if problem:
                    failed = problem
    if failed:
        raise Failure(f"the build failed: {failed}")

    quoted = urllib.parse.quote(tag, safe=":/")
    try:
        portainer.open("GET", f"/api/endpoints/{env_id}/docker/images/{quoted}/json").close()
    except Failure as error:
        raise Failure(f"the build ended, but the image {tag} is not there: {error}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("PORTAINER_URL"))
    parser.add_argument("--env-id", default=os.environ.get("PORTAINER_ENV_ID"))
    parser.add_argument("--tag", help="name of the image, default oloop-server:<revision>")
    parser.add_argument("--archive", type=Path,
                        help="a build context made before, instead of packing a new one")
    parser.add_argument("--pull", action="store_true", help="fetch a newer base image first")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        # Only the names: an unknown argument may be a key that was typed by mistake.
        names = ", ".join(a.split("=", 1)[0] for a in unknown if a.startswith("-"))
        print(f"portainer_build: unknown option {names or '(an argument)'}. "
              "The key is only read from PORTAINER_TOKEN.", file=sys.stderr)
        return 2

    try:
        key = os.environ.get("PORTAINER_TOKEN", "").strip()
        if not key:
            raise Failure("PORTAINER_TOKEN is not set. Make a token in Portainer "
                          "(My account, Access tokens) and put it in the environment.", 2)
        if not args.url:
            raise Failure("PORTAINER_URL is not set (or give --url)", 2)

        portainer = Portainer(check_url(args.url), key, os.environ.get("PORTAINER_CACERT"))
        env_id = pick_environment(portainer, args.env_id)
        archive = args.archive or make_archive()
        if not archive.is_file():
            raise Failure(f"{archive} does not exist", 2)
        tag = args.tag or default_tag(archive)

        print(f"building {tag} on environment {env_id} from {archive.name}", file=sys.stderr)
        build(portainer, env_id, archive, tag, args.pull)
        print(f"\nbuilt {tag}", file=sys.stderr)
        return 0
    except Failure as error:
        print(f"portainer_build: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
