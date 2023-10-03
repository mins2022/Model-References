###############################################################################
# Copyright (C) 2023 Habana Labs, Ltd. an Intel Company
###############################################################################
# Bootstrapped from:
# https://github.com/huggingface/diffusers/blob/main/examples/dreambooth/train_dreambooth.py

import argparse
import hashlib
import inspect
import itertools
import math
import os
import random
import re
from pathlib import Path
from typing import Optional, List, Literal
import numpy as np

import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.checkpoint
from diffusers import (
    AutoencoderKL,
    DDPMScheduler,
    StableDiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers.optimization import get_scheduler
from huggingface_hub import HfFolder, Repository, whoami
from PIL import Image
from torch.utils.data import Dataset
from torchvision import transforms
from torchvision.transforms.functional import pil_to_tensor
from tqdm.auto import tqdm
from transformers import CLIPTextModel, CLIPTokenizer
import wandb
import fire

from lora_diffusion import (
    PivotalTuningDatasetCapation,
    extract_lora_ups_down,
    inject_trainable_lora,
    inject_trainable_lora_extended,
    inspect_lora,
    save_lora_weight,
    save_all,
    prepare_clip_model_sets,
    evaluate_pipe,
    UNET_EXTENDED_TARGET_REPLACE,
)

import sys
try:
  sys.path.append(os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "../../../common/")))
  from tools.synapse_profiler_api import SynapseProfilerApi, TraceType
except ImportError:
  pass

def get_vae_encode_output(vae, data, dtype, device):
        print("use_torch_compile : vae :Model is compiled")
        def vae_encode(vae, data, dtype, device):
            return vae.encode(data.to(dtype).to(device)).latent_dist.sample()
        vae_encode_compiled = torch.compile(vae_encode, backend="aot_hpu_training_backend")
        return vae_encode_compiled(vae, data, dtype, device)

def get_models(
    pretrained_model_name_or_path,
    pretrained_vae_name_or_path,
    revision,
    placeholder_tokens: List[str],
    initializer_tokens: List[str],
    device="cuda:0",
):

    tokenizer = CLIPTokenizer.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="tokenizer",
        revision=revision,
    )

    text_encoder = CLIPTextModel.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="text_encoder",
        revision=revision,
    )

    placeholder_token_ids = []

    for token, init_tok in zip(placeholder_tokens, initializer_tokens):
        num_added_tokens = tokenizer.add_tokens(token)
        if num_added_tokens == 0:
            raise ValueError(
                f"The tokenizer already contains the token {token}. Please pass a different"
                " `placeholder_token` that is not already in the tokenizer."
            )

        placeholder_token_id = tokenizer.convert_tokens_to_ids(token)

        placeholder_token_ids.append(placeholder_token_id)

        # Load models and create wrapper for stable diffusion

        text_encoder.resize_token_embeddings(len(tokenizer))
        token_embeds = text_encoder.get_input_embeddings().weight.data
        if init_tok.startswith("<rand"):
            # <rand-"sigma">, e.g. <rand-0.5>
            sigma_val = float(re.findall(r"<rand-(.*)>", init_tok)[0])

            token_embeds[placeholder_token_id] = (
                torch.randn_like(token_embeds[0]) * sigma_val
            )
            print(
                f"Initialized {token} with random noise (sigma={sigma_val}), empirically {token_embeds[placeholder_token_id].mean().item():.3f} +- {token_embeds[placeholder_token_id].std().item():.3f}"
            )
            print(f"Norm : {token_embeds[placeholder_token_id].norm():.4f}")

        elif init_tok == "<zero>":
            token_embeds[placeholder_token_id] = torch.zeros_like(token_embeds[0])
        else:
            token_ids = tokenizer.encode(init_tok, add_special_tokens=False)
            # Check if initializer_token is a single token or a sequence of tokens
            if len(token_ids) > 1:
                raise ValueError("The initializer token must be a single token.")

            initializer_token_id = token_ids[0]
            token_embeds[placeholder_token_id] = token_embeds[initializer_token_id]

    vae = AutoencoderKL.from_pretrained(
        pretrained_vae_name_or_path or pretrained_model_name_or_path,
        subfolder=None if pretrained_vae_name_or_path else "vae",
        revision=None if pretrained_vae_name_or_path else revision,
    )
    unet = UNet2DConditionModel.from_pretrained(
        pretrained_model_name_or_path,
        subfolder="unet",
        revision=revision,
    )

    return (
        text_encoder.to(device),
        vae.to(device),
        unet.to(device),
        tokenizer,
        placeholder_token_ids,
    )


