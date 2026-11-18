# oloop-server

The server of the optional server backend of [oloop](https://github.com/greebo72/oloop-cli). It stores
**encrypted** projects, tasks and image blobs and never sees them in clear text: all encryption happens
in the clients. The folder backend of oloop does not need it.

**Status:** the login works (accounts, `POST /v1/auth/login`, `/refresh` and `/logout`, with a limit on
failed logins), the key bundle (`GET` and `PUT /v1/keys`), projects and documents (`PUT` and `GET
/v1/documents`, the feed of changes, `/v1/head`, the history of a document, `GET /v1/projects`) and the
image blobs (`PUT`, `GET` and `HEAD /v1/blobs/{id}`). Further members of a project (the agents) and the
quota are not there yet.

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

### Build the image without Docker on your computer

`scripts/build-context.sh` packs what the Dockerfile needs into `dist/oloop-server-<revision>.tar.gz`.
In Portainer, choose *Images*, *Build a new image*, upload that archive and give the image a name such
as `oloop-server:<revision>`. Portainer builds it on the Docker host of the environment you picked, so
the image has the architecture of that host. The stack then says `image: oloop-server:<revision>`
instead of `build: .`.

To update: run the script again, build the image under the new name, change the name in the stack and
redeploy. The database is brought up to date when the server starts; the data folders stay.

`scripts/portainer_build.py` does the same through the API of Portainer, so no upload by hand is needed:

```
export PORTAINER_URL=https://portainer.example.org
export PORTAINER_TOKEN=...            # My account, Access tokens in Portainer
scripts/portainer_build.py --env-id 3
```

It packs the context, has the environment build it, prints the build, and checks that the image exists
afterwards. The token is only read from the environment, never from an argument, and never printed. The
address has to be https (http only for localhost); `PORTAINER_CACERT` names a certificate authority for
a certificate that Portainer signed itself. Without `--env-id` the only environment is taken, with
several they are listed and nothing is built. Mind that the token has the rights of the user that made
it: a user of its own with access to one environment is the safer choice.

### Deploy the stack by script

`scripts/portainer_deploy.py` creates the stack in Portainer, or updates it, from your local
`docker-compose.yml` (not part of the repo, see above). The compose file takes its image from
`${OLOOP_IMAGE}`; the script uses the newest `oloop-server:*` image of the environment, or the one you
name with `--image`.

```
scripts/portainer_deploy.py --env-id 3 --dry-run     # look first, nothing is changed
scripts/portainer_deploy.py --env-id 3
```

It checks that the image and the network of the reverse proxy exist on the environment before it
writes anything, passes the subnet of that network as `OLOOP_TRUSTED_PROXIES` (unless you set it; the
network is the external network of the compose file, `--network` names it when there are several), and
waits until the server answers its health check. If the server does not come up, the log of the
container is shown with the database password hidden.

`POSTGRES_PASSWORD` is made at random when the stack is created (or taken from the environment) and
lives in the stack in Portainer; it is never printed. A later run keeps it, since the database keeps
its own. Variables that you added to the stack in Portainer are kept as well.

The data lives in folders on the host: `OLOOP_STORAGE` of the compose file says where, with `blobs/`
and `pgdata/` below it. A one-time `init` service makes
`blobs/` writable for the server, which does not run as root.

The first account is made in the running container (Portainer: *Containers*, the server, *Console*):

```
oloop-server create-user alice
```

## Accounts

There is no public registration. The operator creates accounts on the command line (the password has
at least 12 characters and is asked for twice, or read from the standard input):

```
oloop-server create-user alice
echo "$PASSWORD" | oloop-server create-user alice --password-stdin
```

`oloop-server serve` brings the database up to date before it starts (`--no-migrate` skips that);
`oloop-server migrate` does only that.

### Agents

An agent is made by a user in their own client, because that needs the user's secret key
(`spec/AGENTS.md`). The server holds its public key, its tokens (as hashes) and the keys of the
projects it was given, which it cannot open. A token is valid for 90 days unless the user asks
otherwise, can be renewed by the agent until the end of its renewal period (one year), and a user can
end it at once. Tokens that can never be used again are removed every
`OLOOP_TOKEN_PURGE_INTERVAL_MINUTES` (60; 0 switches it off); `oloop-server purge-tokens` does it once.

## Blobs

The encrypted images are files below `OLOOP_BLOB_DIR`, in folders named after the first characters of
the id. The database only knows which blob belongs to which task. The server removes what nobody needs:
a blob no task ever referred to after 24 hours, a blob no task refers to any more after 30 days, and
the temporary files of aborted uploads. It does that every `OLOOP_BLOB_GC_INTERVAL_MINUTES` (60; 0
switches it off); `oloop-server gc-blobs` does it once. Back up the folder together with the database.

A blob is uploaded for a task that exists, so an image is added after the task is written.

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
`BLOB_GC_INTERVAL_MINUTES`, `TOKEN_PURGE_INTERVAL_MINUTES`, `LOGIN_WINDOW_MINUTES`, `LOGIN_MAX_FAILURES_PER_USER`, `LOGIN_MAX_FAILURES_PER_ADDRESS`. See
`src/oloop_server/config.py`.

`TRUSTED_PROXIES` takes addresses or networks (`10.9.0.0/16`), comma separated. The `X-Forwarded-For`
header is only believed when the request comes from one of them; the address of the client is what the
limit on failed logins counts.

## License

Apache License 2.0, see [LICENSE](LICENSE).
