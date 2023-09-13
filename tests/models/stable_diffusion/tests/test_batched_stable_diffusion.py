# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

import torch
from tqdm.auto import tqdm
from loguru import logger
import pytest

from transformers import CLIPTextModel, CLIPTokenizer
from diffusers import (
    AutoencoderKL,
    UNet2DConditionModel,
    PNDMScheduler,
    HeunDiscreteScheduler,
    DPMSolverMultistepScheduler,
)
from diffusers import LMSDiscreteScheduler
from models.stable_diffusion.tt.unet_2d_condition import UNet2DConditionModel as tt_unet_condition
from models.stable_diffusion.tt.experimental_ops import UseDeviceConv

import tt_lib as ttl

from models.utility_functions import (
    torch_to_tt_tensor_rm,
    tt_to_torch_tensor,
    torch_to_tt_tensor,
)
from models.utility_functions import (
    comp_pcc,
    comp_allclose_and_pcc,
)


def constant_prop_time_embeddings(timesteps, sample, time_proj):
    timesteps = timesteps[None]
    timesteps = timesteps.expand(sample.shape[0])
    t_emb = time_proj(timesteps)
    return t_emb


def guide(noise_pred, guidance_scale, t):  # will return latents
    noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
    noise_pred = noise_pred_uncond + guidance_scale * (
        noise_pred_text - noise_pred_uncond
    )
    return noise_pred


def latent_expansion(latents, scheduler, t):
    latent_model_input = torch.cat([latents] * 2)
    latent_model_input = scheduler.scale_model_input(latent_model_input, timestep=t)
    return latent_model_input


def make_tt_unet(state_dict):
    tt_unet = tt_unet_condition(
        sample_size=64,
        in_channels=4,
        out_channels=4,
        center_input_sample=False,
        flip_sin_to_cos=True,
        freq_shift=0,
        down_block_types=[
            "CrossAttnDownBlock2D",
            "CrossAttnDownBlock2D",
            "CrossAttnDownBlock2D",
            "DownBlock2D",
        ],
        mid_block_type="UNetMidBlock2DCrossAttn",
        up_block_types=[
            "UpBlock2D",
            "CrossAttnUpBlock2D",
            "CrossAttnUpBlock2D",
            "CrossAttnUpBlock2D",
        ],
        only_cross_attention=False,
        block_out_channels=[320, 640, 1280, 1280],
        layers_per_block=2,
        downsample_padding=1,
        mid_block_scale_factor=1,
        act_fn="silu",
        norm_num_groups=32,
        norm_eps=1e-05,
        cross_attention_dim=768,
        attention_head_dim=8,
        dual_cross_attention=False,
        use_linear_projection=False,
        class_embed_type=None,
        num_class_embeds=None,
        upcast_attention=False,
        resnet_time_scale_shift="default",
        state_dict=state_dict,
        base_address="",
    )
    return tt_unet

