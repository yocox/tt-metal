# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

import pytest
from loguru import logger
from pathlib import Path
import sys

f = f"{Path(__file__).parent}"
sys.path.append(f"{f}/..")
sys.path.append(f"{f}/../..")
sys.path.append(f"{f}/../../..")
sys.path.append(f"{f}/../../../..")

import torch
from transformers import BertForQuestionAnswering
import tt_lib as ttl
from tt_lib.utils import pad_activation, pad_weight, print_diff_argmax
from models.utility_functions import comp_pcc, comp_allclose

from tests.models.metal_BERT_large_15.model_config import get_model_config


def feed_forward(
    ffn_dim,
    hidden_dim,
    ff1_weighta,
    ff1_biasa,
    ff2_weighta,
    ff2_biasa,
    device,
    model_config,
):
    # Weights pre-transposed on host​. No on-the fly transpose of W.
    # ff1_weighta = ttl.tensor.transpose(ff1_weighta)
    # ff2_weighta = ttl.tensor.transpose(ff2_weighta)

    # activation = [1, 9, 384, 1024]
    # ff1_weighta = [1, 1, 1024, 4096]
    # output = [1, 9, 384, 4096]
    def op13_MM_bias_gelu(activation, ff1_weighta, ff1_biasa):
        # profiler.start("___op13_MM_bias_gelu")
        output_plus_bias_act = ttl.tensor.bert_large_ff1_matmul(
            activation,
            ff1_weighta,
            ff1_biasa,
            (ttl.tensor.FusibleActivation.GELU, True),
            output_mem_config=model_config["OP13_FF1_MM_OUTPUT_MEMCFG"],
            output_dtype=model_config["OP13_FF1_MM_OUTPUT_DTYPE"],
        )
        # profiler.end("___op13_MM_bias_gelu")

        return output_plus_bias_act

    # activation = [1, 9, 384, 4096]
    # ff2_weighta = [1, 1, 4096, 1024]
    # output = [1, 9, 384, 1024]
    def op14_MM_bias(activation, ff2_weighta, ff2_biasa):
        # profiler.start("___op14_MM_bias")
        output_plus_bias = ttl.tensor.bert_large_ff2_matmul(
            activation,
            ff2_weighta,
            ff2_biasa,
            output_mem_config=model_config["OP14_FF2_MM_OUTPUT_MEMCFG"],
            output_dtype=model_config["OP14_FF2_MM_OUTPUT_DTYPE"],
        )
        # profiler.end("___op14_MM_bias")

        return output_plus_bias

    def feed_forward_(activation):
        # profiler.start("__ffn")
        ff1_output_plus_bias_act = op13_MM_bias_gelu(activation, ff1_weighta, ff1_biasa)
        # Don't deallocate activations here since it is used by more ops in encoder
        # activation.deallocate()
        ff2_output_plus_bias = op14_MM_bias(
            ff1_output_plus_bias_act, ff2_weighta, ff2_biasa
        )
        ff1_output_plus_bias_act.deallocate()
        # profiler.end("__ffn")

        return ff2_output_plus_bias

    return feed_forward_


class TtFeedForwardModel(torch.nn.Module):
    def __init__(self, encoder_idx, state_dict, device, model_config):
        super().__init__()

        # FF1 params
        encoder0_ff1_weight = pad_weight(
            torch.transpose(
                state_dict[
                    f"bert.encoder.layer.{encoder_idx}.intermediate.dense.weight"
                ],
                -2,
                -1,
            )
        )
        encoder0_ff1_bias = pad_weight(
            state_dict[f"bert.encoder.layer.{encoder_idx}.intermediate.dense.bias"]
        )

        encoder0_ff1_weight_shape = encoder0_ff1_weight.shape
        encoder0_ff1_bias_shape = encoder0_ff1_bias.shape

        encoder0_ff1_weight = (
            ttl.tensor.Tensor(
                encoder0_ff1_weight.reshape(-1).tolist(),
                encoder0_ff1_weight.shape,
                model_config["OP13_FF1_MM_WEIGHTS_DTYPE"],
                ttl.tensor.Layout.ROW_MAJOR,
            )
            .to(ttl.tensor.Layout.TILE)
            .to(device, model_config["OP13_FF1_MM_WEIGHTS_MEMCFG"])
        )
        encoder0_ff1_bias = (
            ttl.tensor.Tensor(
                encoder0_ff1_bias.reshape(-1).tolist(),
                encoder0_ff1_bias.shape,
                model_config["OP13_FF1_MM_BIAS_DTYPE"],
                ttl.tensor.Layout.ROW_MAJOR,
            )
            .to(ttl.tensor.Layout.TILE)
            .to(device, model_config["OP13_FF1_MM_BIAS_MEMCFG"])
        )

        # FF2 params
        encoder0_ff2_weight = pad_weight(
            torch.transpose(
                state_dict[f"bert.encoder.layer.{encoder_idx}.output.dense.weight"],
                -2,
                -1,
            )
        )
        encoder0_ff2_bias = pad_weight(
            state_dict[f"bert.encoder.layer.{encoder_idx}.output.dense.bias"]
        )

        encoder0_ff2_weight_shape = encoder0_ff2_weight.shape
        encoder0_ff2_bias_shape = encoder0_ff2_bias.shape

        encoder0_ff2_weight = (
            ttl.tensor.Tensor(
                encoder0_ff2_weight.reshape(-1).tolist(),
                encoder0_ff2_weight.shape,
                model_config["OP14_FF2_MM_WEIGHTS_DTYPE"],
                ttl.tensor.Layout.ROW_MAJOR,
            )
            .to(ttl.tensor.Layout.TILE)
            .to(device, model_config["OP14_FF2_MM_WEIGHTS_MEMCFG"])
        )
        encoder0_ff2_bias = (
            ttl.tensor.Tensor(
                encoder0_ff2_bias.reshape(-1).tolist(),
                encoder0_ff2_bias.shape,
                model_config["OP14_FF2_MM_BIAS_DTYPE"],
                ttl.tensor.Layout.ROW_MAJOR,
            )
            .to(ttl.tensor.Layout.TILE)
            .to(device, model_config["OP14_FF2_MM_BIAS_MEMCFG"])
        )

        self.ffn = feed_forward(
            *encoder0_ff1_weight_shape[-2:],
            encoder0_ff1_weight,
            encoder0_ff1_bias,
            encoder0_ff2_weight,
            encoder0_ff2_bias,
            device,
            model_config,
        )

    def forward(self, activation):
        return self.ffn(activation)


