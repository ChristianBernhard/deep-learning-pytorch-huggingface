from dataclasses import dataclass
from datetime import datetime
import logging
import os
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed, BitsAndBytesConfig
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import is_liger_kernel_available
from trl import SFTTrainer, TrlParser, ModelConfig, SFTConfig, get_peft_config
from datasets import load_dataset
from peft import AutoPeftModelForCausalLM

if is_liger_kernel_available():
    from liger_kernel.transformers import AutoLigerKernelForCausalLM



########################
# Custom dataclasses
########################
@dataclass
class ScriptArguments:
    dataset_id_or_path: str
    dataset_splits: str = "train"
    tokenizer_name_or_path: str = None
    merge_adapter: bool = False
    use_spectrum: bool = False


########################
# Setup logging
########################
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(handler)

########################
# Helper functions
########################

def get_checkpoint(training_args: SFTConfig):
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir):
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
    return last_checkpoint

def merge_peft_model(adapter_dir, save_dir):
    """Merge the adapter and base model at end of training. This is helpful for single model inference."""
    model = AutoPeftModelForCausalLM.from_pretrained(
        adapter_dir,
        low_cpu_mem_usage=True,
    )
    logger.info('Merging adapter and base model...')
    merged_model = model.merge_and_unload()  # merge adapter and base model
    merged_model.save_pretrained(save_dir, max_shard_size='3GB')

###########################################################################################################

def train_function(model_args: ModelConfig, script_args: ScriptArguments, training_args: SFTConfig):
    """Main training function."""
    #########################
    # Log parameters
    #########################
    logger.info(f'Model parameters {model_args}')
    logger.info(f'Script parameters {script_args}')
    logger.info(f'Training/evaluation parameters {training_args}')

    ###############
    # Load datasets
    ###############
    if script_args.dataset_id_or_path.endswith('.json'):
        train_dataset = load_dataset('json', data_files=script_args.dataset_id_or_path, split='train')
    else:
        train_dataset = load_dataset(script_args.dataset_id_or_path, split=script_args.dataset_splits)
    
    train_dataset = train_dataset.select(range(10000))
    
    logger.info(f'Loaded dataset with {len(train_dataset)} samples and the following features: {train_dataset.features}')
    
    ################
    # Load tokenizer
    ################
    tokenizer = AutoTokenizer.from_pretrained(
        script_args.tokenizer_name_or_path if script_args.tokenizer_name_or_path else model_args.model_name_or_path,
        revision=model_args.model_revision,
        trust_remote_code=model_args.trust_remote_code,
    )
    if tokenizer.pad_token is None: 
        tokenizer.pad_token = tokenizer.eos_token
    # if we use peft we need to make sure we use a chat template that is not using special tokens as by default embedding layers will not be trainable 
    
    
    #######################
    # Load pretrained model
    #######################

    # define model kwargs
    model_kwargs = dict(
        revision=model_args.model_revision, # What revision from Huggingface to use, defaults to main
        trust_remote_code=model_args.trust_remote_code, # Whether to trust the remote code, this also you to fine-tune custom architectures
        attn_implementation=model_args.attn_implementation, # What attention implementation to use, defaults to flash_attention_2
        torch_dtype=model_args.torch_dtype if model_args.torch_dtype in ['auto', None] else getattr(torch, model_args.torch_dtype), # What torch dtype to use, defaults to auto
        use_cache=False if training_args.gradient_checkpointing else True, # Whether
        low_cpu_mem_usage=True,  # Reduces memory usage on CPU for loading the model
    )
    
    # Check which training method to use and if 4-bit quantization is needed
    if model_args.load_in_4bit: 
        model_kwargs['quantization_config'] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type='nf4',
            bnb_4bit_compute_dtype=model_kwargs['torch_dtype'],
            bnb_4bit_quant_storage=model_kwargs['torch_dtype'],
        )
    if model_args.use_peft:
        peft_config = get_peft_config(model_args)
    else:
        peft_config = None
    
    # load the model with our kwargs
    if training_args.use_liger:
        model = AutoLigerKernelForCausalLM.from_pretrained(model_args.model_name_or_path, **model_kwargs)
    else:
        model = AutoModelForCausalLM.from_pretrained(model_args.model_name_or_path, **model_kwargs)
    training_args.distributed_state.wait_for_everyone()  # wait for all processes to load


    ########################
    # Initialize the Trainer
    ########################
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        tokenizer=tokenizer,
        peft_config=peft_config,
    )
    if trainer.accelerator.is_main_process and peft_config:
        trainer.model.print_trainable_parameters()

    ###############
    # Training loop
    ###############
    # Check for last checkpoint
    last_checkpoint = get_checkpoint(training_args)
    if last_checkpoint is not None and training_args.resume_from_checkpoint is None:
        logger.info(f'Checkpoint detected, resuming training at {last_checkpoint}.')

    logger.info(f'*** Starting training {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} for {training_args.num_train_epochs} epochs***')
    train_result = trainer.train(resume_from_checkpoint=last_checkpoint)
    # log metrics
    metrics = train_result.metrics
    metrics['train_samples'] = len(train_dataset)
    trainer.log_metrics('train', metrics)
    trainer.save_metrics('train', metrics)
    trainer.save_state()

    ##################################
    # Save model and create model card
    ##################################
    
    logger.info('*** Save model ***')
    if trainer.is_fsdp_enabled and peft_config:
        trainer.accelerator.state.fsdp_plugin.set_state_dict_type('FULL_STATE_DICT')
    # Restore k,v cache for fast inference
    trainer.model.config.use_cache = True
    if script_args.merge_adapter and peft_config:
        adapter_dir = os.path.join(training_args.output_dir, 'adapter')
        trainer.model.save_pretrained(adapter_dir)
        logger.info(f'Adapters saved to {adapter_dir}')
        logger.info('Merging adapter and base model...')
        if trainer.accelerator.is_main_process:
            # merge adapter and base model on main process
            merge_peft_model(adapter_dir, training_args.output_dir)
    else:
        trainer.save_model(training_args.output_dir)
        logger.info(f'Model saved to {training_args.output_dir}')

    tokenizer.save_pretrained(training_args.output_dir)
    logger.info(f'Tokenizer saved to {training_args.output_dir}')

    # Save everything else on main process
    kwargs = {
        'finetuned_from': model_args.model_name_or_path,
        'tags': ['sft', 'tutorial', 'philschmid'],
    }
    if trainer.accelerator.is_main_process:
        trainer.create_model_card(**kwargs)

    if training_args.push_to_hub is True:
        logger.info('Pushing to hub...')
        trainer.push_to_hub(**kwargs)

    logger.info('*** Training complete! ***')


def main():
    parser = TrlParser((ModelConfig, ScriptArguments, SFTConfig))
    model_args, script_args, training_args = parser.parse_args_and_config()

    # Set seed for reproducibility
    set_seed(training_args.seed)

    # Run the main training loop
    train_function(model_args, script_args, training_args)


if __name__ == '__main__':
    main()