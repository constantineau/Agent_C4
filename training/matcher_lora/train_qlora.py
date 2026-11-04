"""QLoRA SFT for the matcher LoRA (MATCHER_LORA_PLAN §5) — runs on a rented GPU (RunPod A100-class),
NOT on the Orin or the VM. Input: the gen_train.py JSONL (chat messages, assistant turn = target
JSON). Output: a merged full-precision model dir ready for GGUF conversion (merge-then-quantize —
no Ollama ADAPTER, the runtime loads one q4_K_M blob).

    python3 train_qlora.py --data matcher_train.jsonl --out ./out
    # then convert + quantize (llama.cpp) and `ollama create` on the Orin — see README.md

--max-len is the SERVING context window (pi/orin/copilot/config.py LLM_NUM_CTX, Ollama's
OLLAMA_CONTEXT_LENGTH / Modelfile num_ctx): train on what the Orin can actually hold. A row longer
than that is SKIPPED, never truncated — truncating from the right cut the assistant turn off, and
a row with no assistant marker left was trained with its whole prompt unmasked (#54).
"""

import argparse
import json

BASE = "Qwen/Qwen2.5-7B-Instruct"
ASSISTANT_MARK = "<|im_start|>assistant"      # chatml — loss only on the assistant turn
SERVE_NUM_CTX = 6144                          # keep equal to copilot config.LLM_NUM_CTX


def mask_row(ids, mark_ids, max_len):
    """input_ids -> {input_ids, labels, attention_mask} with everything up to and including the
    LAST assistant marker masked (-100), or None when the row is longer than max_len or has no
    assistant marker (nothing safe to train on)."""
    if len(ids) > max_len:
        return None
    for i in range(len(ids) - len(mark_ids), -1, -1):
        if ids[i:i + len(mark_ids)] == mark_ids:
            labels = [-100] * (i + len(mark_ids)) + list(ids[i + len(mark_ids):])
            return {"input_ids": list(ids), "labels": labels, "attention_mask": [1] * len(ids)}
    return None


def load_rows(path, tok, max_len):
    """Tokenize full chat transcripts; mask everything before (and including) the assistant
    marker so the model is trained ONLY to produce the target JSON, not to parrot the prompt.
    Returns (rows, skipped)."""
    rows, skipped = [], 0
    mark_ids = tok(ASSISTANT_MARK, add_special_tokens=False)["input_ids"]
    for line in open(path):
        msgs = json.loads(line)["messages"]
        text = tok.apply_chat_template(msgs, tokenize=False)
        row = mask_row(tok(text)["input_ids"], mark_ids, max_len)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def _collator(tok):
    import torch
    from transformers import DataCollatorForLanguageModeling

    class Collator(DataCollatorForLanguageModeling):
        def __init__(self, tok):
            super().__init__(tok, mlm=False)

        def torch_call(self, features):
            max_len = max(len(f["input_ids"]) for f in features)
            pad = self.tokenizer.pad_token_id
            batch = {"input_ids": [], "labels": [], "attention_mask": []}
            for f in features:
                n = max_len - len(f["input_ids"])
                batch["input_ids"].append(f["input_ids"] + [pad] * n)
                batch["labels"].append(f["labels"] + [-100] * n)
                batch["attention_mask"].append(f["attention_mask"] + [0] * n)
            return {k: torch.tensor(v) for k, v in batch.items()}

    return Collator(tok)


def main():
    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, Trainer,
                              TrainingArguments)

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="./out")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--max-len", type=int, default=SERVE_NUM_CTX,
                    help="tokens; the serving context window (LLM_NUM_CTX)")
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=4)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(BASE)
    tok.pad_token = tok.pad_token or tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        BASE, torch_dtype=torch.bfloat16, device_map="auto",
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True))
    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=args.rank * 2, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"]))
    model.print_trainable_parameters()

    rows, skipped = load_rows(args.data, tok, args.max_len)
    ds = Dataset.from_list(rows)
    print(f"{len(ds)} examples ({skipped} skipped: longer than {args.max_len} tokens or no "
          "assistant turn)")
    Trainer(
        model=model, train_dataset=ds, data_collator=_collator(tok),
        args=TrainingArguments(
            output_dir=args.out + "/ckpt", num_train_epochs=args.epochs,
            per_device_train_batch_size=args.batch, gradient_accumulation_steps=args.grad_accum,
            learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.03,
            logging_steps=10, save_strategy="epoch", bf16=True, report_to=[]),
    ).train()

    # merge-then-quantize: reload the base UNquantized, apply the adapter, merge, save full model
    print("merging adapter into a full-precision model...")
    model.save_pretrained(args.out + "/adapter")
    del model
    torch.cuda.empty_cache()
    from peft import PeftModel
    base = AutoModelForCausalLM.from_pretrained(BASE, torch_dtype=torch.bfloat16,
                                                device_map="auto")
    merged = PeftModel.from_pretrained(base, args.out + "/adapter").merge_and_unload()
    merged.save_pretrained(args.out + "/merged")
    tok.save_pretrained(args.out + "/merged")
    print(f"merged model -> {args.out}/merged  (next: GGUF convert + q4_K_M, see README)")


if __name__ == "__main__":
    main()
