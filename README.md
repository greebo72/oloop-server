# oloop-server

The server of the optional server backend of [oloop](https://github.com/greebo72/oloop-cli). It stores
**encrypted** projects, tasks and image blobs and never sees them in clear text: all encryption happens
in the clients. The folder backend of oloop does not need it.

**Status:** the login works (accounts, `POST /v1/auth/login`, `/refresh` and `/logout`, with a limit on
failed logins) and so does the key bundle (`GET` and `PUT /v1/keys`). Projects, documents, blobs and
agents are not there yet.

The contract is in the [spec](https://github.com/greebo72/oloop-spec), included here as the submodule
`spec/`: [`SERVER.md`](spec/SERVER.md) (encryption, API, blobs), [`AGENTS.md`](spec/AGENTS.md) (the CLI
used by an LLM) and the test vectors in `spec/fixtures/server/`.

## Stack

Python and FastAPI, PostgreSQL (SQLite for development and tests), blobs as files in a volume. Runs in
Docker, behind a reverse proxy that ends TLS. The proxy is not part of this repo.

## Run

The image is built from the `Dockerfile`. The compose file that runs it is **not part of this repo**:
it describes one installation (network, paths), so it stays local (`docker-compose.yml` is ignored).
What the server needs from it:

- The server is `oloop-server serve`, plain HTTP on port 8000. No port has to be published when a
  reverse proxy on the same Docker network reaches it.
- PostgreSQL, reached through `OLOOP_DATABASE_URL`
  (`postgresql+psycopg://user:password@host:5432/oloop`).
- A folder for the encrypted images, mounted at the path of `OLOOP_BLOB_DIR` (`/data/blobs` in the
  image). The image runs as user 10001, so the folder has to be writable for that user, or the
  container runs as another one (`user:`).
- Back up the database and the image folder together.
- `OLOOP_TRUSTED_PROXIES` names the proxy whose `X-Forwarded-*` headers are trusted.

`.env.example` lists the variables of a local compose setup.

## Accounts

There is no public registration. The operator creates accounts on the command line (the password has
at least 12 characters and is asked for twice, or read from the standard input):

```
oloop-server create-user alice
echo "$PASSWORD" | oloop-server create-user alice --password-stdin
```

`oloop-server serve` brings the database up to date before it starts (`--no-migrate` skips that);
`oloop-server migrate` does only that.

## Develop

```
git clone --recurse-submodules <this repo>
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
oloop-server serve
```

## Settings

Environment variables, prefix `OLOOP_`, or a `.env` file: `DATABASE_URL`, `BLOB_DIR`, `TRUSTED_PROXIES`,
`MAX_DOCUMENT_BYTES`, `MAX_BLOB_BYTES`, `ACCESS_TOKEN_MINUTES`, `REFRESH_TOKEN_DAYS`,
`LOGIN_WINDOW_MINUTES`, `LOGIN_MAX_FAILURES_PER_USER`, `LOGIN_MAX_FAILURES_PER_ADDRESS`. See
`src/oloop_server/config.py`.

`TRUSTED_PROXIES` takes addresses or networks (`172.18.0.0/16`), comma separated. The `X-Forwarded-For`
header is only believed when the request comes from one of them; the address of the client is what the
limit on failed logins counts.

## License

Apache License 2.0, see [LICENSE](LICENSE).
