import importlib.util
import json
import re
import struct
from pathlib import Path

import pytest

from fake_portainer import KEY, FakePortainer

SCRIPT = Path(__file__).parent.parent / "scripts" / "portainer_deploy.py"


def load_script():
    spec = importlib.util.spec_from_file_location("portainer_deploy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pd = load_script()

COMPOSE = """\
services:
  oloop:
    image: ${OLOOP_IMAGE:?set OLOOP_IMAGE}
    volumes:
      - ${OLOOP_STORAGE:-/srv/oloop}/blobs:/data/blobs
    networks:
      default:
      proxy-net:
        aliases:
          - oloop-server

networks:
  proxy-net:
    external: true
"""
ENV = 6


def frame(stream: int, text: str) -> bytes:
    body = text.encode()
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(body)) + body


class FakeDocker(FakePortainer):
    """Portainer with the Docker engine of one environment and its stacks."""

    def __init__(self):
        super().__init__()
        self.environments = [{"Id": ENV, "Name": "docker-host"}]
        self.images = [
            {"RepoTags": ["oloop-server:aaa1111"], "Created": 100},
            {"RepoTags": ["oloop-server:bbb2222"], "Created": 300},
            {"RepoTags": ["oloop-server:ccc3333", "other:1"], "Created": 200},
            {"RepoTags": ["postgres:16"], "Created": 999},
            {"RepoTags": None, "Created": 1000},
        ]
        self.networks = {"proxy-net": [{"Subnet": "10.9.0.0/16"}]}
        self.stacks = []
        self.written = []  # (method, path, body) of the creations and updates
        self.write_error = None  # (status, message)
        self.health = ["healthy"]  # what the server reports, one per look
        self.app_state = {"Status": "running", "ExitCode": 0}
        self.init_state = {"Status": "exited", "ExitCode": 0}
        self.log = "starting\n"
        self.inspected = 0

    def route(self, method, path, query, body):
        base = f"/api/endpoints/{ENV}/docker"

        if method == "GET" and path == f"{base}/images/json":
            return 200, self.images
        match = re.fullmatch(re.escape(base) + r"/images/(.+)/json", path)
        if method == "GET" and match:
            known = {t for i in self.images for t in i["RepoTags"] or []}
            return (200, {}) if match.group(1) in known else (404, {"message": "no such image"})
        match = re.fullmatch(re.escape(base) + r"/networks/(.+)", path)
        if method == "GET" and match:
            name = match.group(1)
            if name not in self.networks:
                return 404, {"message": "network not found"}
            return 200, {"Name": name, "IPAM": {"Config": [
                {"Subnet": s["Subnet"]} for s in self.networks[name]]}}

        if method == "GET" and path == "/api/stacks":
            return 200, self.stacks
        if method == "POST" and path == "/api/stacks/create/standalone/string":
            if self.write_error:
                return self.write_error[0], {"message": self.write_error[1]}
            payload = json.loads(body)
            self.written.append(("POST", path, query, payload))
            stack = {"Id": 11, "Name": payload["name"], "EndpointId": int(query["endpointId"][0]),
                     "Env": payload["env"]}
            self.stacks.append(stack)
            return 200, stack
        match = re.fullmatch(r"/api/stacks/(\d+)", path)
        if method == "PUT" and match:
            if self.write_error:
                return self.write_error[0], {"message": self.write_error[1]}
            payload = json.loads(body)
            self.written.append(("PUT", path, query, payload))
            return 200, {}

        if method == "GET" and path == f"{base}/containers/json":
            label = json.loads(query["filters"][0])["label"][0]
            project = label.split("=", 1)[1]
            return 200, [
                {"Id": f"{service}-id", "Names": [f"/{project}-{service}-1"],
                 "Labels": {"com.docker.compose.project": project,
                            "com.docker.compose.service": service}}
                for service in ("init", "oloop", "db")]
        match = re.fullmatch(re.escape(base) + r"/containers/(init|oloop|db)-id/json", path)
        if method == "GET" and match:
            if match.group(1) == "init":
                return 200, {"State": self.init_state}
            self.inspected += 1
            state = dict(self.app_state)
            health = self.health[min(self.inspected - 1, len(self.health) - 1)]
            if health:
                state["Health"] = {"Status": health}
            return 200, {"State": state}
        if method == "GET" and re.fullmatch(re.escape(base) + r"/containers/[a-z]+-id/logs", path):
            return 200, frame(1, self.log)
        return None


@pytest.fixture
def docker():
    with FakeDocker() as fake:
        yield fake


