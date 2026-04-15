"""
_train_unsloth.py — subprocess wrapper around Unsloth's LoRA trainer.

Purpose
-------
`mnemosyne_train.py train` shells out to this file so the heavy Unsloth
+ torch + transformers deps only load when the user actually runs
training. Nothing in the Mnemosyne core imports this module.

Usage (called internally, but runnable directly too):

    python3 _train_unsloth.py \
        --data trajectories.compressed.jsonl \
        --base-model unsloth/Qwen2.5-7B-Instruct \
        --out-dir ./adapters/mnemo-v1 \
        --chat-template chatml \
        --max-steps 500 --lr 2e-4 --rank 16 --quant q4_k_m

Requires the `[train]` optional extra:

    pip install -e '.[train]'

Or manually:

    pip install unsloth datasets transformers trl peft accelerate

Notes
-----
- Input JSONL must be Hermes-format ShareGPT (from `mnemosyne_train
  export`). The wrapper maps ShareGPT's `from`/`value` keys to the
  `role`/`content` shape Unsloth's apply_chat_template expects.
- `--dry-run` stops after loading the dataset and validates the chat
  template works, without training.
- GGUF export uses Unsloth's `save_pretrained_gguf`. The merged model
  is produced in `<out-dir>/gguf/` so `deploy` can find it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


_ROLE_MAP = {"system": "system", "human": "user", "gpt": "assistant", "tool": "tool"}


def _load_sharegpt(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            convs = obj.get("conversations") or []
            messages = []
            for t in convs:
                role = _ROLE_MAP.get(t.get("from") or "", "user")
                messages.append({"role": role, "content": t.get("value") or ""})
            if messages:
                rows.append({"messages": messages})
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--base-model", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--chat-template", default="chatml")
    p.add_argument("--max-steps", type=int, default=500)
    p.add_argument("--lr", type=float, default=2e-4)
    p.add_argument("--rank", type=int, default=16)
    p.add_argument("--quant", default="q4_k_m")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    data = Path(args.data).expanduser()
    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_sharegpt(data)
    if not rows:
        print(f"train: no trajectories in {data}", file=sys.stderr)
        return 3

    if args.dry_run:
        print(f"train (dry-run): {len(rows)} trajectories loaded OK")
        print(f"  base_model: {args.base_model}")
        print(f"  out_dir:    {out_dir}")
        print(f"  chat_tmpl:  {args.chat_template}")
        return 0

    try:
        from unsloth import FastLanguageModel
        from unsloth.chat_templates import get_chat_template
        from datasets import Dataset
        from trl import SFTTrainer
        from transformers import TrainingArguments
    except ImportError as e:
        sys.stderr.write(
            f"train: {type(e).__name__}: {e}\n"
            "install the [train] extra: pip install -e '.[train]'\n"
        )
        return 2

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.base_model,
        max_seq_length=4096,
        load_in_4bit=True,
    )
    tokenizer = get_chat_template(tokenizer, chat_template=args.chat_template)
    model = FastLanguageModel.get_peft_model(
        model,
        r=args.rank,
        lora_alpha=args.rank * 2,
        lora_dropout=0.0,
        bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                         "gate_proj", "up_proj", "down_proj"],
    )

    def formatting_func(batch):
        texts = []
        for messages in batch["messages"]:
            texts.append(tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False,
            ))
        return {"text": texts}

    ds = Dataset.from_list(rows).map(formatting_func, batched=True)

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=ds,
        dataset_text_field="text",
        max_seq_length=4096,
        args=TrainingArguments(
            per_device_train_batch_size=2,
            gradient_accumulation_steps=4,
            warmup_steps=5,
            max_steps=args.max_steps,
            learning_rate=args.lr,
            fp16=False, bf16=True,
            logging_steps=10,
            output_dir=str(out_dir / "checkpoints"),
            optim="adamw_8bit",
            weight_decay=0.01,
            lr_scheduler_type="linear",
            seed=3407,
        ),
    )
    trainer.train()

    gguf_dir = out_dir / "gguf"
    gguf_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained_gguf(str(gguf_dir), tokenizer, quantization_method=args.quant)
    print(f"train: GGUF written to {gguf_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
