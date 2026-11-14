#!/usr/bin/env python3
"""Creates or updates the stack of oloop-server on a Docker host that Portainer manages.

It sends docker-compose.yml (which is not part of the repo: it describes one
installation) to Portainer, with the settings of the stack, and then waits until
the server is healthy. Before it changes anything it checks that the image and
the network of the reverse proxy exist on the environment. Python standard
library only.

Connection, from the environment (see portainer_api.py): PORTAINER_TOKEN,
PORTAINER_URL, PORTAINER_ENV_ID, PORTAINER_CACERT.

Settings of the stack:
  OLOOP_IMAGE            --image, else the newest oloop-server:* image of the
                         environment (build one with portainer_build.py).
  OLOOP_TRUSTED_PROXIES  the subnet of the network of the reverse proxy,
                         unless set in the environment or already in the stack.
                         The network is the external network of the compose file
                         (--network names it when there are several).
  OLOOP_STORAGE, OLOOP_UID, OLOOP_GID
                         only when set in the environment; the compose file has
                         the defaults.
  POSTGRES_PASSWORD      made at random when the stack is created, or taken from
                         the environment. It is kept in the stack in Portainer
                         and never printed. A later run keeps the one that is
                         there: the database has its own and does not follow.
A variable that someone added to the stack in Portainer is kept.

--dry-run shows what would happen and changes nothing.

Exit status: 0 done, 1 failed, 2 wrong use.
"""
import argparse
import json
import os
import re
import secrets
import sys
import time
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
SECRETS = ("POSTGRES_PASSWORD",)
FROM_ENVIRONMENT = ("OLOOP_STORAGE", "OLOOP_UID", "OLOOP_GID", "OLOOP_TRUSTED_PROXIES")
IMAGE_PREFIX = "oloop-server:"
POLL_SECONDS = 3.0


# ---------------- The Docker engine behind Portainer ----------------

def docker(portainer: Portainer, env_id: int, path: str):
    return portainer.json("GET", f"/api/endpoints/{env_id}/docker{path}")


def newest_image(portainer: Portainer, env_id: int) -> str:
    found = []
    for image in docker(portainer, env_id, "/images/json") or []:
        for tag in image.get("RepoTags") or []:
            if tag.startswith(IMAGE_PREFIX):
                found.append((image.get("Created", 0), tag))
    if not found:
        raise Failure(f"there is no {IMAGE_PREFIX}* image on this environment. "
                      "Build one with portainer_build.py, or name one with --image.")
    return max(found)[1]


def check_image(portainer: Portainer, env_id: int, image: str) -> None:
    try:
        docker(portainer, env_id, f"/images/{urllib.parse.quote(image, safe=':/')}/json")
    except Failure as error:
        raise Failure(f"the image {image} is not on this environment ({error}). "
                      "Build it with portainer_build.py.") from None


def network_subnets(portainer: Portainer, env_id: int, name: str) -> list[str]:
    try:
        network = docker(portainer, env_id, f"/networks/{urllib.parse.quote(name)}")
    except Failure as error:
        raise Failure(f"the network {name} is not on this environment ({error}). It is the "
                      "network of the reverse proxy and has to exist.") from None
    return [c["Subnet"] for c in (network.get("IPAM") or {}).get("Config") or [] if c.get("Subnet")]


def external_networks(compose: str) -> list[str]:
    """The names of the external networks of a compose file.

    The compose file says which networks exist already, and the one of the
    reverse proxy is among them. This reads the top-level `networks:` block
    without a YAML library: an entry is external when it says `external: true`
    (also in the older form `external:` with a `name:` below, and as `{ ... }`
    on one line); a `name:` is the real name of the network. Networks of a
    service are not read.
    """
    names: list[str] = []
    entry: dict | None = None
    entry_indent: int | None = None
    in_block = False

    def finish() -> None:
        if entry and entry["external"]:
            names.append(entry["name"] or entry["key"])

    for raw in compose.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():  # a key of the top level
            finish()
            entry, entry_indent = None, None
            in_block = line.strip() == "networks:"
            continue
        if not in_block:
            continue

        indent = len(line) - len(line.lstrip())
        text = line.strip()
        if entry_indent is None:
            entry_indent = indent
        if indent == entry_indent:  # a network
            finish()
            key, _, rest = text.partition(":")
            entry = {"key": key.strip("'\""), "external": False, "name": None}
            inline = rest.strip()
            if re.search(r"external:\s*true\b", inline, re.IGNORECASE):
                entry["external"] = True
            named = re.search(r"name:\s*['\"]?([^'\",}\s]+)", inline)
            if named:
                entry["name"] = named.group(1)
        elif entry is not None:  # a setting of the network
            if re.fullmatch(r"external:\s*(true)?", text, re.IGNORECASE):
                entry["external"] = True
            named = re.fullmatch(r"name:\s*['\"]?([^'\"\s]+)['\"]?", text)
            if named:
                entry["name"] = named.group(1)
    finish()
    return names


