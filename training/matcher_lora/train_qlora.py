"""QLoRA SFT for the matcher LoRA (MATCHER_LORA_PLAN §5) — runs on a rented GPU (RunPod A100-class),
NOT on the Orin or the VM. Input: the gen_train.py JSONL (chat messages, assistant turn = target
JSON). Output: a merged full-precision model dir ready for GGUF conversion (merge-then-quantize —
no Ollama ADAPTER, the runtime loads one q4_K_M blob).

    python3 train_qlora.py --data matcher_train.jsonl --out ./out
    # then convert + quantize (llama.cpp) and `ollama create` on the Orin — see README.md
"""

import argparse
import json

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig,
                          DataCollatorForLanguageModeling, Trainer, TrainingArguments)

BASE = "Qwen/Qwen2.5-7B-Instruct"
ASSISTANT_MARK = "<|im_start|>assistant"      # chatml — loss only on the assistant turn


def load_rows(path, tok, max_len):
    """Tokenize full chat transcripts; mask everything before (and including) the assistant
    marker so the model is trained ONLY to produce the target JSON, not to parrot the prompt."""
    rows = []
    mark_ids = tok(ASSISTANT_MARK, add_special_tokens=False)["input_ids"]
    for line in open(path):
        msgs = json.loads(line)["messages"]
        text = tok.apply_chat_template(msgs, tokenize=False)
        ids = tok(text, truncation=True, max_length=max_len)["input_ids"]
        labels = list(ids)
        # find the LAST assistant marker (the system prompt itself contains no chatml tokens)
        for i in range(len(ids) - len(mark_ids), -1, -1):
            if ids[i:i + len(mark_ids)] == mark_ids:
                for j in range(i + len(mark_ids)):
                    labels[j] = -100
                break
        rows.append({"input_ids": ids, "labels": labels, "attention_mask": [1] * len(ids)})
    return Dataset.from_list(rows)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="./out")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--max-len", type=int, default=2048)
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

    ds = load_rows(args.data, tok, args.max_len)
    print(f"{len(ds)} examples")
    Trainer(
        model=model, train_dataset=ds, data_collator=Collator(tok),
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