@torch.no_grad()
def text2img_dataloader(
    train_dataset,
    train_batch_size,
    tokenizer,
    vae,
    text_encoder,
    cached_latents: bool = False,
    use_torch_compile:bool = False,
):

    if cached_latents:
        cached_latents_dataset = []
        for idx in tqdm(range(len(train_dataset))):
            batch = train_dataset[idx]
            # rint(batch)
            latents = None
            if use_torch_compile:
                latents = get_vae_encode_output(vae, batch["instance_images"].unsqueeze(0), vae.dtype, vae.device)
            else:
                latents = vae.encode(
                    batch["instance_images"].unsqueeze(0).to(dtype=vae.dtype).to(vae.device)
                ).latent_dist.sample()
            latents = latents * 0.18215
            batch["instance_images"] = latents.squeeze(0)
            cached_latents_dataset.append(batch)

    def collate_fn(examples):
        input_ids = [example["instance_prompt_ids"] for example in examples]
        pixel_values = [example["instance_images"] for example in examples]
        pixel_values = torch.stack(pixel_values)
        pixel_values = pixel_values.to(memory_format=torch.contiguous_format).float()

        input_ids = tokenizer.pad(
            {"input_ids": input_ids},
            padding="max_length",
            max_length=tokenizer.model_max_length,
            return_tensors="pt",
        ).input_ids

        batch = {
            "input_ids": input_ids,
            "pixel_values": pixel_values,
        }

        if examples[0].get("mask", None) is not None:
            batch["mask"] = torch.stack([example["mask"] for example in examples])

        return batch

    if cached_latents:

        train_dataloader = torch.utils.data.DataLoader(
            cached_latents_dataset,
            batch_size=train_batch_size,
            shuffle=True,
            collate_fn=collate_fn,
        )

        print("PTI : Using cached latent.")

    else:
        train_dataloader = torch.utils.data.DataLoader(
            train_dataset,
            batch_size=train_batch_size,
            shuffle=True,
            collate_fn=collate_fn,
        )

    return train_dataloader


def inpainting_dataloader(
    train_dataset, train_batch_size, tokenizer, vae, text_encoder
):
    def collate_fn(examples):
        input_ids = [example["instance_prompt_ids"] for example in examples]
        pixel_values = [example["instance_images"] for example in examples]
        mask_values = [example["instance_masks"] for example in examples]
        masked_image_values = [
            example["instance_masked_images"] for example in examples
        ]

        # Concat class and instance examples for prior preservation.
        # We do this to avoid doing two forward passes.
        if examples[0].get("class_prompt_ids", None) is not None:
            input_ids += [example["class_prompt_ids"] for example in examples]
            pixel_values += [example["class_images"] for example in examples]
            mask_values += [example["class_masks"] for example in examples]
            masked_image_values += [
                example["class_masked_images"] for example in examples
            ]

        pixel_values = (
            torch.stack(pixel_values).to(memory_format=torch.contiguous_format).float()
        )
        mask_values = (
            torch.stack(mask_values).to(memory_format=torch.contiguous_format).float()
        )
        masked_image_values = (
            torch.stack(masked_image_values)
            .to(memory_format=torch.contiguous_format)
            .float()
        )

        input_ids = tokenizer.pad(
            {"input_ids": input_ids},
            padding="max_length",
            max_length=tokenizer.model_max_length,
            return_tensors="pt",
        ).input_ids

        batch = {
            "input_ids": input_ids,
            "pixel_values": pixel_values,
            "mask_values": mask_values,
            "masked_image_values": masked_image_values,
        }

        if examples[0].get("mask", None) is not None:
            batch["mask"] = torch.stack([example["mask"] for example in examples])

        return batch

    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=train_batch_size,
        shuffle=True,
        collate_fn=collate_fn,
    )

    return train_dataloader


def loss_step(
    batch,
    unet,
    vae,
    text_encoder,
    scheduler,
    train_inpainting=False,
    t_mutliplier=1.0,
    mixed_precision=False,
    mask_temperature=1.0,
    cached_latents: bool = False,
    use_torch_compile: bool = False,
):
    weight_dtype = torch.float32
    if not cached_latents:
        latents = None
        if use_torch_compile:
            latents = get_vae_encode_output(vae, batch["pixel_values"], weight_dtype, unet.device)
        else:
            latents = vae.encode(
                batch["pixel_values"].to(dtype=weight_dtype).to(unet.device)
            ).latent_dist.sample()
        latents = latents * 0.18215

        if train_inpainting:
            masked_image_latents = None
            if use_torch_compile:
                masked_image_latents = get_vae_encode_output(vae, batch["masked_image_values"], weight_dtype, unet.device)
            else:
                masked_image_latents = vae.encode(
                    batch["masked_image_values"].to(dtype=weight_dtype).to(unet.device)
                ).latent_dist.sample()
            masked_image_latents = masked_image_latents * 0.18215
            mask = F.interpolate(
                batch["mask_values"].to(dtype=weight_dtype).to(unet.device),
                scale_factor=1 / 8,
            )
    else:
        latents = batch["pixel_values"]

        if train_inpainting:
            masked_image_latents = batch["masked_image_latents"]
            mask = batch["mask_values"]

    noise = torch.randn_like(latents)
    bsz = latents.shape[0]
    if use_torch_compile:
        timesteps = torch.randint(
            0,
            int(scheduler.config.num_train_timesteps * t_mutliplier),
            (bsz,),
            device="cpu",
        )
        timesteps =timesteps.to("hpu")
    else:
        timesteps = torch.randint(
            0,
            int(scheduler.config.num_train_timesteps * t_mutliplier),
            (bsz,),
            device=latents.device,
        )

    timesteps = timesteps.long()

    noisy_latents = scheduler.add_noise(latents, noise, timesteps)

    if train_inpainting:
        latent_model_input = torch.cat(
            [noisy_latents, mask, masked_image_latents], dim=1
        )
    else:
        latent_model_input = noisy_latents

    if mixed_precision:
        with torch.autocast(device_type=text_encoder.device.type,
                            dtype=torch.bfloat16 if text_encoder.device.type in ["hpu", "cpu"] else torch.float16,
                            enabled=mixed_precision):

            encoder_hidden_states = text_encoder(
                batch["input_ids"].to(text_encoder.device)
            )[0]

            model_pred = unet(
                latent_model_input, timesteps, encoder_hidden_states, return_dict=False
            )[0]
    else:

        encoder_hidden_states = text_encoder(
            batch["input_ids"].to(text_encoder.device)
        )[0]

        model_pred = unet(latent_model_input, timesteps, encoder_hidden_states, return_dict=False)[0]

    if scheduler.config.prediction_type == "epsilon":
        target = noise
    elif scheduler.config.prediction_type == "v_prediction":
        target = scheduler.get_velocity(latents, noise, timesteps)
    else:
        raise ValueError(f"Unknown prediction type {scheduler.config.prediction_type}")

    if batch.get("mask", None) is not None:

        mask = (
            batch["mask"]
            .to(model_pred.device)
            .reshape(
                model_pred.shape[0], 1, model_pred.shape[2] * 8, model_pred.shape[3] * 8
            )
        )
        # resize to match model_pred
        mask = F.interpolate(
            mask.float(),
            size=model_pred.shape[-2:],
            mode="nearest",
        )

        mask = (mask + 0.01).pow(mask_temperature)

        mask = mask / mask.max()

        model_pred = model_pred * mask

        target = target * mask

    loss = (
        F.mse_loss(model_pred.float(), target.float(), reduction="none")
        .mean([1, 2, 3])
        .mean()
    )

    return loss


