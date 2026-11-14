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
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portainer_api import (
    Failure,
    Portainer,
    connect,
    pick_environment,
    reject_unknown,
)

ROOT = Path(__file__).resolve().parent.parent


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

    try:
        reject_unknown(unknown)
        portainer = connect(args.url)
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