class PytorchFeedForwardModel(torch.nn.Module):
    def __init__(self, hugging_face_reference_model):
        super().__init__()
        self.ff1 = hugging_face_reference_model.bert.encoder.layer[0].intermediate
        self.ff2 = hugging_face_reference_model.bert.encoder.layer[0].output.dense

    def forward(self, x):
        return self.ff2(self.ff1(x))


def summarize_stats(t, name):
    mean = t.mean()
    std = t.std()
    mag = t.norm()
    max = t.max()
    print(f"STATS FOR {name}")
    print(f"mean {mean}")
    print(f"std {std}")
    print(f"mag {mag}")
    print(f"max {max}")
    print()


def run_ffn_inference(
    model_version, batch, seq_len, pcc, model_config, model_location_generator
):
    device = ttl.device.CreateDevice(ttl.device.Arch.GRAYSKULL, 0)
    # Initialize the device
    ttl.device.InitializeDevice(device)

    model_name = str(model_location_generator(model_version, model_subdir = "Bert"))

    hugging_face_reference_model = BertForQuestionAnswering.from_pretrained(
        model_name, torchscript=False
    )
    tt_ffn_model = TtFeedForwardModel(
        0, hugging_face_reference_model.state_dict(), device, model_config
    )
    pytorch_ffn_model = PytorchFeedForwardModel(hugging_face_reference_model)

    # Prepare input
    torch.manual_seed(0)
    ffn_input = (
        torch.rand(batch, 1, seq_len, hugging_face_reference_model.config.hidden_size)
        * 2
    ) - 1

    pytorch_out = pytorch_ffn_model(ffn_input)

    pad_ffn_input = pad_activation(ffn_input)
    tilized_ffn_input = ttl.tensor.Tensor(
        pad_ffn_input.reshape(-1).tolist(),
        pad_ffn_input.shape,
        model_config["OP12_LAYERNORM_OUTPUT_DTYPE"],
        ttl.tensor.Layout.ROW_MAJOR,
    ).to(ttl.tensor.Layout.TILE)
    tilized_ffn_input = tilized_ffn_input.to(
        device, model_config["OP12_LAYERNORM_OUTPUT_MEMCFG"]
    )

    tt_out = tt_ffn_model(tilized_ffn_input).cpu()
    tt_out = tt_out.to(ttl.tensor.Layout.ROW_MAJOR).to_torch().reshape(
        tt_out.shape()
    )

    ttl.device.CloseDevice(device)

    passing, output = comp_pcc(pytorch_out, tt_out, pcc)
    logger.info(f"Output {output}")

    _, output = comp_allclose(
        pytorch_out, tt_out, 0.5, 0.5
    )  # Only interested in reporting atol/rtol, using PCC for pass/fail
    logger.info(f"Output {output}")

    if not passing:
        logger.error(f"Output PCC < {pcc}")

    assert passing


@pytest.mark.parametrize(
    "batch, model_config_str",
    (
        (9, "BFLOAT8_B-DRAM"),
        (9, "BFLOAT16-DRAM"),
        (9, "BFLOAT8_B-L1"),
        (9, "BFLOAT16-L1"),
        (9, "MIXED_PRECISION_BATCH9"),
        (8, "MIXED_PRECISION_BATCH8"),
    ),
    ids=[
        "batch_9-BFLOAT8_B-DRAM",
        "batch_9-BFLOAT16-DRAM",
        "batch_9-BFLOAT8_B-L1",
        "batch_9-BFLOAT16-L1",
        "batch_9-MIXED_PRECISION_BATCH9",
        "batch_8-MIXED_PRECISION_BATCH8",
    ],
)
@pytest.mark.parametrize(
    "model_version, seq_len, pcc",
    (("phiyodr/bert-large-finetuned-squad2", 384, 0.99),),
    ids=["BERT_LARGE"],
)
def test_ffn_inference(
    model_version,
    batch,
    seq_len,
    pcc,
    model_config_str,
    model_location_generator,
    request,
):
    model_config = get_model_config(model_config_str)

    ttl.profiler.set_profiler_location(
        f"tt_metal/tools/profiler/logs/BERT_large_ffn_{request.node.callspec.id}"
    )
    run_ffn_inference(
        model_version,
        batch,
        seq_len,
        pcc,
        model_config,
        model_location_generator,
    )