def train_inversion(
    unet,
    vae,
    text_encoder,
    dataloader,
    num_steps: int,
    scheduler,
    index_no_updates,
    optimizer,
    save_steps: int,
    placeholder_token_ids,
    placeholder_tokens,
    save_path: str,
    tokenizer,
    lr_scheduler,
    test_image_path: str,
    cached_latents: bool,
    accum_iter: int = 1,
    log_wandb: bool = False,
    wandb_log_prompt_cnt: int = 10,
    class_token: str = "person",
    train_inpainting: bool = False,
    mixed_precision: bool = False,
    clip_ti_decay: bool = True,
    htcore = None,
    log_tb: bool = False,
    writer = None,
    use_synapse_profiler: bool = False,
    use_pytorch_profiler: bool = False,
    profiler_step: int = 3,
    profile_ti: bool = False,
    print_freq: int = 50,
    use_torch_compile:bool = False,
):

    progress_bar = tqdm(range(num_steps),mininterval=0,miniters=10, smoothing=1)
    progress_bar.set_description("Steps")
    global_step = 0

    if not profile_ti:
        use_synapse_profiler = False
        use_pytorch_profiler = False

    # Original Emb for TI
    orig_embeds_params = text_encoder.get_input_embeddings().weight.data.clone()

    if log_wandb or log_tb:
        preped_clip = prepare_clip_model_sets()

    index_updates = ~index_no_updates
    loss_sum = 0.0
    loss_list_ti = []
    pt_prof = None
    syn_prof = None

    if use_pytorch_profiler:
        activities = [torch.profiler.ProfilerActivity.CPU]
        if htcore:
            activities.append(torch.profiler.ProfilerActivity.HPU)
        pt_prof = torch.profiler.profile(
                activities=activities,
                schedule=torch.profiler.schedule(wait=0, warmup=0, active=1, repeat=1),
                on_trace_ready=torch.profiler.tensorboard_trace_handler("./trace-log"),
                record_shapes=True,
                with_stack=True)
        pt_prof.start()
    if use_synapse_profiler:
        syn_prof = SynapseProfilerApi()
    if htcore:
        torch.hpu.memory.reset_peak_memory_stats()
    for epoch in range(math.ceil(num_steps / len(dataloader))):
        unet.eval()
        text_encoder.train()
        for batch in dataloader:
            if syn_prof and global_step == profiler_step:
                syn_prof.profiler_start(TraceType.TraceDevice,0)
            lr_scheduler.step()
            logs={}
            with torch.set_grad_enabled(True):
                loss = (
                    loss_step(
                        batch,
                        unet,
                        vae,
                        text_encoder,
                        scheduler,
                        train_inpainting=train_inpainting,
                        mixed_precision=mixed_precision,
                        cached_latents=cached_latents,
                        use_torch_compile=use_torch_compile,
                    )
                    / accum_iter
                )

                loss.backward()
                if htcore:
                    htcore.mark_step()
                loss_list_ti.append(loss)
                if global_step % accum_iter == 0:
                    # print gradient of text encoder embedding
                    embedding_grad_norm = text_encoder.get_input_embeddings().weight.grad[index_updates, :].detach().norm(dim=-1).mean().item()
                    #print(embedding_grad_norm)
                    logs['ti/embedding_grad_norm']=embedding_grad_norm
                    optimizer.step()
                    optimizer.zero_grad(set_to_none=True)
                    if htcore:
                        htcore.mark_step()

                    with torch.no_grad():

                        # normalize embeddings
                        if clip_ti_decay:
                            pre_norm = (
                                text_encoder.get_input_embeddings()
                                .weight[index_updates, :]
                                .norm(dim=-1, keepdim=True)
                            )

                            lambda_ = min(1.0, 100 * lr_scheduler.get_last_lr()[0])
                            text_encoder.get_input_embeddings().weight[
                                index_updates
                            ] = F.normalize(
                                text_encoder.get_input_embeddings().weight[
                                    index_updates, :
                                ],
                                dim=-1,
                            ) * (
                                pre_norm + lambda_ * (0.4 - pre_norm)
                            )
                            #print(pre_norm)

                        current_norm = (
                            text_encoder.get_input_embeddings()
                            .weight[index_updates, :]
                            .norm(dim=-1)
                        )

                        text_encoder.get_input_embeddings().weight[
                            index_no_updates
                        ] = orig_embeds_params[index_no_updates]

                        if htcore:
                            htcore.mark_step()
                        #print(f"Current Norm : {current_norm}")
                        logs["ti/current_norm"]=current_norm.detach().mean().item()
                        logs['ti/pre_norm']=pre_norm.detach().mean().item()

                if pt_prof and global_step == profiler_step:
                    pt_prof.step()
                if syn_prof and global_step == profiler_step:
                    syn_prof.profiler_sync(0)
                    syn_prof.profiler_stop(TraceType.TraceDevice, 0)
                    syn_prof.profiler_get_trace_json(TraceType.TraceDevice, 0)
                if(global_step % print_freq == 0):
                    progress_bar.update(print_freq)
                    logs.update({
                    "ti/step_loss": loss.detach().item(),
                    "ti/lr": lr_scheduler.get_last_lr()[0],
                     })
                    for loss_t in loss_list_ti:
                        loss_sum += loss_t.detach().item()
                    loss_list_ti.clear()
                global_step += 1
                #progress_bar.set_postfix(**logs)

            if global_step % save_steps == 0:
                save_all(
                    unet=unet,
                    text_encoder=text_encoder,
                    placeholder_token_ids=placeholder_token_ids,
                    placeholder_tokens=placeholder_tokens,
                    save_path=os.path.join(
                        save_path, f"step_inv_{global_step}.{'safetensors' if text_encoder.device.type != 'cpu' else 'pt'}"
                    ),
                    safe_form=True if text_encoder.device.type != "cpu" else False,
                    save_lora=False,
                )
                if log_wandb or log_tb:
                    with torch.no_grad():
                        pipe = StableDiffusionPipeline(
                            vae=vae,
                            text_encoder=text_encoder,
                            tokenizer=tokenizer,
                            unet=unet,
                            scheduler=scheduler,
                            safety_checker=None,
                            feature_extractor=None,
                        )

                        # open all images in test_image_path
                        images = []
                        for file in os.listdir(test_image_path):
                            if (
                                file.lower().endswith(".png")
                                or file.lower().endswith(".jpg")
                                or file.lower().endswith(".jpeg")
                            ):
                                images.append(
                                        Image.open(os.path.join(test_image_path, file))
                                )

                        logs.update({"ti/loss": loss_sum / save_steps})
                        loss_sum = 0.0
                        evaluation_metrics,images = evaluate_pipe(
                                pipe,
                                target_images=images,
                                class_token=class_token,
                                learnt_token="".join(placeholder_tokens),
                                n_test=wandb_log_prompt_cnt,
                                n_step=50,
                                clip_model_sets=preped_clip
                                )
                        if log_wandb:
                            logs.update({ f'{prompt}': wandb.Image(img) for e,(img,prompt) in enumerate(images) })
                        elif log_tb:
                            logs.update({"ti/images": images})
                        logs.update({ f'ti/{k}':v for k,v in evaluation_metrics.items() })
                        # logs.update({ f'image_{e}': wandb.Image(img,caption=prompt) for e,(img,prompt) in enumerate(v) if k=='images' else  f'ti/{k}':v for k,v in
                        #     evaluate_pipe(
                        #         pipe,
                        #         target_images=images,
                        #         class_token=class_token,
                        #         learnt_token="".join(placeholder_tokens),
                        #         n_test=wandb_log_prompt_cnt,
                        #         n_step=50,
                        #         clip_model_sets=preped_clip,
                        #     ).items()}
                        # )

            progress_bar.set_postfix(**logs)
            progress_bar.refresh()
            if log_wandb:
                wandb.log(logs)
            elif log_tb:
                for key, value in logs.items():
                    if key == "ti/images":
                        for img, prompt in images:
                            writer.add_image(prompt, pil_to_tensor(img), global_step)
                    elif key == "ti/image_alignment_all" or key == "ti/text_alignment_all":
                        for i in range(len(value)):
                            writer.add_scalar(key+str(i), value[i], global_step)
                    else:
                        writer.add_scalar(key, value, global_step)
            if global_step >= num_steps:
                break
    if pt_prof:
        pt_prof.stop()

    if htcore:
        max_memory = torch.hpu.memory.max_memory_allocated() / 2 ** 30
        print(f"Inversion Training Peak memory {(max_memory):.2f} GiB")

