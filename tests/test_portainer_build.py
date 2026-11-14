import importlib.util
import io
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "portainer_build.py"
KEY = "ptr_secret-token-1234567890"


def load_script():
    spec = importlib.util.spec_from_file_location("portainer_build", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pb = load_script()


class FakePortainer:
    """Answers like the parts of Portainer the script uses, and keeps what it got."""

    def __init__(self):
        self.environments = [{"Id": 3, "Name": "docker-host"}]
        self.build_stream = [
            {"stream": "Step 1/3 : FROM python:3.12-slim\n"},
            {"stream": "Successfully built abc123\n"},
        ]
        self.image_status = 200
        self.fail_with = None  # (status, body) for every request
        self.requests = []

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, body=b"", content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urlparse(self.path)
                owner.requests.append({
                    "method": self.command, "path": url.path,
                    "query": parse_qs(url.query),
                    "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body,
                })

                if owner.fail_with:
                    status, text = owner.fail_with
                    return self._answer(status, json.dumps({"message": text}).encode())
                if self.headers.get("X-API-Key") != KEY:
                    return self._answer(401, b'{"message": "Invalid API key"}')

                if url.path == "/api/endpoints":
                    return self._answer(200, json.dumps(owner.environments).encode())
                if url.path.endswith("/docker/build") and self.command == "POST":
                    lines = "".join(json.dumps(m) + "\r\n" for m in owner.build_stream)
                    return self._answer(200, lines.encode())
                if "/docker/images/" in url.path and url.path.endswith("/json"):
                    return self._answer(owner.image_status, b"{}")
                return self._answer(404, b'{"message": "not found"}')

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, method, suffix):
        return [r for r in self.requests if r["method"] == method and r["path"].endswith(suffix)]


@pytest.fixture
def portainer():
    with FakePortainer() as fake:
        yield fake


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "oloop-server-abc1234.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, content in (("Dockerfile", b"FROM scratch\n"), ("pyproject.toml", b"[project]\n"),
                              ("src/oloop_server/main.py", b"x = 1\n")):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return path


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("PORTAINER_TOKEN", KEY)
    for name in ("PORTAINER_URL", "PORTAINER_ENV_ID", "PORTAINER_CACERT"):
        monkeypatch.delenv(name, raising=False)


def run(capsys, *args):
    code = pb.main(list(args))
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err, "the key must never be printed"
    return code, captured.out, captured.err


# ---------------- A build that works ----------------

def test_the_build_is_sent_with_the_key_the_tag_and_the_context(portainer, archive, capsys):
    code, out, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 0
    (build,) = portainer.calls("POST", "/docker/build")
    assert build["path"] == "/api/endpoints/3/docker/build"
    assert build["headers"]["x-api-key"] == KEY
    assert build["headers"]["content-type"] == "application/x-tar"
    assert build["query"]["t"] == ["oloop-server:abc1234"]
    assert "pull" not in build["query"]
    with tarfile.open(fileobj=io.BytesIO(build["body"]), mode="r:gz") as tar:
        assert "Dockerfile" in tar.getnames()
        assert "src/oloop_server/main.py" in tar.getnames()
    assert "Step 1/3 : FROM python:3.12-slim" in out
    assert "built oloop-server:abc1234" in err


def test_the_image_is_asked_for_after_the_build(portainer, archive, capsys):
    run(capsys, "--url", portainer.url, "--archive", str(archive))

    (check,) = portainer.calls("GET", "/json")
    assert check["path"] == "/api/endpoints/3/docker/images/oloop-server:abc1234/json"


def test_the_name_and_the_pull_can_be_chosen(portainer, archive, capsys):
    code, _, _ = run(capsys, "--url", portainer.url, "--archive", str(archive),
                     "--tag", "registry.example/oloop/server:1.2", "--pull")

    assert code == 0
    (build,) = portainer.calls("POST", "/docker/build")
    assert build["query"]["t"] == ["registry.example/oloop/server:1.2"]
    assert build["query"]["pull"] == ["true"]
    (check,) = portainer.calls("GET", "/json")
    assert check["path"].endswith("/images/registry.example/oloop/server:1.2/json")


def test_the_url_and_environment_can_come_from_the_environment(portainer, archive, capsys, monkeypatch):
    monkeypatch.setenv("PORTAINER_URL", portainer.url + "/")
    monkeypatch.setenv("PORTAINER_ENV_ID", "7")

    code, _, _ = run(capsys, "--archive", str(archive))

    assert code == 0
    assert portainer.calls("POST", "/docker/build")[0]["path"] == "/api/endpoints/7/docker/build"
    assert not portainer.calls("GET", "/api/endpoints"), "a given id needs no list"


def test_the_only_environment_is_taken(portainer, archive, capsys):
    run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert len(portainer.calls("GET", "/api/endpoints")) == 1


def test_a_name_without_a_revision_gets_latest(portainer, tmp_path, archive, capsys):
    other = archive.rename(tmp_path / "context.tar.gz")

    run(capsys, "--url", portainer.url, "--archive", str(other))

    assert portainer.calls("POST", "/docker/build")[0]["query"]["t"] == ["oloop-server:latest"]


def test_without_an_archive_the_context_is_packed_from_the_repo(portainer, capsys):
    code, _, err = run(capsys, "--url", portainer.url)

    assert code == 0
    (build,) = portainer.calls("POST", "/docker/build")
    with tarfile.open(fileobj=io.BytesIO(build["body"]), mode="r:gz") as tar:
        names = tar.getnames()
    assert names[0] == "Dockerfile" or "Dockerfile" in names
    assert "pyproject.toml" in names and "src/oloop_server/main.py" in names
    assert not [n for n in names if "__pycache__" in n or n.startswith("tests")]
    assert "built oloop-server:" in err


