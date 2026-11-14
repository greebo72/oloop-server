"""The command line of the server."""
import argparse
import getpass
import re
import sys

import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from oloop_server.auth import username_key
from oloop_server.blobstore import collect_garbage
from oloop_server.config import get_settings
from oloop_server.db import make_engine
from oloop_server.migrate import upgrade
from oloop_server.models import User
from oloop_server.security import hash_password

MIN_PASSWORD_LENGTH = 12
_USERNAME = re.compile(r"^[A-Za-z0-9._@-]{1,100}$")


def _fail(message: str) -> int:
    print(f"oloop-server: {message}", file=sys.stderr)
    return 1


def create_user(username: str, password: str) -> int:
    """Creates an account. The server has no public registration."""
    if not _USERNAME.match(username):
        return _fail("the user name may hold letters, digits and . _ @ - (1 to 100 characters)")
    if len(password) < MIN_PASSWORD_LENGTH:
        return _fail(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")

    url = get_settings().database_url
    upgrade(url)
    engine = make_engine(url)
    with Session(engine) as session:
        key = username_key(username)
        if session.scalar(select(User).where(User.username_key == key)) is not None:
            return _fail(f"the user '{username}' exists already")
        session.add(User(username=username, username_key=key, password_hash=hash_password(password)))
        session.commit()

    print(f"created the user '{username}'")
    return 0


def _read_password(from_stdin: bool) -> str | None:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")

    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat: ") != first:
        return None
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oloop-server")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-migrate", action="store_true", help="do not update the database first")

    commands.add_parser("migrate", help="update the database to the current version")
    commands.add_parser("gc-blobs", help="remove the blobs nobody needs, once")

    user = commands.add_parser("create-user", help="create an account")
    user.add_argument("username")
    user.add_argument("--password-stdin", action="store_true",
                      help="read the password from the first line of the standard input")

    args = parser.parse_args(argv)

    if args.command == "migrate":
        upgrade(get_settings().database_url)
        return 0

    if args.command == "gc-blobs":
        settings = get_settings()
        upgrade(settings.database_url)
        result = collect_garbage(
            sessionmaker(make_engine(settings.database_url)), settings.blob_dir)
        print(f"removed {result.never_referenced} blobs never used, {result.dropped} dropped "
              f"ones and {result.stray_files} stray files")
        return 0

    if args.command == "create-user":
        password = _read_password(args.password_stdin)
        if password is None:
            return _fail("the passwords differ")
        return create_user(args.username, password)

    if not args.no_migrate:
        upgrade(get_settings().database_url)
    # The address of the client is read by the app itself, from the proxies it
    # trusts; uvicorn must not change it first.
    uvicorn.run("oloop_server.main:create_app", factory=True, host=args.host,
                port=args.port, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