def perform_tuning(
    unet,
    vae,
    text_encoder,
    dataloader,
    num_steps,
    scheduler,
    optimizer,
    save_steps: int,
    placeholder_token_ids,
    placeholder_tokens,
    save_path,
    lr_scheduler_lora,
    lora_unet_target_modules,
    lora_clip_target_modules,
    mask_temperature,
    out_name: str,
    tokenizer,
    test_image_path: str,
    cached_latents: bool,
    log_wandb: bool = False,
    wandb_log_prompt_cnt: int = 10,
    class_token: str = "person",
    train_inpainting: bool = False,
    htcore = None,
    log_tb: bool = False,
    writer = None,
    use_synapse_profiler: bool = False,
    use_pytorch_profiler: bool = False,
    profiler_step: int = 3,
    profile_tuning: bool = False,
    print_freq: int =50,
    use_fused_clip_norm: bool = True,
    use_torch_compile:bool = False,
):

    progress_bar = tqdm(range(num_steps),mininterval=0,miniters=10, smoothing=1)
    progress_bar.set_description("Steps")
    global_step = 0

    if not profile_tuning:
        use_synapse_profiler = False
        use_pytorch_profiler = False

    weight_dtype = torch.float16
    if htcore and not use_torch_compile:
        htcore.hpu.ModuleCacher(max_graphs=5)(model=text_encoder,
                                        inplace=True,
                                        use_lfu=False,
                                        verbose=False)
    unet.train()
    text_encoder.train()

    if log_wandb or log_tb:
        preped_clip = prepare_clip_model_sets()

    loss_sum = 0.0
    loss_list = []

    pt_prof = None
    syn_prof = None
    if use_pytorch_profiler:
        activities = [torch.profiler.ProfilerActivity.CPU]
        if htcore:
            activities.append(torch.profiler.ProfilerActivity.HPU)
        pt_prof = torch.profiler.profile(
                activities=activities,
                schedule=torch.profiler.schedule(wait=0, warmup=0, active=1, repeat=1),
                on_trace_ready=torch.profiler.tensorboard_trace_handler("./trace-log"),
                record_shapes=True,
                with_stack=True)
        pt_prof.start()
    if use_synapse_profiler:
        syn_prof = SynapseProfilerApi()

    if use_fused_clip_norm and htcore:
        from habana_frameworks.torch.hpex.normalization import FusedClipNorm
        fused_clip_norm = FusedClipNorm(itertools.chain(unet.parameters(), text_encoder.parameters()), 1.0)
    if htcore:
        torch.hpu.memory.reset_peak_memory_stats()
    for epoch in range(math.ceil(num_steps / len(dataloader))):
        for batch in dataloader:
            if syn_prof and global_step == profiler_step:
                syn_prof.profiler_start(TraceType.TraceDevice,0)
            lr_scheduler_lora.step()

            optimizer.zero_grad(set_to_none=True)

            loss = loss_step(
                batch,
                unet,
                vae,
                text_encoder,
                scheduler,
                train_inpainting=train_inpainting,
                t_mutliplier=0.8,
                mixed_precision=True,
                mask_temperature=mask_temperature,
                cached_latents=cached_latents,
                use_torch_compile=use_torch_compile,
            )
            loss_list.append(loss)

            loss.backward()
            if htcore:
                htcore.mark_step()
            if use_fused_clip_norm:
                fused_clip_norm.clip_norm(itertools.chain(unet.parameters(), text_encoder.parameters()))
            else:
                torch.nn.utils.clip_grad_norm_(
                    itertools.chain(unet.parameters(), text_encoder.parameters()), 1.0
                )
            optimizer.step()
            if htcore:
                htcore.mark_step()
            if(global_step % print_freq == 0):
                progress_bar.update(print_freq)
                logs = {
                    "tuning/step_loss": loss.detach().item(),
                    "tuning/lr": lr_scheduler_lora.get_last_lr()[0],
                 }
                for loss_t in loss_list:
                    loss_sum += loss_t.detach().item()
                loss_list.clear()

            if pt_prof and global_step == profiler_step:
                pt_prof.step()
            if syn_prof and global_step == profiler_step:
                syn_prof.profiler_sync(0)
                syn_prof.profiler_stop(TraceType.TraceDevice, 0)
                syn_prof.profiler_get_trace_json(TraceType.TraceDevice, 0)
            #progress_bar.set_postfix(**logs)
            global_step += 1

            if global_step % save_steps == 0:
                save_all(
                    unet,
                    text_encoder,
                    placeholder_token_ids=placeholder_token_ids,
                    placeholder_tokens=placeholder_tokens,
                    save_path=os.path.join(
                        save_path, f"step_{global_step}.{'safetensors' if text_encoder.device.type != 'cpu' else 'pt'}"
                    ),
                    safe_form=True if text_encoder.device.type != "cpu" else False,
                    target_replace_module_text=lora_clip_target_modules,
                    target_replace_module_unet=lora_unet_target_modules,
                )
                unet_moved = (
                    torch.tensor(list(itertools.chain(*inspect_lora(unet).values())))
                    .mean()
                    .item()
                )

                #print("LORA Unet Moved", unet_moved)
                clip_moved = (
                    torch.tensor(
                        list(itertools.chain(*inspect_lora(text_encoder).values()))
                    )
                    .mean()
                    .item()
                )

                #print("LORA CLIP Moved", clip_moved)

                if log_wandb or log_tb:
                    with torch.no_grad():
                        pipe = StableDiffusionPipeline(
                            vae=vae,
                            text_encoder=text_encoder,
                            tokenizer=tokenizer,
                            unet=unet,
                            scheduler=scheduler,
                            safety_checker=None,
                            feature_extractor=None,
                        )

                        # open all images in test_image_path
                        images = []
                        for file in os.listdir(test_image_path):
                            if file.endswith(".png") or file.endswith(".jpg"):
                                images.append(
                                    Image.open(os.path.join(test_image_path, file))
                                )

                        #wandb.log({"loss": loss_sum / save_steps, "unet_lora_step":unet_moved,"clip_lora_moved":clip_moved})
                        logs.update({"tuning/loss": loss_sum / save_steps, "tuning/unet_lora_step":unet_moved,"tuning/clip_lora_moved":clip_moved})
                        loss_sum = 0.0
                        #wandb.log(
                        evaluation_metrics,images = evaluate_pipe(
                                pipe,
                                target_images=images,
                                class_token=class_token,
                                learnt_token="".join(placeholder_tokens),
                                n_test=wandb_log_prompt_cnt,
                                n_step=50,
                                clip_model_sets=preped_clip
                                )
                        if log_wandb:
                            logs.update({ f'{prompt}': wandb.Image(img) for e,(img,prompt) in enumerate(images) } )
                        elif log_tb:
                            logs.update({"tuning/images": images})
                        logs.update({ f'tuning/{k}':v for k,v in evaluation_metrics.items() })

                        # logs.update({ ( f'image_{e}': wandb.Image(img,caption=prompt) for e,(img,prompt) in enumerate(v) ) if k=='images' else f'tuning/{k}':v for k,v in evaluate_pipe(
                        #         pipe,
                        #         target_images=images,
                        #         class_token=class_token,
                        #         learnt_token="".join(placeholder_tokens),
                        #         n_test=wandb_log_prompt_cnt,
                        #         n_step=50,
                        #         clip_model_sets=preped_clip,
                        #     ).items()}
                        # )

            progress_bar.set_postfix(**logs)
            progress_bar.refresh()

            if log_wandb:
                wandb.log(logs)
            elif log_tb:
                for key, value in logs.items():
                    if key == "tuning/images":
                        for img, prompt in images:
                            writer.add_image(prompt, pil_to_tensor(img), global_step)
                    elif key == "tuning/image_alignment_all" or key == "tuning/text_alignment_all":
                        for i in range(len(value)):
                            writer.add_scalar(key+str(i), value[i], global_step)
                    else:
                        writer.add_scalar(key, value, global_step)

            if global_step >= num_steps:
                break

    if htcore:
        max_memory = torch.hpu.memory.max_memory_allocated() / 2 ** 30
        print(f"Average Peak memory {(max_memory):.2f} GiB")
    if pt_prof:
        pt_prof.stop()
    save_all(
        unet,
        text_encoder,
        placeholder_token_ids=placeholder_token_ids,
        placeholder_tokens=placeholder_tokens,
        save_path=os.path.join(save_path, f"{out_name}.{'safetensors' if text_encoder.device.type != 'cpu' else 'pt'}"),
        safe_form=True if text_encoder.device.type != "cpu" else False,
        target_replace_module_text=lora_clip_target_modules,
        target_replace_module_unet=lora_unet_target_modules,
    )