@pytest.fixture
def compose(tmp_path):
    path = tmp_path / "docker-compose.yml"
    path.write_text(COMPOSE)
    return path


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("PORTAINER_TOKEN", KEY)
    for name in ("PORTAINER_URL", "PORTAINER_ENV_ID", "PORTAINER_CACERT", "POSTGRES_PASSWORD",
                 "OLOOP_STORAGE", "OLOOP_UID", "OLOOP_GID", "OLOOP_TRUSTED_PROXIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(pd, "POLL_SECONDS", 0.01)


def run(capsys, docker, compose, *args):
    code = pd.main(["--url", docker.url, "--compose", str(compose), *args])
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err, "the token must never be printed"
    for _method, _path, _query, payload in docker.written:
        password = {p["name"]: p["value"] for p in payload["env"]}.get("POSTGRES_PASSWORD")
        assert password not in (None, "")
        assert password not in captured.out + captured.err, "the password must never be printed"
    return code, captured.err


def sent_env(docker):
    return {p["name"]: p["value"] for p in docker.written[-1][3]["env"]}


# ---------------- Creating the stack ----------------

def test_a_new_stack_is_created_with_the_newest_image_and_the_proxy_subnet(docker, compose, capsys):
    code, err = run(capsys, docker, compose)

    assert code == 0
    ((method, path, query, payload),) = docker.written
    assert (method, path) == ("POST", "/api/stacks/create/standalone/string")
    assert query == {"endpointId": [str(ENV)]}
    assert payload["name"] == "oloop-server"
    assert payload["stackFileContent"] == COMPOSE
    env = sent_env(docker)
    assert env["OLOOP_IMAGE"] == "oloop-server:bbb2222", "the newest one, others are no oloop-server"
    assert env["OLOOP_TRUSTED_PROXIES"] == "10.9.0.0/16"
    assert "oloop-server is up" in err


def test_the_password_is_made_at_random_and_kept_in_the_stack(docker, compose, capsys):
    run(capsys, docker, compose)

    password = sent_env(docker)["POSTGRES_PASSWORD"]
    assert len(password) >= 40
    assert docker.written[0][3]["stackFileContent"].count(password) == 0


def test_two_new_stacks_get_different_passwords(compose, capsys):
    seen = set()
    for _ in range(2):
        with FakeDocker() as docker:
            run(capsys, docker, compose)
            seen.add(sent_env(docker)["POSTGRES_PASSWORD"])

    assert len(seen) == 2


def test_a_password_from_the_environment_is_used(docker, compose, capsys, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "chosen-by-the-user-1234")

    code, err = pd.main(["--url", docker.url, "--compose", str(compose)]), capsys.readouterr().err

    assert code == 0 and sent_env(docker)["POSTGRES_PASSWORD"] == "chosen-by-the-user-1234"
    assert "chosen-by-the-user-1234" not in err


def test_settings_of_the_environment_are_passed_on(docker, compose, capsys, monkeypatch):
    monkeypatch.setenv("OLOOP_STORAGE", "/srv/elsewhere")
    monkeypatch.setenv("OLOOP_UID", "1000")
    monkeypatch.setenv("OLOOP_TRUSTED_PROXIES", "10.1.2.3")

    code, err = run(capsys, docker, compose)

    env = sent_env(docker)
    assert code == 0
    assert env["OLOOP_STORAGE"] == "/srv/elsewhere" and env["OLOOP_UID"] == "1000"
    assert env["OLOOP_TRUSTED_PROXIES"] == "10.1.2.3", "a choice wins over the subnet"
    assert "/srv/elsewhere" in err


def test_an_image_can_be_named(docker, compose, capsys):
    code, _ = run(capsys, docker, compose, "--image", "oloop-server:aaa1111")

    assert code == 0 and sent_env(docker)["OLOOP_IMAGE"] == "oloop-server:aaa1111"


def test_the_data_folder_of_the_compose_file_is_shown(docker, compose, capsys):
    _, err = run(capsys, docker, compose, "--dry-run")

    assert "/srv/oloop" in err and "blobs/ and pgdata/" in err


# ---------------- Updating it ----------------

def existing_stack(docker, **env):
    pairs = {"OLOOP_IMAGE": "oloop-server:aaa1111", "POSTGRES_PASSWORD": "the-old-password-xyz",
             "OLOOP_TRUSTED_PROXIES": "10.9.0.0/16", **env}
    docker.stacks.append({"Id": 7, "Name": "oloop-server", "EndpointId": ENV,
                          "Env": [{"name": k, "value": v} for k, v in pairs.items()]})


def test_an_existing_stack_is_updated_and_keeps_its_password(docker, compose, capsys):
    existing_stack(docker)

    code, err = run(capsys, docker, compose)

    assert code == 0
    ((method, path, query, payload),) = docker.written
    assert (method, path) == ("PUT", "/api/stacks/7") and query == {"endpointId": [str(ENV)]}
    assert payload["prune"] is False and payload["pullImage"] is False
    assert sent_env(docker)["POSTGRES_PASSWORD"] == "the-old-password-xyz"
    assert sent_env(docker)["OLOOP_IMAGE"] == "oloop-server:bbb2222"
    assert "update (Id 7)" in err and "OLOOP_IMAGE (changed)" in err
    assert "POSTGRES_PASSWORD (kept)" in err


def test_a_variable_someone_added_in_portainer_is_kept(docker, compose, capsys):
    existing_stack(docker, MY_OWN="value", OLOOP_TRUSTED_PROXIES="192.0.2.1")

    run(capsys, docker, compose)

    env = sent_env(docker)
    assert env["MY_OWN"] == "value"
    assert env["OLOOP_TRUSTED_PROXIES"] == "192.0.2.1", "not replaced by the subnet"


def test_another_password_in_the_environment_does_not_replace_the_one_of_the_stack(
        docker, compose, capsys, monkeypatch):
    existing_stack(docker)
    monkeypatch.setenv("POSTGRES_PASSWORD", "something-else-entirely")

    code, err = pd.main(["--url", docker.url, "--compose", str(compose)]), capsys.readouterr().err

    assert code == 0 and sent_env(docker)["POSTGRES_PASSWORD"] == "the-old-password-xyz"
    assert "not used" in err and "something-else-entirely" not in err


def test_a_stack_without_a_password_is_refused_unless_one_is_given(docker, compose, capsys):
    docker.stacks.append({"Id": 7, "Name": "oloop-server", "EndpointId": ENV,
                          "Env": [{"name": "OLOOP_IMAGE", "value": "x"}]})

    code, err = run(capsys, docker, compose)

    assert code == 2 and "no POSTGRES_PASSWORD" in err and not docker.written


def test_a_stack_of_the_same_name_on_another_environment_is_not_touched(docker, compose, capsys):
    docker.stacks.append({"Id": 3, "Name": "oloop-server", "EndpointId": 99, "Env": []})

    code, err = run(capsys, docker, compose)

    assert code == 0 and docker.written[0][0] == "POST" and "create" in err


def test_the_env_list_in_the_other_spelling_is_read_too(docker, compose, capsys):
    docker.stacks.append({"Id": 7, "Name": "oloop-server", "EndpointId": ENV,
                          "env": [{"name": "POSTGRES_PASSWORD", "value": "lowercase-key-pw"}]})

    code, _ = run(capsys, docker, compose)

    assert code == 0 and sent_env(docker)["POSTGRES_PASSWORD"] == "lowercase-key-pw"


# ---------------- Dry run ----------------

def test_a_dry_run_changes_nothing_and_shows_the_plan(docker, compose, capsys):
    code, err = run(capsys, docker, compose, "--dry-run")

    assert code == 0 and not docker.written
    assert "create" in err and "oloop-server:bbb2222" in err and "10.9.0.0/16" in err
    assert "POSTGRES_PASSWORD (made at random)" in err
    assert "dry run: nothing was changed" in err
    assert docker.inspected == 0, "it does not wait for a server"


def test_a_dry_run_of_an_update(docker, compose, capsys):
    existing_stack(docker)

    code, err = run(capsys, docker, compose, "--dry-run")

    assert code == 0 and not docker.written and "update (Id 7)" in err


# ---------------- What is checked first ----------------

def test_an_image_that_is_not_there_stops_before_anything_is_written(docker, compose, capsys):
    code, err = run(capsys, docker, compose, "--image", "oloop-server:nothere")

    assert code == 1 and "oloop-server:nothere is not on this environment" in err
    assert not docker.written


def test_without_an_oloop_image_it_says_to_build_one(docker, compose, capsys):
    docker.images = [{"RepoTags": ["postgres:16"], "Created": 1}]

    code, err = run(capsys, docker, compose)

    assert code == 1 and "portainer_build.py" in err and not docker.written


def test_a_network_that_is_not_there_stops_before_anything_is_written(docker, compose, capsys):
    docker.networks = {}

    code, err = run(capsys, docker, compose)

    assert code == 1 and "network proxy-net is not on this environment" in err
    assert not docker.written


def test_another_network_can_be_named(docker, compose, capsys):
    docker.networks["other-net"] = [{"Subnet": "10.8.0.0/24"}, {"Subnet": "fd00::/64"}]

    code, _ = run(capsys, docker, compose, "--network", "other-net")

    assert code == 0 and sent_env(docker)["OLOOP_TRUSTED_PROXIES"] == "10.8.0.0/24,fd00::/64"


def test_the_network_comes_from_the_compose_file(docker, compose, capsys):
    code, err = run(capsys, docker, compose, "--dry-run")

    assert code == 0 and "(network proxy-net)" in err
    asked = [r["path"] for r in docker.requests if "/networks/" in r["path"]]
    assert asked == [f"/api/endpoints/{ENV}/docker/networks/proxy-net"]


def test_a_compose_file_without_an_external_network_trusts_no_proxy(docker, tmp_path, capsys):
    path = tmp_path / "docker-compose.yml"
    path.write_text("services:\n  oloop:\n    image: ${OLOOP_IMAGE}\n")

    code, err = run(capsys, docker, path)

    assert code == 0
    assert "OLOOP_TRUSTED_PROXIES" not in sent_env(docker)
    assert "no external network in the compose file" in err and "no proxy is trusted" in err
    assert not [r for r in docker.requests if "/networks/" in r["path"]]


def test_several_external_networks_must_be_chosen(docker, tmp_path, capsys):
    path = tmp_path / "docker-compose.yml"
    path.write_text(COMPOSE + "  other-net:\n    external: true\n")

    code, err = run(capsys, docker, path)

    assert code == 2 and "proxy-net, other-net" in err and "--network" in err
    assert not docker.written

    docker.networks["other-net"] = [{"Subnet": "10.8.0.0/24"}]
    code, _ = run(capsys, docker, path, "--network", "other-net")
    assert code == 0 and sent_env(docker)["OLOOP_TRUSTED_PROXIES"] == "10.8.0.0/24"


@pytest.mark.parametrize("compose,expected", [
    ("networks:\n  proxy-net:\n    external: true\n", ["proxy-net"]),
    ("networks:\n  internal:\n    driver: bridge\n  proxy-net:\n    external: true\n", ["proxy-net"]),
    ("networks:\n  default:\n  proxy-net:\n    external: true\n", ["proxy-net"]),
    ("networks:\n  a:\n    external: true\n  b:\n    external: true\n", ["a", "b"]),
    ("networks:\n  proxy-net:\n    external: true\n    name: real-net\n", ["real-net"]),
    ("networks:\n  proxy-net:\n    external:\n      name: real-net\n", ["real-net"]),
    ("networks:\n  proxy-net: { external: true }\n", ["proxy-net"]),
    ("networks:\n  proxy-net: { external: true, name: real-net }\n", ["real-net"]),
    ("networks:\n  proxy-net:\n    external: false\n", []),
    ('networks:\n  "proxy-net":  # the proxy\n    external: true # yes\n', ["proxy-net"]),
    ("networks:\r\n  proxy-net:\r\n    external: true\r\n", ["proxy-net"]),
    ("networks:\n    proxy-net:\n        external: true\n", ["proxy-net"]),
    ("networks:\n  a:\n    external: true\nvolumes:\n  v:\n    external: true\n", ["a"]),
    ("services:\n  s:\n    networks:\n      x:\n        external: true\n", []),
    ("services:\n  s:\n    image: i\nnetworks:\n  n:\n    external: true\n", ["n"]),
    ("networks:\n", []),
    ("", []),
])
def test_the_external_networks_of_a_compose_file(compose, expected):
    assert pd.external_networks(compose) == expected


# ---------------- Waiting for the server ----------------

def test_it_waits_until_the_server_is_healthy(docker, compose, capsys):
    docker.health = ["starting", "starting", "healthy"]

    code, err = run(capsys, docker, compose)

    assert code == 0 and docker.inspected == 3 and "oloop-server is up" in err


def test_a_server_without_a_healthcheck_counts_as_up_when_it_runs(docker, compose, capsys):
    docker.health = [None]

    code, _ = run(capsys, docker, compose)

    assert code == 0


def test_a_server_that_never_gets_healthy_is_a_failure_with_its_log(docker, compose, capsys):
    docker.health = ["starting"]
    docker.log = "Traceback: could not connect to the database\n"

    code, err = run(capsys, docker, compose, "--wait", "0.2")

    assert code == 1
    assert "not healthy after" in err
    assert "log of oloop-server-oloop-1" in err and "could not connect to the database" in err


def test_an_unhealthy_server_is_a_failure_at_once(docker, compose, capsys):
    docker.health = ["unhealthy"]

    code, err = run(capsys, docker, compose, "--wait", "60")

    assert code == 1 and "does not answer /healthz" in err


def test_a_server_that_stopped_is_a_failure(docker, compose, capsys):
    docker.app_state = {"Status": "exited", "ExitCode": 3}
    docker.log = "alembic: cannot upgrade\n"

    code, err = run(capsys, docker, compose, "--wait", "60")

    assert code == 1 and "stopped (exit code 3)" in err and "cannot upgrade" in err


def test_a_failed_init_step_is_reported_with_its_log(docker, compose, capsys):
    docker.init_state = {"Status": "exited", "ExitCode": 1}
    docker.log = "chown: Operation not permitted\n"

    code, err = run(capsys, docker, compose, "--wait", "60")

    assert code == 1 and "init step of the stack failed" in err
    assert "Operation not permitted" in err


def test_the_password_is_hidden_in_a_log(docker, compose, capsys, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret-in-the-log-9876")
    docker.health = ["unhealthy"]
    docker.log = "connecting with postgresql://oloop:secret-in-the-log-9876@db/oloop\n"

    code = pd.main(["--url", docker.url, "--compose", str(compose), "--wait", "5"])
    err = capsys.readouterr().err

    assert code == 1
    assert "secret-in-the-log-9876" not in err and "oloop:***@db" in err


# ---------------- Errors of Portainer ----------------

def test_a_refusal_of_portainer_is_shown(docker, compose, capsys):
    docker.write_error = (400, "stack name is already in use")

    code = pd.main(["--url", docker.url, "--compose", str(compose)])
    err = capsys.readouterr().err

    assert code == 1 and "Portainer answered 400: stack name is already in use" in err


# ---------------- Wrong use ----------------

def test_without_a_compose_file_it_says_where_it_is_described(docker, tmp_path, capsys):
    code = pd.main(["--url", docker.url, "--compose", str(tmp_path / "none.yml")])
    err = capsys.readouterr().err

    assert code == 2 and "not part of the repo" in err and not docker.requests


def test_a_compose_file_that_builds_instead_of_using_an_image_is_refused(docker, tmp_path, capsys):
    path = tmp_path / "docker-compose.yml"
    path.write_text("services:\n  oloop:\n    build: .\n")

    code = pd.main(["--url", docker.url, "--compose", str(path)])

    assert code == 2 and "OLOOP_IMAGE" in capsys.readouterr().err and not docker.requests


@pytest.mark.parametrize("name", ["Oloop", "oloop server", "-x", "a/b", ""])
def test_a_name_of_a_stack_that_portainer_would_refuse_is_refused_here(docker, compose, capsys, name):
    code = pd.main(["--url", docker.url, "--compose", str(compose), f"--name={name}"])

    assert code == 2 and not docker.requests


def test_without_a_token_nothing_is_sent(docker, compose, capsys, monkeypatch):
    monkeypatch.delenv("PORTAINER_TOKEN")

    code = pd.main(["--url", docker.url, "--compose", str(compose)])

    assert code == 2 and not docker.requests
    assert "PORTAINER_TOKEN is not set" in capsys.readouterr().err


def test_an_address_that_would_expose_the_token_is_refused(compose, capsys):
    code = pd.main(["--url", "http://portainer.example.org", "--compose", str(compose)])

    assert code == 2 and "https" in capsys.readouterr().err


def test_the_token_is_not_taken_as_an_argument_and_never_echoed(docker, compose, capsys):
    code = pd.main(["--url", docker.url, "--compose", str(compose), f"--token={KEY}"])
    err = capsys.readouterr().err

    assert code == 2 and KEY not in err and "unknown option --token" in err
    assert not docker.requests


def test_several_environments_must_be_chosen(docker, compose, capsys):
    docker.environments = [{"Id": 3, "Name": "local"}, {"Id": ENV, "Name": "docker-host"}]

    code, err = run(capsys, docker, compose)

    assert code == 2 and f"{ENV}  docker-host" in err and not docker.written


# ---------------- The log of a container ----------------

def test_a_log_in_frames_is_read_as_text():
    data = frame(1, "out line\n") + frame(2, "error line\n")

    assert pd.demux(data) == "out line\nerror line\n"


def test_a_log_without_frames_is_text_as_it_is():
    assert pd.demux(b"plain text\n") == "plain text\n"


def test_a_log_that_stops_in_a_frame_does_not_break():
    data = frame(1, "complete\n") + frame(1, "cut off")[:-4]

    assert pd.demux(data).startswith("complete\n")
