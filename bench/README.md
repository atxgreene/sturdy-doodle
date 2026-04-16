# Comparative benchmarks (`bench/`)

Stuff in this directory **is not part of the Mnemosyne distribution.** It
ships in the repo so the methodology is reproducible, but it is not
installed by `pip install mnemosyne-harness`, has its own optional deps
(see `requirements.txt`), and is not tested in CI.

The point of this directory: when someone asks "OK, but what does
Mnemosyne actually score on LOCOMO vs Mem0?", we have a runnable answer
in the same repo as the substrate it benchmarks.

## What lives here

- `locomo.py` — runner skeleton for the LOCOMO benchmark
  (long-conversation memory recall; arxiv 2402.09727). Targets the
  four standard categories: single-hop, multi-hop, temporal, open-domain.
- `mem0_adapter.py` — thin wrapper around the Mem0 SDK so we can run the
  same scenarios through both substrates and compare. Requires
  `pip install -r bench/requirements.txt`.
- `requirements.txt` — `datasets`, `mem0ai`, `openai`, plus whichever
  embedding/LLM you want as the comparator. None of these are required
  for normal Mnemosyne use; they exist only for the benchmark scripts.
- (planned) `longmemeval.py` — LongMemEval runner once we have a stable
  comparison story on LOCOMO.

## How to run

Set up the optional dependencies in a separate venv to keep the
stdlib-only invariant of the main install intact:

```sh
python3 -m venv bench/.venv
bench/.venv/bin/pip install -r bench/requirements.txt
bench/.venv/bin/python bench/locomo.py --help
```

The runners write JSON reports to `bench/results/` (gitignored). Don't
commit raw API outputs — the harness output already has identifying
content from the conversation samples.

## Why a separate directory and not unit tests

1. **Deps.** Running Mem0 requires its SDK, an embedding provider, a
   vector backend, and an LLM API key. None of that belongs in the
   stdlib-only core surface.
2. **Cost.** A LOCOMO run hits a paid API. CI shouldn't run it on
   every push.
3. **Honesty.** Benchmark numbers belong in a dated, cited report, not
   in a passing test that flips green every CI run regardless of
   whether the underlying systems changed.

## Status as of v0.8.0

- `locomo.py` — skeleton lands in v0.8; first numbers TBD.
- `mem0_adapter.py` — skeleton lands in v0.8; first head-to-head TBD.
- LongMemEval — not yet stubbed.

If you have access to a paid LLM API and want to run these, the
skeletons are intentionally readable. PRs with reproducible numbers
welcome — include the dataset hash, the model + temperature, and the
exact `bench/locomo.py` command line.