def train(
    instance_data_dir: str,
    pretrained_model_name_or_path: str,
    output_dir: str,
    train_text_encoder: bool = True,
    pretrained_vae_name_or_path: str = None,
    revision: Optional[str] = None,
    perform_inversion: bool = True,
    use_template: Literal[None, "object", "style"] = None,
    train_inpainting: bool = False,
    placeholder_tokens: str = "",
    placeholder_token_at_data: Optional[str] = None,
    initializer_tokens: Optional[str] = None,
    seed: int = 42,
    resolution: int = 512,
    color_jitter: bool = True,
    train_batch_size: int = 1,
    sample_batch_size: int = 1,
    max_train_steps_tuning: int = 1000,
    max_train_steps_ti: int = 1000,
    save_steps: int = 100,
    gradient_accumulation_steps: int = 4,
    gradient_checkpointing: bool = False,
    lora_rank: int = 4,
    lora_unet_target_modules={"CrossAttention", "Attention", "GEGLU"},
    lora_clip_target_modules={"CLIPAttention"},
    lora_dropout_p: float = 0.0,
    lora_scale: float = 1.0,
    use_extended_lora: bool = False,
    clip_ti_decay: bool = True,
    learning_rate_unet: float = 1e-4,
    learning_rate_text: float = 1e-5,
    learning_rate_ti: float = 5e-4,
    continue_inversion: bool = False,
    continue_inversion_lr: Optional[float] = None,
    use_face_segmentation_condition: bool = False,
    cached_latents: bool = True,
    use_mask_captioned_data: bool = False,
    mask_temperature: float = 1.0,
    scale_lr: bool = False,
    lr_scheduler: str = "linear",
    lr_warmup_steps: int = 0,
    lr_scheduler_lora: str = "linear",
    lr_warmup_steps_lora: int = 0,
    weight_decay_ti: float = 0.00,
    weight_decay_lora: float = 0.001,
    use_8bit_adam: bool = False,
    device="cuda:0",
    extra_args: Optional[dict] = None,
    log_wandb: bool = False,
    wandb_log_prompt_cnt: int = 10,
    wandb_project_name: str = "new_pti_project",
    wandb_entity: str = "new_pti_entity",
    proxy_token: str = "person",
    enable_xformers_memory_efficient_attention: bool = False,
    out_name: str = "final_lora",
    use_lazy_mode: bool = True,
    use_fused_adamw: bool = True,
    log_tb: bool = False,
    log_dir: str = "/tmp/runs/",
    use_synapse_profiler: bool = False,
    use_pytorch_profiler: bool = False,
    profiler_step: int = 3,
    profile_ti: bool = False,
    profile_tuning: bool = False,
    print_freq: int = 50,
    use_fused_clip_norm: bool = True,
    use_torch_compile: bool = False,
):
    htcore = None
    writer = None
    if use_torch_compile:
        if device != "hpu":
            use_lazy_mode = False
        assert not use_lazy_mode, f"use_torch_compile and use_lazy_mode both can't be used"
    if device == "hpu":
        if not use_lazy_mode and not use_torch_compile:
            os.environ["PT_HPU_LAZY_MODE"] = "2"
            import habana_frameworks.torch.core
        else:
            import habana_frameworks.torch.core as htcore
        # Disable hpu dynamic shape
        try:
            import habana_frameworks.torch.hpu as hthpu
            hthpu.disable_dynamic_shape()
        except ImportError:
            print("habana_frameworks could not be loaded")
    else:
        use_lazy_mode = False
        use_fused_adamw = False
        use_fused_clip_norm = False

    torch.manual_seed(seed)

    if log_wandb:
        wandb.init(
            project=wandb_project_name,
            entity=wandb_entity,
            name=f"steps_{max_train_steps_ti}_lr_{learning_rate_ti}_{instance_data_dir.split('/')[-1]}",
            reinit=True,
            config={
                **(extra_args if extra_args is not None else {}),
            },
        )
    elif log_tb:
        from torch.utils.tensorboard import SummaryWriter
        from datetime import datetime
        writer = SummaryWriter(log_dir=(log_dir+datetime.now().strftime("%b%d_%H-%M-%S")))

    if output_dir is not None:
        os.makedirs(output_dir, exist_ok=True)
    # print(placeholder_tokens, initializer_tokens)
    if len(placeholder_tokens) == 0:
        placeholder_tokens = []
        print("PTI : Placeholder Tokens not given, using null token")
    else:
        placeholder_tokens = placeholder_tokens.split("|")

        assert (
            sorted(placeholder_tokens) == placeholder_tokens
        ), f"Placeholder tokens should be sorted. Use something like {'|'.join(sorted(placeholder_tokens))}'"

    if initializer_tokens is None:
        print("PTI : Initializer Tokens not given, doing random inits")
        initializer_tokens = ["<rand-0.017>"] * len(placeholder_tokens)
    else:
        initializer_tokens = initializer_tokens.split("|")

    assert len(initializer_tokens) == len(
        placeholder_tokens
    ), "Unequal Initializer token for Placeholder tokens."

    if proxy_token is not None:
        class_token = proxy_token
    class_token = "".join(initializer_tokens)

    if placeholder_token_at_data is not None:
        tok, pat = placeholder_token_at_data.split("|")
        token_map = {tok: pat}

    else:
        token_map = {"DUMMY": "".join(placeholder_tokens)}

    print("PTI : Placeholder Tokens", placeholder_tokens)
    print("PTI : Initializer Tokens", initializer_tokens)

    # get the models
    text_encoder, vae, unet, tokenizer, placeholder_token_ids = get_models(
        pretrained_model_name_or_path,
        pretrained_vae_name_or_path,
        revision,
        placeholder_tokens,
        initializer_tokens,
        device=device,
    )

    if use_torch_compile:
        if device == "hpu":
            print("use_torch_compile : text_encoder :Model is compiled")
            text_encoder = torch.compile(text_encoder, backend="aot_hpu_training_backend")
            print("use_torch_compile : unet :Model is compiled")
            unet = torch.compile(unet, backend="aot_hpu_training_backend")

    noise_scheduler = DDPMScheduler.from_config(
        pretrained_model_name_or_path, subfolder="scheduler"
    )

    if gradient_checkpointing:
        unet.enable_gradient_checkpointing()

    if enable_xformers_memory_efficient_attention:
        from diffusers.utils.import_utils import is_xformers_available

        if is_xformers_available():
            unet.enable_xformers_memory_efficient_attention()
        else:
            raise ValueError(
                "xformers is not available. Make sure it is installed correctly"
            )

    if scale_lr:
        unet_lr = learning_rate_unet * gradient_accumulation_steps * train_batch_size
        text_encoder_lr = (
            learning_rate_text * gradient_accumulation_steps * train_batch_size
        )
        ti_lr = learning_rate_ti * gradient_accumulation_steps * train_batch_size
    else:
        unet_lr = learning_rate_unet
        text_encoder_lr = learning_rate_text
        ti_lr = learning_rate_ti

    train_dataset = PivotalTuningDatasetCapation(
        instance_data_root=instance_data_dir,
        token_map=token_map,
        use_template=use_template,
        tokenizer=tokenizer,
        size=resolution,
        color_jitter=color_jitter,
        use_face_segmentation_condition=use_face_segmentation_condition,
        use_mask_captioned_data=use_mask_captioned_data,
        train_inpainting=train_inpainting,
    )

    train_dataset.blur_amount = 200

    if train_inpainting:
        assert not cached_latents, "Cached latents not supported for inpainting"

        train_dataloader = inpainting_dataloader(
            train_dataset, train_batch_size, tokenizer, vae, text_encoder
        )
    else:
        train_dataloader = text2img_dataloader(
            train_dataset,
            train_batch_size,
            tokenizer,
            vae,
            text_encoder,
            cached_latents=cached_latents,
            use_torch_compile=use_torch_compile
        )

    index_no_updates = torch.arange(len(tokenizer)) != -1
    if htcore and not use_torch_compile:
        htcore.hpu.ModuleCacher(max_graphs=5)(model=unet,
                                        inplace=True,
                                        use_lfu=False,
                                        verbose=False)

    for tok_id in placeholder_token_ids:
        index_no_updates[tok_id] = False

    unet.requires_grad_(False)
    vae.requires_grad_(False)

    params_to_freeze = itertools.chain(
        text_encoder.text_model.encoder.parameters(),
        text_encoder.text_model.final_layer_norm.parameters(),
        text_encoder.text_model.embeddings.position_embedding.parameters(),
    )
    for param in params_to_freeze:
        param.requires_grad = False

    #if cached_latents:
        #vae = None
    # STEP 1 : Perform Inversion
    if perform_inversion:
        ti_optimizer = None
        if use_fused_adamw and htcore:
            from habana_frameworks.torch.hpex.optimizers import FusedAdamW
            ti_optimizer = FusedAdamW(text_encoder.get_input_embeddings().parameters(), lr=ti_lr, eps=1e-08, weight_decay=weight_decay_ti)
        else:
            ti_optimizer = optim.AdamW(
                text_encoder.get_input_embeddings().parameters(), #weight[~index_no_updates]],
                lr=ti_lr,
                betas=(0.9, 0.999),
                eps=1e-08,
                weight_decay=weight_decay_ti,
            )

        lr_scheduler = get_scheduler(
            lr_scheduler,
            optimizer=ti_optimizer,
            num_warmup_steps=lr_warmup_steps,
            num_training_steps=max_train_steps_ti,
        )

        train_inversion(
            unet,
            vae,
            text_encoder,
            train_dataloader,
            max_train_steps_ti,
            cached_latents=cached_latents,
            accum_iter=gradient_accumulation_steps,
            scheduler=noise_scheduler,
            index_no_updates=index_no_updates,
            optimizer=ti_optimizer,
            lr_scheduler=lr_scheduler,
            save_steps=save_steps,
            placeholder_tokens=placeholder_tokens,
            placeholder_token_ids=placeholder_token_ids,
            save_path=output_dir,
            test_image_path=instance_data_dir,
            log_wandb=log_wandb,
            wandb_log_prompt_cnt=wandb_log_prompt_cnt,
            class_token=class_token,
            train_inpainting=train_inpainting,
            mixed_precision=False,
            tokenizer=tokenizer,
            clip_ti_decay=clip_ti_decay,
            htcore=htcore,
            log_tb=log_tb,
            writer=writer,
            use_synapse_profiler=use_synapse_profiler,
            use_pytorch_profiler=use_pytorch_profiler,
            profiler_step=profiler_step,
            profile_ti=profile_ti,
            print_freq=print_freq,
            use_torch_compile=use_torch_compile,
        )

        del ti_optimizer

    # Next perform Tuning with LoRA:
    if not use_extended_lora:
        unet_lora_params, _ = inject_trainable_lora(
            unet,
            r=lora_rank,
            target_replace_module=lora_unet_target_modules,
            dropout_p=lora_dropout_p,
            scale=lora_scale,
        )
    else:
        print("PTI : USING EXTENDED UNET!!!")
        lora_unet_target_modules = (
            lora_unet_target_modules | UNET_EXTENDED_TARGET_REPLACE
        )
        print("PTI : Will replace modules: ", lora_unet_target_modules)

        unet_lora_params, _ = inject_trainable_lora_extended(
            unet, r=lora_rank, target_replace_module=lora_unet_target_modules
        )
    print(f"PTI : has {len(unet_lora_params)} lora")

    print("PTI : Before training:")
    inspect_lora(unet)

    params_to_optimize = [
        {"params": itertools.chain(*unet_lora_params), "lr": unet_lr},
    ]

    text_encoder.requires_grad_(False)

    if continue_inversion:
        params_to_optimize += [
            {
                "params": text_encoder.get_input_embeddings().parameters(), #.weight[~index_no_updates]],
                "lr": continue_inversion_lr
                if continue_inversion_lr is not None
                else ti_lr,
            }
        ]
        text_encoder.requires_grad_(True)
        params_to_freeze = itertools.chain(
            text_encoder.text_model.encoder.parameters(),
            text_encoder.text_model.final_layer_norm.parameters(),
            text_encoder.text_model.embeddings.position_embedding.parameters(),
        )
        for param in params_to_freeze:
            param.requires_grad = False
    else:
        text_encoder.requires_grad_(False)
    if train_text_encoder:
        text_encoder_lora_params, _ = inject_trainable_lora(
            text_encoder,
            target_replace_module=lora_clip_target_modules,
            r=lora_rank,
        )
        params_to_optimize += [
            {
                "params": itertools.chain(*text_encoder_lora_params),
                "lr": text_encoder_lr,
            }
        ]
        inspect_lora(text_encoder)

    lora_optimizers = None
    if use_fused_adamw and htcore:
        from habana_frameworks.torch.hpex.optimizers import FusedAdamW
        lora_optimizers = FusedAdamW(params_to_optimize, eps=1e-08, weight_decay=weight_decay_lora)
    else:
        lora_optimizers = optim.AdamW(params_to_optimize, weight_decay=weight_decay_lora)

    unet.train()
    if train_text_encoder:
        text_encoder.train()

    train_dataset.blur_amount = 70

    lr_scheduler_lora = get_scheduler(
        lr_scheduler_lora,
        optimizer=lora_optimizers,
        num_warmup_steps=lr_warmup_steps_lora,
        num_training_steps=max_train_steps_tuning,
    )

    perform_tuning(
        unet,
        vae,
        text_encoder,
        train_dataloader,
        max_train_steps_tuning,
        cached_latents=cached_latents,
        scheduler=noise_scheduler,
        optimizer=lora_optimizers,
        save_steps=save_steps,
        placeholder_tokens=placeholder_tokens,
        placeholder_token_ids=placeholder_token_ids,
        save_path=output_dir,
        lr_scheduler_lora=lr_scheduler_lora,
        lora_unet_target_modules=lora_unet_target_modules,
        lora_clip_target_modules=lora_clip_target_modules,
        mask_temperature=mask_temperature,
        tokenizer=tokenizer,
        out_name=out_name,
        test_image_path=instance_data_dir,
        log_wandb=log_wandb,
        wandb_log_prompt_cnt=wandb_log_prompt_cnt,
        class_token=class_token,
        train_inpainting=train_inpainting,
        htcore=htcore,
        log_tb=log_tb,
        writer=writer,
        use_synapse_profiler=use_synapse_profiler,
        use_pytorch_profiler=use_pytorch_profiler,
        profiler_step=profiler_step,
        profile_tuning=profile_tuning,
        print_freq=print_freq,
        use_fused_clip_norm=use_fused_clip_norm,
        use_torch_compile=use_torch_compile,
    )
    if writer:
        writer.close()


def main():
    fire.Fire(train)
