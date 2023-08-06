import os
import sys
import torch

import transformers

from transformers import (
    LlamaForSequenceClassification,
    LlamaTokenizer
)

from peft import (
    prepare_model_for_int8_training,
    set_peft_model_state_dict,
    PeftModel
)

from core.llm import LLM


class LLAMAClassify(LLM):
    tokenizer = None

    def get_model_tokenizer(self):
        model = LlamaForSequenceClassification.from_pretrained(
            self.base_model,
            load_in_8bit=self.load_8bit,
            device_map=self.device_map,
            low_cpu_mem_usage=True
        )
        tokenizer = LlamaTokenizer.from_pretrained(
            self.base_model,
            add_eos_token=self.add_eos_token
        )  # default add_eos_token=False

        # Some Models do not have pad_token
        if tokenizer.pad_token is None:
            tokenizer.add_special_tokens({'pad_token': tokenizer.eos_token})

        return model, tokenizer

    def tokenize_prompt(self, data_point):
        tokenize_res = self.tokenizer(data_point["input"], truncation=True, padding=False)
        tokenize_res["labels"] = torch.tensor(self.labels.index(data_point["output"]))

        return tokenize_res

    def split_train_data(self, data):
        if self.val_set_size > 0:
            train_val = data["train"].train_test_split(
                test_size=self.val_set_size, shuffle=True, seed=42
            )
            train_data = (
                train_val["train"].shuffle().map(self.tokenize_prompt).remove_columns(["input", "instruction", "output"])
            )
            val_data = (
                train_val["test"].shuffle().map(self.tokenize_prompt).remove_columns(["input", "instruction", "output"])
            )
        else:
            train_data = data["train"].shuffle().map(self.tokenize_prompt).remove_columns(["input", "instruction", "output"])
            val_data = None

        return train_data, val_data

    def finetune(self, fromdb, iteration):
        self.auto_device()

        if not self.lora_target_modules:
            if self.model_type == "llama2":
                self.lora_target_modules = [
                    "q_proj",
                    "v_proj",
                    "k_proj",
                    "o_proj",
                    "gate_proj",
                    "down_proj",
                    "up_proj"
                ]
            else:
                self.lora_target_modules = [
                    "q_proj",
                    "v_proj"
                ]

        model, self.tokenizer = self.get_model_tokenizer()
        if self.load_8bit:
            model = prepare_model_for_int8_training(model)

        model = self.load_adapter_config(model)

        data = self.load_train_data(fromdb, iteration)
        if not data:
            print("Warning! Empty Train Data!")
            return

        train_data, val_data = self.split_train_data(data)

        if self.resume_from_checkpoint:
            # Check the available weights and load them
            checkpoint_name = os.path.join(
                self.resume_from_checkpoint, "pytorch_model.bin"
            )  # Full checkpoint
            if not os.path.exists(checkpoint_name):
                checkpoint_name = os.path.join(
                    self.resume_from_checkpoint, "adapter_model.bin"
                )  # only LoRA model - LoRA config above has to fit
                self.resume_from_checkpoint = (
                    False  # So the trainer won't try loading its state
                )
            # The two files above have a different name depending on how they were saved, but are actually the same.
            if os.path.exists(checkpoint_name):
                print(f"Restarting from {checkpoint_name}")
                adapters_weights = torch.load(checkpoint_name)
                set_peft_model_state_dict(model, adapters_weights)
            else:
                print(f"Checkpoint {checkpoint_name} not found")

        total_batch_size = self.per_gpu_train_batch_size * self.gradient_accumulation_steps * (self.world_size if self.ddp else 1)
        total_optim_steps = train_data.num_rows // total_batch_size
        saving_step = int(total_optim_steps / 10)
        warmup_steps = int(total_optim_steps / 10)
        train_args = transformers.TrainingArguments(
            per_device_train_batch_size=self.per_gpu_train_batch_size,
            gradient_accumulation_steps=self.gradient_accumulation_steps,
            warmup_steps=warmup_steps,
            num_train_epochs=self.epochs,
            learning_rate=self.learning_rate,
            fp16=self.is_fp16,
            optim="adamw_torch",
            logging_steps=self.logging_steps,
            evaluation_strategy="steps" if self.val_set_size > 0 else "no",
            save_strategy="steps",
            eval_steps=saving_step if self.val_set_size > 0 else None,
            save_steps=saving_step,
            # max_steps=200,
            output_dir=self.output_dir,
            save_total_limit=11,
            load_best_model_at_end=True if self.val_set_size > 0 else False,
            ddp_find_unused_parameters=False if self.ddp else None,
            group_by_length=self.group_by_length,
            use_mps_device=self.use_mps_device,
            report_to=None if self.disable_wandb else "wandb"
        )

        trainer = transformers.Trainer(
            model=model,
            train_dataset=train_data,
            eval_dataset=val_data,
            args=train_args,
            data_collator=transformers.DataCollatorWithPadding(self.tokenizer, return_tensors="pt"),
        )

        model.config.use_cache = False

        if torch.__version__ >= "2" and sys.platform != "win32":
            model = torch.compile(model)

        trainer.train(resume_from_checkpoint=self.resume_from_checkpoint)

        model.save_pretrained(self.output_dir)

        print("\n If there's a warning about missing keys above, please disregard :)")

    def evaluate(self, model, input=None, **kwargs):
        inputs = self.tokenizer(input, return_tensors="pt")
        with torch.no_grad():
            logits = model(**inputs).logits
            predicted_class_idx = torch.argmax(logits, dim=1).item()

            return self.labels[predicted_class_idx]

    def generate(self, instruction, input, data, fromdb, type, iteration, test_iteration):
        self.auto_device()

        model, self.tokenizer = self.get_model_tokenizer()

        if self.adapter_weights != "None":
            model = PeftModel.from_pretrained(
                model,
                self.adapter_weights,
            )

        if not self.load_8bit:
            model.half()

        model.to(self.device).eval()

        eval_inputs = self.get_eval_input(instruction, input, data, fromdb, type, iteration)

        for item in eval_inputs:
            response = self.evaluate(model, item["input"])

            item["ac_output"] = response

        self.eval_output(eval_inputs, data, fromdb, type, iteration, test_iteration)


if __name__ == "__main__":
    llama = LLAMAClassify()
    llama.finetune()
