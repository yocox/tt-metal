# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

import torch.nn as nn
from typing import (
    List,
)
import tt_lib
import tt_lib.fallback_ops as fallback_ops

from models.ssd.tt.ssd_mobilenetv3_convlayer import (
    TtMobileNetV3ConvLayer,
)

from models.utility_functions import torch_to_tt_tensor_rm, tt_to_torch_tensor


class TtSSDclassificationhead(nn.Module):
    def __init__(
        self,
        config,
        in_channels: List[int],
        num_classes: int,
        state_dict=None,
        base_address="",
        device=None,
    ):
        super().__init__()
        self.channels = in_channels
        self.num_classes = num_classes
        self.device = device
        self.classification_head = nn.ModuleList()
        for i in range(6):
            self.conv = TtMobileNetV3ConvLayer(
                config,
                in_channels=self.channels[i],
                out_channels=self.channels[i],
                kernel_size=3,
                stride=1,
                padding=1,
                groups=self.channels[i],
                use_activation=True,
                state_dict=state_dict,
                base_address=f"{base_address}.{i}.{'0'}",
                device=device,
            )
            self.classification_head.append(self.conv)
            weight = torch_to_tt_tensor_rm(
                state_dict[f"{base_address}.{i}.1.weight"], device, put_on_device=False
            )

            self.convolution = fallback_ops.Conv2d(
                weights=weight,
                biases=None,
                in_channels=self.channels[i],
                out_channels=546,
                kernel_size=1,
                stride=1,
            )
            self.classification_head.append(self.convolution)

    def _get_result_from_module_list(self, x: tt_lib.tensor.Tensor, idx: int):
        out = x
        for i, module in enumerate(self.classification_head):
            if i == idx:
                out = module(x)
            elif i == idx + 1:
                out = module(out)

        return out

    def forward(self, x: List[tt_lib.tensor.Tensor]) -> tt_lib.tensor.Tensor:
        all_results = []
        for i in range(len(x)):
            result = self._get_result_from_module_list(x[i], i * 2)
            result = tt_to_torch_tensor(result)
            N, _, H, W = result.shape
            result = result.view(N, -1, self.num_classes, H, W)
            result = result.permute(0, 3, 4, 1, 2)
            result = result.reshape(N, -1, self.num_classes)

            result = torch_to_tt_tensor_rm(
                result, device=self.device, put_on_device=False
            )
            all_results.append(result)

        return fallback_ops.concat(all_results, 2)
