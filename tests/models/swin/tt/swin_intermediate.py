# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

from typing import Optional, Tuple, Union
import torch
import torch.nn as nn

from models.utility_functions import (
    tt_to_torch_tensor,
    torch_to_tt_tensor_rm,
)
from tests.models.swin.swin_helper_funcs import linear as TtLinear
import tt_lib


class TtSwinIntermediate(nn.Module):
    def __init__(
        self,
        config,
        dim,
        state_dict,
        base_address,
        device,
        host,
        fall_back_to_torch_gelu=False,
    ):
        super().__init__()
        self.device = device
        self.host = host
        self.fall_back_to_torch_gelu = fall_back_to_torch_gelu

        self.dense_weight = torch_to_tt_tensor_rm(
            state_dict[f"{base_address}.dense.weight"], self.device
        )
        self.dense_bias = torch_to_tt_tensor_rm(
            state_dict[f"{base_address}.dense.bias"], self.device
        )

    def forward(self, hidden_states: tt_lib.tensor.Tensor) -> tt_lib.tensor.Tensor:
        hidden_states = TtLinear(hidden_states, self.dense_weight, self.dense_bias)
        if self.fall_back_to_torch_gelu:
            torch_hidden_states = tt_to_torch_tensor(hidden_states)
            torch_hidden_states = torch.nn.functional.gelu(torch_hidden_states)
            hidden_states = torch_to_tt_tensor_rm(torch_hidden_states, self.device)
        else:
            hidden_states = tt_lib.tensor.gelu(hidden_states)
        return hidden_states