# ---------------- Environments ----------------

def test_several_environments_are_listed_and_nothing_is_built(portainer, archive, capsys):
    portainer.environments = [{"Id": 1, "Name": "local"}, {"Id": 2, "Name": "nas"}]

    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 2
    assert "1  local" in err and "2  nas" in err
    assert not portainer.calls("POST", "/docker/build")


def test_no_environment_at_all(portainer, archive, capsys):
    portainer.environments = []

    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 2 and "(none)" in err


def test_an_environment_id_must_be_a_number(portainer, archive, capsys):
    code, _, _ = run(capsys, "--url", portainer.url, "--archive", str(archive), "--env-id", "../x")

    assert code == 2 and not portainer.requests


# ---------------- What goes wrong ----------------

def test_a_failed_build_is_reported_though_docker_answers_200(portainer, archive, capsys):
    portainer.build_stream = [
        {"stream": "Step 2/3 : RUN pip install .\n"},
        {"errorDetail": {"message": "pip failed"}, "error": "pip failed"},
    ]

    code, out, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 1
    assert "the build failed: pip failed" in err
    assert "Step 2/3" in out
    assert not portainer.calls("GET", "/json"), "no check of an image of a failed build"


def test_an_error_in_the_stream_without_detail_is_reported_too(portainer, archive, capsys):
    portainer.build_stream = [{"error": "no space left on device"}]

    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 1 and "no space left on device" in err


def test_a_build_that_ended_without_an_image_is_a_failure(portainer, archive, capsys):
    portainer.image_status = 404

    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 1 and "is not there" in err


def test_lines_that_are_no_json_are_skipped(portainer, archive, capsys):
    portainer.build_stream = [{"stream": "ok\n"}]
    original = portainer.server.RequestHandlerClass

    code, out, _ = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 0 and "ok" in out
    assert original is portainer.server.RequestHandlerClass


@pytest.mark.parametrize("status,text", [
    (401, "the API key is not accepted"),
    (403, "the user of the API key may not do this"),
    (404, "not found, is the environment id right?"),
    (500, "Portainer answered 500"),
])
def test_an_answer_of_portainer_is_explained(portainer, archive, capsys, status, text):
    portainer.fail_with = (status, "details of portainer")

    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(archive), "--env-id", "3")

    assert code == 1
    assert text in err and "details of portainer" in err


def test_a_wrong_key_is_refused_and_not_shown(portainer, archive, capsys, monkeypatch):
    monkeypatch.setenv("PORTAINER_TOKEN", "ptr_wrong-key-0000")

    code, out, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 1 and "not accepted" in err
    assert "wrong-key" not in out + err


def test_a_server_that_cannot_be_reached(archive, capsys):
    code, _, err = run(capsys, "--url", "http://127.0.0.1:1", "--archive", str(archive))

    assert code == 1 and "cannot be reached" in err


# ---------------- Wrong use ----------------

def test_without_a_key_nothing_is_sent(portainer, archive, capsys, monkeypatch):
    monkeypatch.delenv("PORTAINER_TOKEN")

    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 2 and "PORTAINER_TOKEN is not set" in err
    assert not portainer.requests


def test_an_empty_key_is_no_key(portainer, archive, capsys, monkeypatch):
    monkeypatch.setenv("PORTAINER_TOKEN", "   ")

    code, _, _ = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 2 and not portainer.requests


def test_without_an_address(archive, capsys):
    code, _, err = run(capsys, "--archive", str(archive))

    assert code == 2 and "PORTAINER_URL" in err


@pytest.mark.parametrize("args", [["--api-key", KEY], [f"--api-key={KEY}"], ["--key", KEY]])
def test_the_key_is_not_taken_as_an_argument_and_never_echoed(capsys, args):
    code, _out, err = run(capsys, *args)

    assert code == 2
    assert "PORTAINER_TOKEN" in err and "unknown option --" in err


@pytest.mark.parametrize("url", [
    "http://portainer.example.org",
    "http://203.0.113.10:9000",
    "ftp://portainer.example.org",
    "portainer.example.org",
    "https://user:secret@portainer.example.org",
    "https://portainer.example.org/?x=1",
    "",
])
def test_an_address_that_would_expose_the_key_or_is_none_is_refused(archive, capsys, url):
    code, _, err = run(capsys, "--url", url, "--archive", str(archive))

    assert code == 2
    assert "secret" not in err


def test_https_and_loopback_http_are_accepted():
    assert pb.check_url("https://portainer.example.org:9443/") == "https://portainer.example.org:9443"
    assert pb.check_url("http://localhost:9000") == "http://localhost:9000"
    assert pb.check_url("http://127.0.0.1:9000") == "http://127.0.0.1:9000"


def test_a_missing_archive_is_refused(portainer, tmp_path, capsys):
    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(tmp_path / "none.tar.gz"))

    assert code == 2 and "does not exist" in err


def test_a_certificate_file_that_does_not_exist_is_refused(portainer, archive, capsys, monkeypatch):
    monkeypatch.setenv("PORTAINER_CACERT", "/nonexistent/ca.pem")

    code, _, err = run(capsys, "--url", portainer.url, "--archive", str(archive))

    assert code == 2 and "PORTAINER_CACERT" in err
    assert not portainer.requests