def logs_of(portainer: Portainer, env_id: int, container_id: str, tail: int = 30) -> str:
    raw = portainer.text("GET", f"/api/endpoints/{env_id}/docker/containers/{container_id}"
                                f"/logs?stdout=1&stderr=1&tail={tail}")
    return demux(raw)


def demux(data: bytes) -> str:
    """The log of a container without a terminal comes in frames: a byte for the
    stream, three zeros, the length (4 bytes), the text."""
    out, i = [], 0
    while i + 8 <= len(data) and data[i] in (0, 1, 2) and data[i + 1:i + 4] == b"\0\0\0":
        size = int.from_bytes(data[i + 4:i + 8], "big")
        out.append(data[i + 8:i + 8 + size])
        i += 8 + size
    return (b"".join(out) if out else data).decode(errors="replace")


# ---------------- The stack ----------------

def find_stack(portainer: Portainer, env_id: int, name: str) -> dict | None:
    for stack in portainer.json("GET", "/api/stacks") or []:
        if stack.get("Name") == name and stack.get("EndpointId") == env_id:
            return stack
    return None


def stack_environment(stack: dict | None) -> dict[str, str]:
    pairs = (stack or {}).get("Env") or (stack or {}).get("env") or []
    return {p["name"]: p.get("value", "") for p in pairs if p.get("name")}


def compose_default(compose: str, name: str) -> str | None:
    match = re.search(r"\$\{" + re.escape(name) + r":-([^}]*)\}", compose)
    return match.group(1) if match else None