def test_batched_stable_diffusion():
    # Initialize the device
    device = ttl.device.CreateDevice(ttl.device.Arch.GRAYSKULL, 0)
    ttl.device.InitializeDevice(device)
    ttl.device.SetDefaultDevice(device)

    # 1. Load the autoencoder model which will be used to decode the latents into image space.
    vae = AutoencoderKL.from_pretrained(
        "CompVis/stable-diffusion-v1-4", subfolder="vae"
    )

    # 2. Load the tokenizer and text encoder to tokenize and encode the text.
    tokenizer = CLIPTokenizer.from_pretrained("openai/clip-vit-large-patch14")
    text_encoder = CLIPTextModel.from_pretrained("openai/clip-vit-large-patch14")

    # 3. The UNet model for generating the latents.
    unet = UNet2DConditionModel.from_pretrained(
        "CompVis/stable-diffusion-v1-4", subfolder="unet"
    )

    # 4. load the K-LMS scheduler with some fitting parameters.
    scheduler = LMSDiscreteScheduler(
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear",
        num_train_timesteps=1000,
    )
    tt_scheduler = LMSDiscreteScheduler(
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear",
        num_train_timesteps=1000,
    )
    # scheduler = PNDMScheduler(beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear", num_train_timesteps=1000)
    # scheduler = HeunDiscreteScheduler(beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear", num_train_timesteps=1000)
    # scheduler = DPMSolverMultistepScheduler(beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear", num_train_timesteps=1000)

    torch_device = "cpu"
    vae.to(torch_device)
    text_encoder.to(torch_device)
    unet.to(torch_device)

    state_dict = unet.state_dict()
    tt_unet = make_tt_unet(state_dict)
    tt_unet.config = unet.config

    experiment_name = "mountain_fallback_nolatentupdate"
    prompt = [
        "oil painting frame of Breathtaking mountain range with a clear river running through it, surrounded by tall trees and misty clouds, serene, peaceful, mountain landscape, high detail"
    ]

    height = 256  # default height of Stable Diffusion
    width = 256  # default width of Stable Diffusion
    num_inference_steps = 1  # Number of denoising steps
    guidance_scale = 7.5  # Scale for classifier-free guidance
    generator = torch.manual_seed(
        174
    )  # 10233 Seed generator to create the inital latent noise
    batch_size = len(prompt)

    ## First, we get the text_embeddings for the prompt. These embeddings will be used to condition the UNet model.
    # Tokenizer and Text Encoder
    text_input = tokenizer(
        prompt,
        padding="max_length",
        max_length=tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_embeddings = text_encoder(text_input.input_ids.to(torch_device))[0]
    max_length = text_input.input_ids.shape[-1]
    uncond_input = tokenizer(
        [""] * batch_size,
        padding="max_length",
        max_length=max_length,
        return_tensors="pt",
    )
    uncond_embeddings = text_encoder(uncond_input.input_ids.to(torch_device))[0]

    # For classifier-free guidance, we need to do two forward passes: one with the conditioned input (text_embeddings),
    # and another with the unconditional embeddings (uncond_embeddings).
    # In practice, we can concatenate both into a single batch to avoid doing two forward passes.
    text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

    # Initial random noise
    latents = torch.randn(
        (batch_size, unet.config.in_channels, height // 8, width // 8),
        generator=generator,
    )
    latents = latents.to(torch_device)

    scheduler.set_timesteps(num_inference_steps)
    tt_scheduler.set_timesteps(num_inference_steps)
    latents = latents * scheduler.init_noise_sigma
    tt_latents = torch.tensor(latents)

    iter = 0

    # torch Denoising loop
    for t in tqdm(scheduler.timesteps):
        # expand the latents if we are doing classifier-free guidance to avoid doing two forward passes.
        latent_model_input = latent_expansion(latents, scheduler, t)
        # predict the noise residual
        with torch.no_grad():
            noise_pred = unet(
                latent_model_input, t, encoder_hidden_states=text_embeddings
            ).sample
        # perform guidance
        noise_pred = guide(noise_pred, guidance_scale, t)
        # compute the previous noisy sample x_t -> x_t-1
        if UseDeviceConv.READY:
            # force unpad noise_pred
            noise_pred = noise_pred[:, :4, :, :]
        latents = scheduler.step(noise_pred, t, latents).prev_sample
        # We need only one iteration
        break

    iter = 0
    last_latents = None
    # # Denoising loop
    for t in tqdm(tt_scheduler.timesteps):
        # expand the latents if we are doing classifier-free guidance to avoid doing two forward passes.
        tt_latent_model_input = latent_expansion(tt_latents, tt_scheduler, t)

        _t = constant_prop_time_embeddings(t, tt_latent_model_input, unet.time_proj)

        _t = torch_to_tt_tensor_rm(_t, device, put_on_device=False)
        tt_latent_model_input = torch_to_tt_tensor_rm(
            tt_latent_model_input, device, put_on_device=False
        )
        tt_text_embeddings = torch_to_tt_tensor_rm(
            text_embeddings, device, put_on_device=False
        )

        # predict the noise residual
        with torch.no_grad():
            tt_noise_pred = tt_unet(
                tt_latent_model_input, _t, encoder_hidden_states=tt_text_embeddings
            )
            ttl.device.Synchronize()
            noise_pred = tt_to_torch_tensor(tt_noise_pred)

        # perform guidance
        noise_pred = guide(noise_pred, guidance_scale, t)

        # compute the previous noisy sample x_t -> x_t-1
        tt_latents = tt_scheduler.step(noise_pred, t, tt_latents).prev_sample

        # We need only one iteration
        break

    does_pass, pcc_message = comp_pcc(latents, tt_latents, pcc=0.99)
    pcc_res = comp_allclose_and_pcc(latents, tt_latents)
    logger.info(pcc_res)

    ttl.device.CloseDevice(device)

    if does_pass:
        logger.info("Batched Stable Diffusion Passed!")
    else:
        logger.warning("Batched Stable Diffusion Failed!")

    assert does_pass
