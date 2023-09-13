# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import sys
import torch
import torch.nn as nn
import pytest
import timm
from loguru import logger

f = f"{Path(__file__).parent}"
sys.path.append(f"{f}/../../../..")

from models.utility_functions import (
    torch_to_tt_tensor_rm,
    tt_to_torch_tensor,
    comp_allclose,
    comp_pcc,
)
import tt_lib
from tests.models.vovnet.tt.osa_stage import TtOsaStage


@pytest.mark.parametrize(
    "pcc",
    ((0.99),),
)
def test_osa_stage_inference(pcc, reset_seeds):
    device = tt_lib.device.CreateDevice(tt_lib.device.Arch.GRAYSKULL, 0)
    tt_lib.device.InitializeDevice(device)
    tt_lib.device.SetDefaultDevice(device)


    STAGE_INDEX = 0

    base_address = f"stages.{STAGE_INDEX}"
    model = timm.create_model("hf_hub:timm/ese_vovnet19b_dw.ra_in1k", pretrained=True)

    torch_model = model.stages[STAGE_INDEX]

    tt_model = TtOsaStage(
        in_chs=1,
        mid_chs=128,
        out_chs=256,
        layer_per_block=3,
        residual=False,
        depthwise=True,
        base_address=base_address,
        state_dict=model.state_dict(),
        host=host,
        device=device,
        downsample=False,
    )

    # run torch model
    input = torch.randn(1, 64, 56, 56)
    model_output = torch_model(input)

    # run tt model
    tt_input = torch_to_tt_tensor_rm(input, device)
    tt_output = tt_model(tt_input)
    tt_output_torch = tt_to_torch_tensor(tt_output)

    # compare output
    passing, pcc_message = comp_pcc(model_output, tt_output_torch, pcc)

    logger.info(comp_allclose(model_output, tt_output_torch))
    logger.info(pcc_message)

    tt_lib.device.CloseDevice(device)
    if passing:
        logger.info("OsaStage Passed!")
    else:
        logger.warning("OsaStage Failed!")

    assert passing