def merge_environment(existing: dict[str, str] | None, image: str, subnets: list[str],
                      environ: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """The environment of the stack, and for each name what happens to it."""
    creating = existing is None
    env = dict(existing or {})
    what: dict[str, str] = {}

    def put(name: str, value: str, kind_new: str, kind_changed: str = "changed"):
        what[name] = kind_new if name not in env else (kind_changed if env[name] != value else "kept")
        env[name] = value

    put("OLOOP_IMAGE", image, "new")
    for name in FROM_ENVIRONMENT:
        if environ.get(name):
            put(name, environ[name], "new")
    if not env.get("OLOOP_TRUSTED_PROXIES") and subnets:
        put("OLOOP_TRUSTED_PROXIES", ",".join(subnets), "new")

    if "POSTGRES_PASSWORD" not in env or not env["POSTGRES_PASSWORD"]:
        if creating:
            given = environ.get("POSTGRES_PASSWORD")
            env["POSTGRES_PASSWORD"] = given or secrets.token_urlsafe(32)
            what["POSTGRES_PASSWORD"] = "from the environment" if given else "made at random"
        elif environ.get("POSTGRES_PASSWORD"):
            env["POSTGRES_PASSWORD"] = environ["POSTGRES_PASSWORD"]
            what["POSTGRES_PASSWORD"] = "from the environment"
        else:
            raise Failure("the stack has no POSTGRES_PASSWORD. The database has one, set it "
                          "in the stack in Portainer or in the environment.", 2)
    else:
        what["POSTGRES_PASSWORD"] = "kept"
    return env, what


def scrub(text: str, env: dict[str, str]) -> str:
    for name in SECRETS:
        if env.get(name):
            text = text.replace(env[name], "***")
    return text


def write_stack(portainer: Portainer, env_id: int, name: str, stack: dict | None,
                compose: str, env: dict[str, str]) -> None:
    pairs = [{"name": k, "value": v} for k, v in sorted(env.items())]
    query = urllib.parse.urlencode({"endpointId": env_id})
    if stack is None:
        portainer.json("POST", f"/api/stacks/create/standalone/string?{query}", {
            "name": name, "stackFileContent": compose, "env": pairs, "fromAppTemplate": False})
    else:
        portainer.json("PUT", f"/api/stacks/{stack['Id']}?{query}", {
            "stackFileContent": compose, "env": pairs, "prune": False, "pullImage": False})


# ---------------- Waiting for the server ----------------

def project_containers(portainer: Portainer, env_id: int, project: str) -> dict[str, dict]:
    filters = json.dumps({"label": [f"com.docker.compose.project={project}"]})
    query = urllib.parse.urlencode({"all": "true", "filters": filters})
    found = docker(portainer, env_id, f"/containers/json?{query}") or []
    return {(c.get("Labels") or {}).get("com.docker.compose.service", ""): c for c in found}


def wait_until_up(portainer: Portainer, env_id: int, project: str, seconds: float,
                  env: dict[str, str]) -> None:
    deadline = time.monotonic() + seconds

    def fail(message: str, container: dict | None) -> Failure:
        tail = ""
        if container:
            tail = "\n--- log of " + (container.get("Names") or ["?"])[0].lstrip("/") + ":\n"
            tail += scrub(logs_of(portainer, env_id, container["Id"]), env).rstrip()
        return Failure(message + tail)

    while True:
        found = project_containers(portainer, env_id, project)
        init, app = found.get("init"), found.get("oloop")

        if init:
            state = docker(portainer, env_id, f"/containers/{init['Id']}/json")["State"]
            if state.get("Status") == "exited" and state.get("ExitCode") != 0:
                raise fail("the init step of the stack failed", init)

        if app:
            state = docker(portainer, env_id, f"/containers/{app['Id']}/json")["State"]
            status, health = state.get("Status"), (state.get("Health") or {}).get("Status")
            if status in ("exited", "dead"):
                raise fail(f"the server stopped (exit code {state.get('ExitCode')})", app)
            if health == "unhealthy":
                raise fail("the server is running but does not answer /healthz", app)
            if status == "running" and health in ("healthy", None):
                return

        if time.monotonic() >= deadline:
            raise fail(f"the server is not healthy after {seconds:.0f} seconds", app)
        time.sleep(POLL_SECONDS)


# ---------------- The script ----------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("PORTAINER_URL"))
    parser.add_argument("--env-id", default=os.environ.get("PORTAINER_ENV_ID"))
    parser.add_argument("--name", default="oloop-server", help="name of the stack")
    parser.add_argument("--compose", type=Path, default=ROOT / "docker-compose.yml")
    parser.add_argument("--image", help="default: the newest oloop-server:* of the environment")
    parser.add_argument("--network",
                        help="network of the reverse proxy, its subnet is trusted "
                             "(default: the only external network of the compose file)")
    parser.add_argument("--wait", type=float, default=180, help="seconds to wait for the server")
    parser.add_argument("--dry-run", action="store_true", help="change nothing, show the plan")
    args, unknown = parser.parse_known_args(argv)

    try:
        reject_unknown(unknown)
        if not re.match(r"^[a-z0-9][a-z0-9_-]*$", args.name):
            raise Failure("the name of a stack has lowercase letters, digits, - and _", 2)
        if not args.compose.is_file():
            raise Failure(f"{args.compose} does not exist. The compose file is not part of the "
                          "repo, it describes one installation: see README.md.", 2)
        compose = args.compose.read_text()
        if "OLOOP_IMAGE" not in compose:
            raise Failure(f"{args.compose.name} does not use ${{OLOOP_IMAGE}}: the image of the "
                          "stack could not be chosen. Use image: instead of build:.", 2)

        portainer = connect(args.url)
        env_id = pick_environment(portainer, args.env_id)

        image = args.image or newest_image(portainer, env_id)
        check_image(portainer, env_id, image)
        network = args.network
        if network is None:
            declared = external_networks(compose)
            if len(declared) > 1:
                raise Failure("the compose file has several external networks "
                              f"({', '.join(declared)}). Name the one of the reverse proxy "
                              "with --network.", 2)
            network = declared[0] if declared else None
        subnets = network_subnets(portainer, env_id, network) if network else []
        stack = find_stack(portainer, env_id, args.name)
        env, what = merge_environment(
            None if stack is None else stack_environment(stack), image, subnets, dict(os.environ))
        given = os.environ.get("POSTGRES_PASSWORD")
        if stack is not None and given and given != env["POSTGRES_PASSWORD"]:
            print("note: POSTGRES_PASSWORD of the environment is not used, the stack keeps its own",
                  file=sys.stderr)

        storage = env.get("OLOOP_STORAGE") or compose_default(compose, "OLOOP_STORAGE") or "(not set)"
        action = "create" if stack is None else f"update (Id {stack['Id']})"
        print(f"stack {args.name} on environment {env_id}: {action}", file=sys.stderr)
        print(f"  image             {image}", file=sys.stderr)
        print(f"  data on the host  {storage}  (blobs/ and pgdata/)", file=sys.stderr)
        source = f"network {network}" if network else "no external network in the compose file"
        print(f"  trusted proxies   {env.get('OLOOP_TRUSTED_PROXIES') or '(none)'}  ({source})",
              file=sys.stderr)
        if not env.get("OLOOP_TRUSTED_PROXIES"):
            print("note: no proxy is trusted, so the limit on failed logins would see the address "
                  "of the proxy. Name its network with --network or set OLOOP_TRUSTED_PROXIES.",
                  file=sys.stderr)
        print("  variables         " + ", ".join(f"{k} ({v})" for k, v in sorted(what.items())),
              file=sys.stderr)

        if args.dry_run:
            print("dry run: nothing was changed", file=sys.stderr)
            return 0

        write_stack(portainer, env_id, args.name, stack, compose, env)
        print("sent to Portainer, waiting for the server ...", file=sys.stderr)
        wait_until_up(portainer, env_id, args.name, args.wait, env)
        print(f"{args.name} is up", file=sys.stderr)
        return 0
    except Failure as error:
        print(f"portainer_deploy: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    raise SystemExit(main())
