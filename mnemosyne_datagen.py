"""
mnemosyne_datagen.py — template-driven synthetic prompt generator.

Purpose
-------
Synthetic prompt datasets are useful when:
  - You want to bootstrap training data before having real conversations
  - You're testing how the agent handles a specific category at scale
  - You're generating regression scenarios for a new tool

This module reads a JSON config and produces a JSONL prompt file the
`mnemosyne-batch run` CLI consumes. The output is the standard prompt
shape (`{"id", "prompt", "tags", "metadata"}`).

Inspired by NousResearch/hermes-agent's `datagen-config-examples/`
(MIT). Their version uses YAML; we use JSON to stay stdlib-only. A
config can also be a `.yaml` file if PyYAML happens to be installed —
silently graceful when it isn't.

Config shape
------------
    {
      "templates": [
        "What is the capital of {country}?",
        "How many people live in {country}?",
        "What language is spoken in {country}?"
      ],
      "vars": {
        "country": ["France", "Spain", "Germany", "Japan"]
      },
      "tags": ["geo", "synthetic"],
      "metadata": {"source": "datagen-geo-v1"},
      "id_prefix": "geo",
      "shuffle": true,
      "limit": 50
    }

Result: `len(templates) × len(vars[country])` prompts, with one
`{"id": "geo-NNN", "prompt": "<expanded>", "tags": ["geo","synthetic"],
"metadata": {"source": "..."}}` per line.

Multiple variables Cartesian-multiply:

    "vars": {"country": ["France","Spain"], "topic": ["food","music"]}

→ 2×2 = 4 expansions per template.

Optional: `--judge` flag wraps each generated prompt with a
`expected_contains` field harvested from a per-template answer key,
turning the output into a `scenarios.jsonl`-compatible file rather
than a `prompts.jsonl`.

Stdlib only (with optional PyYAML).
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import re
import sys
from pathlib import Path
from typing import Any


_VAR_RE = re.compile(r"\{(\w+)\}")


# ---- config loading --------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
            return yaml.safe_load(text)
        except ImportError:
            sys.stderr.write(
                f"datagen: {path} is YAML but PyYAML is not installed; "
                "convert to JSON or `pip install pyyaml`\n"
            )
            sys.exit(2)
    return json.loads(text)


# ---- expansion -------------------------------------------------------------

def expand_template(template: str, bindings: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        return str(bindings.get(key, m.group(0)))
    return _VAR_RE.sub(sub, template)


def cartesian_bindings(vars_dict: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product over `vars_dict`. Empty dict → [{}]."""
    if not vars_dict:
        return [{}]
    keys = list(vars_dict.keys())
    values = [vars_dict[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def generate_prompts(config: dict[str, Any]) -> list[dict[str, Any]]:
    templates = config.get("templates") or []
    if not isinstance(templates, list) or not templates:
        return []
    vars_dict = config.get("vars") or {}
    if not isinstance(vars_dict, dict):
        vars_dict = {}
    base_tags = list(config.get("tags") or [])
    base_meta = dict(config.get("metadata") or {})
    id_prefix = str(config.get("id_prefix") or "syn")
    shuffle = bool(config.get("shuffle"))
    limit = config.get("limit")

    bindings_list = cartesian_bindings(vars_dict)
    out: list[dict[str, Any]] = []
    counter = 0
    for tmpl in templates:
        if not isinstance(tmpl, str):
            continue
        for binding in bindings_list:
            counter += 1
            text = expand_template(tmpl, binding)
            entry: dict[str, Any] = {
                "id": f"{id_prefix}-{counter:06d}",
                "prompt": text,
                "tags": list(base_tags),
                "metadata": {**base_meta, "bindings": binding,
                              "template": tmpl},
            }
            out.append(entry)

    if shuffle:
        rng = random.Random(int(config.get("seed", 0)) or None)
        rng.shuffle(out)
    if isinstance(limit, int) and limit > 0:
        out = out[:limit]
    return out


# ---- scenario expansion (optional --judge mode) ----------------------------

def to_scenarios(prompts: list[dict[str, Any]],
                  answer_key: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    """Convert prompt dicts into `scenarios.jsonl`-compatible entries.

    `answer_key` maps a template (or template substring) to a list of
    `expected_contains` tokens.
    """
    out: list[dict[str, Any]] = []
    for p in prompts:
        tmpl = (p.get("metadata") or {}).get("template", "")
        expected: list[str] = []
        if answer_key:
            for needle, tokens in answer_key.items():
                if needle in tmpl:
                    expected = list(tokens)
                    break
        out.append({
            "id": p["id"],
            "prompt": p["prompt"],
            "tags": p.get("tags") or [],
            "expected_contains": expected,
        })
    return out


# ---- IO --------------------------------------------------------------------

def write_jsonl(entries: list[dict[str, Any]], out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write("# Generated by mnemosyne-datagen. Edit by hand only if "
                "you accept that re-running will overwrite.\n")
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(entries)


# ---- CLI -------------------------------------------------------------------

def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mnemosyne-datagen",
        description="Generate synthetic prompt JSONL from a template "
                    "config. Output is consumable by mnemosyne-batch run.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    gp = sub.add_parser("generate", help="expand a config into JSONL")
    gp.add_argument("config", help="JSON or YAML config file")
    gp.add_argument("--out", required=True)
    gp.add_argument("--judge",
                     help="optional answer-key JSON file; output is then "
                          "scenarios.jsonl-compatible (with expected_contains)")
    gp.add_argument("--limit", type=int, default=None,
                     help="cap output count (overrides config.limit)")
    gp.add_argument("--seed", type=int, default=None,
                     help="random seed for --shuffle")
    gp.add_argument("--shuffle", action="store_true")
    gp.add_argument("--json", action="store_true")

    sp = sub.add_parser("preview", help="print the first N expansions to stdout")
    sp.add_argument("config")
    sp.add_argument("--n", type=int, default=10)

    args = p.parse_args(argv)

    if args.cmd == "preview":
        cfg = load_config(Path(args.config).expanduser())
        prompts = generate_prompts(cfg)[:args.n]
        for entry in prompts:
            print(json.dumps(entry, ensure_ascii=False))
        return 0

    if args.cmd == "generate":
        cfg = load_config(Path(args.config).expanduser())
        if args.shuffle:
            cfg["shuffle"] = True
        if args.limit is not None:
            cfg["limit"] = args.limit
        if args.seed is not None:
            cfg["seed"] = args.seed
        prompts = generate_prompts(cfg)

        if args.judge:
            answer_key = json.loads(
                Path(args.judge).expanduser().read_text(encoding="utf-8")
            )
            out_entries = to_scenarios(prompts, answer_key)
        else:
            out_entries = prompts

        out_path = Path(args.out).expanduser()
        n = write_jsonl(out_entries, out_path)
        if args.json:
            json.dump({"out": str(out_path), "count": n}, sys.stdout, indent=2)
            print()
            return 0
        print(f"datagen: wrote {n} entries → {out_path}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_main())
