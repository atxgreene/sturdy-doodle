# Security model

What Mnemosyne defends against, what it doesn't, and how to harden a
deployment for hostile networks.

## Threat model

Mnemosyne is **local-first** and **single-tenant** by design. The
default install assumes:

- The user is trusted (it's their machine, their files, their agent).
- The local network is trusted *only when explicitly configured to be*
  (`--host 0.0.0.0` is opt-in; default is `127.0.0.1`).
- The model can be *adversarial* (it might try to make the agent run
  destructive tools, leak data, or impersonate another identity).
- The user's prompts can be *malformed* (typos, oversized, weird
  Unicode) but not actively hostile (this isn't a multi-tenant SaaS).

Out of scope:

- Multi-tenant isolation.
- Defending against a local user with shell access.
- Side-channel attacks (CPU timing, power analysis).
- Supply-chain attacks on the Python interpreter or stdlib.

## Audit summary (v0.3.5)

Audited in commit `<this release>`. Categories swept across the entire
codebase via grep + manual review:

| Class | Result |
|---|---|
| `shell=True` subprocess invocations | **none** outside the documented allow-list in `mnemosyne_skills_builtin.shell_exec_safe` |
| `eval` / `exec` / `compile` (other than `re.compile`) | **none** |
| `pickle` / `marshal` deserialization | **none** |
| `os.system` | **none** |
| Bare `urlopen()` without timeout | **none** |
| SQL injection via f-string / `%` formatting | **none** — every query is parameterized |
| File `open()` without explicit encoding | **none** outside binary writes |
| HTML escaping in UI-served strings | dashboard escapes via `escapeHtml()` in `app.js`; daemon JSON is returned as `application/json` |
| Path traversal in user-input paths | guarded via `_safe_join()` (`mnemosyne_skills_builtin`), `relative_to()` (`mnemosyne_serve._serve_static`, `obsidian_search`) |
| Token compare timing | constant-time via `hmac.compare_digest` |
| POST body DoS | 1 MiB cap rejected with HTTP 413 *before* the body is read |
| `.env` write atomicity | `umask 077` + `chmod 600` + atomic mv (`mnemosyne-wizard.sh`) |
| Atomic file writes (TOCTOU) | tmp + `os.replace` in `mnemosyne_avatar`, `mnemosyne_goals`, `mnemosyne_skills_builtin.fs_write_safe`, `mnemosyne_apply` |
| API keys logged in events | redacted by key name via `harness_telemetry.DEFAULT_REDACT_PATTERNS` |
| **SSRF in `http_get` / `web_fetch_text`** | **fixed in v0.3.5** — see below |

## Defenses

### Network

- **`mnemosyne-serve` binds 127.0.0.1 by default.** `--host 0.0.0.0`
  is explicit opt-in.
- **Bearer-token auth** (`MNEMOSYNE_SERVE_TOKEN` env or `--token`).
  Required when binding to anything other than loopback. Compared in
  constant time.
- **POST bodies capped at 1 MiB.** Larger requests rejected with HTTP
  413 before the body is consumed. Cap is `Handler.MAX_BODY_BYTES`.
- **No CORS headers ever.** Same-origin only.
- **SSE stream re-uses the same auth.** The dashboard falls back to
  polling `/recent_events` over `Authorization: Bearer …` when a token
  is set, since `EventSource` can't carry custom headers.

### Outbound HTTP (`http_get` / `web_fetch_text` skills)

- **SSRF defense**: hostname is resolved *before* the request fires;
  if any resolved address is private, loopback, link-local, reserved,
  multicast, or unspecified, the request is refused. Stops a model
  from being weaponized to probe `169.254.169.254` (cloud metadata),
  `127.0.0.1:11434` (Ollama), or RFC1918 networks.
- **Redirects are not followed.** A 3xx response is returned as-is;
  the caller decides whether to fetch the new location (which would
  go through the same SSRF check).
- **Timeout** — 10s default, configurable.
- **Size cap** — 2 MB default, configurable, enforced at read.
- **Schemes** — only `http://` and `https://`. `file://`, `ftp://`,
  `data://`, etc. rejected.
- **Override**: `allow_private=True` is available for tests; never
  expose this flag to model-callable surfaces.

### Filesystem (model-callable skills)

- **Root jail.** `fs_read`, `fs_list`, `fs_write_safe`, `grep_code`
  resolve every path under a root directory (default
  `$MNEMOSYNE_PROJECTS_DIR`). Any path that escapes the root via `..`
  or symlinks raises `PermissionError` before any I/O.
- **No overwrite by default.** `fs_write_safe(overwrite=True)` is
  explicit.
- **Atomic writes.** Tmp + `os.replace`, so partial writes never
  leave a half-written file visible.

### Shell (`shell_exec_safe`)

- **Allow-list.** First argv token must be in
  `_SHELL_ALLOWLIST` = `{ls, cat, head, tail, wc, file, git, which,
  pwd, date, uname, env, python3, pip}`. Any other command rejected.
- **No `shell=True`.** Argv form only; shlex-parsed.
- **Bounded.** 10 s timeout, 50 KB output cap per stream.

### SQLite (`sqlite_query`)

- **SELECT/WITH only.** Anything else (`DROP`, `INSERT`, `UPDATE`,
  `ATTACH`, ...) is rejected.
- **Single-statement.** Embedded `;` is rejected.
- **Bounded.** Default 200-row `LIMIT`, capped at the function level.

### Memory store

- **All queries parameterized.** No string interpolation, no f-strings
  in any `cur.execute()` call.
- **WAL + busy-timeout.** Concurrent opens won't corrupt the DB even
  under heavy `mnemosyne-batch` load (fixed in v0.3.1).
- **Schema-init lock.** Module-level `_SCHEMA_INIT_LOCK` serializes
  CREATE-VIRTUAL-TABLE across MemoryStore instances in the same
  interpreter. Cross-process is documented as a known limitation;
  workaround is `mnemosyne-memory stats` once before spawning workers.

### Telemetry

- **Redaction by key name.** `DEFAULT_REDACT_PATTERNS` matches
  `(?i)(api_?key|token|secret|password|bearer|auth)` and rewrites the
  value to `<redacted>` before write. Recursive — applies inside
  nested dicts.
- **Events written with default umask.** If you want strict 600
  across the whole tree, create the run directory yourself with
  `umask 077`. Documented in `harness_telemetry.py:65`.

### Identity lock (4-layer)

Specific to model adversarial behavior:

1. **System-prompt preamble** (`MNEMOSYNE_IDENTITY`) — 1.4 KB block
   that instructs the model on its identity, how to handle
   "what model are you" questions, and forbids first-person
   identification as Claude / GPT / Gemini / etc.
2. **`IDENTITY.md` extension** — user-editable file at
   `$PROJECTS_DIR/IDENTITY.md` appended to the preamble.
3. **Post-filter regex** — `enforce_identity()` rewrites first-person
   slips ("I am Claude") to ("I am Mnemosyne") in the assistant's
   response before the user sees it.
4. **Scenario validation** — `scenarios/jailbreak.jsonl` (40 attack
   prompts) measures slip rate per backend.

`enforce_identity_audit_only=True` keeps the lock detecting but not
rewriting, so you can measure slip rate without affecting UX.

## Hardening for production

If you're running Mnemosyne behind a reverse proxy, on a shared host,
or with a backend other than localhost:

```sh
# 1. Always set a token.
export MNEMOSYNE_SERVE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')

# 2. Bind to 127.0.0.1 only and front with nginx/Caddy if you need
#    public access. Don't use --host 0.0.0.0 directly.
mnemosyne-serve --host 127.0.0.1 --port 8484

# 3. Run under systemd with the sandbox unit.
bash deploy/install-service.sh

# 4. Use `enforce_identity_lock=True` (default) and ship
#    scenarios/jailbreak.jsonl as a regression suite for your backend.
mnemosyne-pipeline evaluate --scenarios scenarios/jailbreak.jsonl

# 5. Run mnemosyne-resolver check periodically to catch new skills
#    that ship with weak descriptions.
mnemosyne-resolver check --strict   # exit nonzero on warnings too
```

## Reporting a vulnerability

Open a [GitHub issue](https://github.com/atxgreene/Mnemosyne/issues)
prefixed `[security]`. For sensitive reports, email the maintainer
listed in `pyproject.toml`. Please include:

- Mnemosyne version (`mnemosyne-models version` or
  `pip show mnemosyne-harness`)
- Reproduction steps
- Affected component (e.g. `mnemosyne_serve`, `http_get` skill)
- Whether the issue is exploitable from the model surface (model
  tricks the agent) or the host surface (local attacker / network
  attacker)

## Known limitations

- **Cross-process schema race.** Documented in v0.3.1 release notes.
  Workaround: pre-create the schema with `mnemosyne-memory stats`.
- **Unbounded line-length on JSONL reads.** `events.jsonl`,
  `goals.jsonl`, etc. are parsed line-by-line with no size cap. A
  malicious local user with write access to the projects dir could
  craft a multi-GB single line that OOMs the daemon during avatar
  refresh. Mitigated by systemd `MemoryLimit=` in the deploy unit;
  a code-level fix is on the roadmap.
- **No HMAC verification on inbound webhooks** because we don't have
  any. If you add a `/webhook/<provider>` endpoint, verify the
  provider's signature header before processing.
- **The `[train]` extras pull in heavy dependencies** (torch,
  transformers, unsloth) that have their own audit surface. Train in
  a separate venv if you're uncomfortable mixing.
