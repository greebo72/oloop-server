# oloop-server

The server of the optional server backend of [oloop](https://github.com/greebo72/oloop-cli). It stores
**encrypted** projects, tasks and image blobs and never sees them in clear text: all encryption happens
in the clients. The folder backend of oloop does not need it.

**Status:** skeleton. It answers `/healthz` and nothing else yet.

The contract is in the [spec](https://github.com/greebo72/oloop-spec), included here as the submodule
`spec/`: [`SERVER.md`](spec/SERVER.md) (encryption, API, blobs), [`AGENTS.md`](spec/AGENTS.md) (the CLI
used by an LLM) and the test vectors in `spec/fixtures/server/`.

## Stack

Python and FastAPI, PostgreSQL (SQLite for development and tests), blobs as files in a volume. Runs in
Docker, behind a reverse proxy that ends TLS. The proxy is not part of this repo.

## Run

```
cp .env.example .env      # set POSTGRES_PASSWORD
docker compose up --build
```

The port is bound to `127.0.0.1:8000`; point the reverse proxy there.

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
`MAX_DOCUMENT_BYTES`, `MAX_BLOB_BYTES`. See `src/oloop_server/config.py`.

## License

Apache License 2.0, see [LICENSE](LICENSE).
