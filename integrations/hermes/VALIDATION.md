# Kestrel Windows — Mnemosyne Hermes memory-provider smoke

## Runtime memory-provider validation — 2026-06-11T05:09:22Z (2026-06-11 00:09:22 CDT)

**Machine label:** Kestrel Windows PC
**Branch:** `claude/gracious-newton-auh2kh`
**Purpose:** Make Mnemosyne work as Kestrel's local Hermes memory provider using SQLite + FTS5. Semantic embeddings were intentionally skipped.

## Environment

| item | value |
|---|---|
| Python | 3.11.15 `[MSC v.1944 64 bit (AMD64)]` |
| Hermes runtime | Hermes Agent v0.16.0 |
| Hermes root | `C:\Users\austi\AppData\Local\hermes\hermes-agent` |
| Hermes home | `C:\Users\austi\AppData\Local\hermes` |
| Hermes config path | `C:\Users\austi\AppData\Local\hermes\config.yaml` |
| Mnemosyne checkout | `C:\Users\austi\Repos\Mnemosyne` |
| `MNEMOSYNE_PATH` | `C:/Users/austi/Repos/Mnemosyne` |
| plugin install path | `C:\Users\austi\AppData\Local\hermes\plugins\mnemosyne` |
| plugin config path | `C:\Users\austi\AppData\Local\hermes\mnemosyne.json` |
| database path | `C:\Users\austi\.mnemosyne\kestrel-hermes.db` |

`memory.provider` is set to `mnemosyne` in Hermes config. The plugin is also enabled via `hermes plugins enable mnemosyne`.

## Explicitly out of scope for this round

- Hugging Face dependency: **not used**.
- sentence-transformers dependency: **not used**.
- LM Studio embeddings: **not used**.
- Dense/hybrid retrieval: **not used**.
- Semantic retrieval improvement: **not tested / not claimed**.

## Standalone smoke

Command:

```powershell
python experiments\hermes_plugin\mnemosyne\test_provider.py
```

Result: **PASS**.

Passed checks:

- `system_prompt_block`
- `get_tool_schemas`
- `memory_write` ×3
- `sync_turn`
- `queue_prefetch` / `prefetch`
- `memory_search`
- `memory_stats`
- `on_session_end`
- `on_pre_compress`
- `shutdown`

## Hermes/Kestrel runtime checks

Runtime check used Hermes' installed plugin loader and `MemoryManager` from:

`C:\Users\austi\AppData\Local\hermes\hermes-agent`

| check | result |
|---|---|
| plugin discovery | PASS — `discover_memory_providers()` returned `mnemosyne` with `available=True` |
| plugin enabled | PASS — `hermes plugins list --plain --no-bundled` shows `enabled user 0.1.0 mnemosyne` |
| provider load | PASS — `load_memory_provider("mnemosyne")` returned provider name `mnemosyne` |
| provider availability | PASS — imports `mnemosyne_memory.py` from `MNEMOSYNE_PATH` |
| session initialization | PASS — `MemoryManager.initialize_all()` initialized provider for `kestrel-runtime-mnemosyne-smoke` |
| tool routing | PASS — `memory_write`, `memory_search`, `memory_stats` registered in `MemoryManager` |
| memory_write | PASS — `Memory stored (id=1, tier=2, kind=fact).` |
| memory_search | PASS — returned planted memory |
| memory_stats | PASS — `Memory stats (total 3): tier 2: 3` |
| conversation turn persistence | PASS — two `kind='turn'` rows persisted for the smoke session |
| prefetch | PASS — non-empty context returned and included Mnemosyne content |
| shutdown / flush | PASS — `shutdown_all()` returned; SQLite verification found persisted rows |

Planted memory:

```text
Kestrel memory smoke test: Austin wants Mnemosyne as Hermes memory.
```

Search query:

```text
What does Austin want as Hermes memory?
```

Search result contained the planted memory.

SQLite verification after shutdown:

| probe | value |
|---|---:|
| database exists | true |
| total rows | 3 |
| planted smoke memory rows | 1 |
| persisted turn rows for smoke session | 2 |

## Windows path/config notes

- User plugin install path is `C:\Users\austi\AppData\Local\hermes\plugins\mnemosyne`; this is the path Hermes scans for user-installed memory providers.
- Main Mnemosyne checkout lives locally at `C:\Users\austi\Repos\Mnemosyne`.
- Mnemosyne DB lives locally at `C:\Users\austi\.mnemosyne\kestrel-hermes.db`.
- No Windows path issue observed with spaces in unrelated repo paths; this runtime integration uses local non-cloud paths.
- Fixed provider tool schemas to match Hermes `MemoryManager` routing expectations.
- Fixed stats to use SQLite counts instead of search-term sampling.

## Runtime integration status

**Confirmed through Hermes runtime modules.** Kestrel can use Mnemosyne as a Hermes memory provider for local SQLite-backed write/search/stats/turn persistence/shutdown.

One remaining operational confirmation is a fresh interactive Hermes chat process after restart, so the running assistant session also starts with the provider loaded from config.
