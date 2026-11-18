# Plan: agents in oloop-server

Status: started 2026-11-18. The contract is `spec/AGENTS.md` and `spec/SERVER.md`; this file says in which order the server grows to meet it, and records the choices made on the way.

## Where the server stands

Members and keys are ready for agents: `project_members.kind` knows `agent`, `project_keys` holds one key per principal and key version, `revisions.written_by` is a principal id without a foreign key to the users, and the roles are checked in one place (`access.py`). What is missing is the sign-in. Every endpoint depends on `current_device` and uses `device.user_id` as the principal, and an agent has neither a device nor a row in `users`.

## Decisions

1. **Feed after a member is added.** The spec wants the documents of a project delivered again when a member joins; a saved cursor is past them. The cursor stays a plain number. When a member is added (a new row, not a change of role), the server gives every document of the project a new `seq`, as if it had been touched, without a new revision. Everybody in the project then gets each document once more with the same `rev`, and clients take that in their stride. No client needs a new rule, and the ordering of the feed by `seq` stays as it is. (A cursor that carries a membership version was considered and left out: documents of a new project would share one position, and a page boundary inside them loses documents unless the cursor carries a second number.)
2. **Lifetimes** as in the spec: token 90 days, renewal period one year. Both can be set when the agent is created. A token may not be valid for longer than 10 years.
3. **Names** of the agents of one user are unique among the agents that are not revoked, compared without regard to case. `oloop agent renew <name>` needs that.
4. **Token storage.** An own table for agent tokens (hash only), not the table of the user tokens, whose rows belong to a device. A token has `expires_at`, and `replaced_at` once a newer one was issued; a replaced token lives 10 more minutes.
5. **Who may renew.** The newest token and a replaced one within its 10 minutes (a lost response). Renewing needs room to grow: the new expiry is `min(now + lifetime, renewUntil)` and must be later than the current one, otherwise `403`. That is what `--no-renew` means.
6. **The server checks the form, not the cryptography.** It cannot open a registration or a statement; it checks their length (88 and 92 bytes) and that a rotation covers every key of the agent. The clients check the chain.

## Steps

Every step is its own commit, with tests. SQLite, as before.

1. **One principal in place of a device.** A dependency `current_principal` gives a `Principal(id, kind, user_id)`. Documents, blobs and the project list use it. Everything that is for users only (key bundle, logout, creating projects, later the management of agents) keeps `current_device`, which answers `403` to an agent. No new behaviour; the existing tests stay green.
2. **Tables** (migration 0005): `agents`, `agent_tokens`, `agent_keys` (the chain).
3. **Endpoints for the user:** create, list, revoke an agent, issue a new token.
4. **Signing in as an agent:** the bearer token is looked up among the agent tokens; expired, revoked or replaced-and-grace-over gives `401`; `lastUsedAt`. One test for each line of "what an agent cannot do".
5. **Members and keys:** list members, add a member (agents of the owner only, the newest key version is required), revoke a member with or without a new key version. Adding a member re-delivers the project in the feed (decision 1).
6. **The agent about itself:** renew the token, rotate the key pair (all or nothing).
7. **Housekeeping:** delete expired tokens, user and agent; update `SERVER.md` and `AGENTS.md` with what was decided; raise the pointer of the spec submodule.

Not part of it: building the image and updating the stack (done only when asked), the CLI with PyNaCl and the pins in the desktop client (a plan of their own).

## Risks

- Step 1 touches every endpoint. It adds nothing, so a failing test points at the refactoring.
- A mistake in the cryptography of registration, statement or key chain would only show with a real client. The vectors in `spec/fixtures/server/` are the check; the CLI is the first user of them.
- A lost credentials file loses the key pair of the agent. The spec does not solve this: the user makes a new agent.
