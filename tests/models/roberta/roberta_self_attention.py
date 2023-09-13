# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

import math
from pathlib import Path
import sys
import random
from typing import Optional, Tuple, Union
import torch
import torch.nn as nn
import numpy as np
from loguru import logger

f = f"{Path(__file__).parent}"
sys.path.append(f"{f}/..")
sys.path.append(f"{f}/../..")
sys.path.append(f"{f}/../../..")
sys.path.append(f"{f}/../../../..")

from tests.models.roberta.roberta_common import (
    torch2tt_tensor,
    tt2torch_tensor,
)
from models.helper_funcs import Linear as TTLinear
from models.utility_functions import pad_by_zero
import tt_lib
from tt_lib.fallback_ops import fallback_ops

class TtRobertaSelfAttention(nn.Module):
    def __init__(
        self, config, state_dict, base_address, device, position_embedding_type=None
    ):
        super().__init__()
        if config.hidden_size % config.num_attention_heads != 0 and not hasattr(
            config, "embedding_size"
        ):
            raise ValueError(
                f"The hidden size ({config.hidden_size}) is not a multiple of the number of attention "
                f"heads ({config.num_attention_heads})"
            )
        self.device = device
        self.mem_config = tt_lib.tensor.MemoryConfig(True, tt_lib.tensor.BufferType.L1)

        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query_weight = pad_by_zero(
            state_dict[f"{base_address}.query.weight"], self.device
        )[0]
        self.query_bias = pad_by_zero(
            state_dict[f"{base_address}.query.bias"], self.device
        )[0]

        self.key_weight = pad_by_zero(
            state_dict[f"{base_address}.key.weight"], self.device
        )[0]
        self.key_bias = pad_by_zero(
            state_dict[f"{base_address}.key.bias"], self.device
        )[0]

        self.value_weight = pad_by_zero(
            state_dict[f"{base_address}.value.weight"], self.device
        )[0]
        self.value_bias = pad_by_zero(
            state_dict[f"{base_address}.value.bias"], self.device
        )[0]

        # TODO: Add dropout when supported
        # self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

        self.position_embedding_type = position_embedding_type or getattr(
            config, "position_embedding_type", "absolute"
        )
        if (
            self.position_embedding_type == "relative_key"
            or self.position_embedding_type == "relative_key_query"
        ):
            self.max_position_embeddings = config.max_position_embeddings
            self.distance_embedding = nn.Embedding(
                2 * config.max_position_embeddings - 1, self.attention_head_size
            )

        self.is_decoder = config.is_decoder

        self.query_linear = TTLinear(
            self.query_weight.shape()[-1],
            self.query_weight.shape()[-2],
            self.query_weight,
            self.query_bias,
        )
        self.key_linear = TTLinear(
            self.key_weight.shape()[-1],
            self.key_weight.shape()[-2],
            self.key_weight,
            self.key_bias,
        )
        self.value_linear = TTLinear(
            self.value_weight.shape()[-1],
            self.value_weight.shape()[-2],
            self.value_weight,
            self.value_bias,
        )

    def transpose_for_scores(self, x: tt_lib.tensor.Tensor) -> tt_lib.tensor.Tensor:
        # x must be 4d originaly
        # 1 is appended to the beggining
        # so create tensor shape by ommiting the first dimension
        new_x_shape = list(x.shape()[1:-1]) + [
            self.num_attention_heads,
            self.attention_head_size,
        ]
        x = fallback_ops.reshape(x, *new_x_shape)
        x = tt_lib.tensor.permute(x, 0, 2, 1, 3)
        return x

    def linear(self, x, weight, bias):
        weight = tt_lib.tensor.transpose(weight)
        x = tt_lib.tensor.matmul(x, weight, output_mem_config = self.mem_config)
        x = tt_lib.tensor.bcast(
            x, bias, tt_lib.tensor.BcastOpMath.ADD, tt_lib.tensor.BcastOpDim.H, output_mem_config = self.mem_config
        )
        return x

    def forward(
        self,
        hidden_states: tt_lib.tensor.Tensor,
        attention_mask: Optional[tt_lib.tensor.Tensor] = None,
        head_mask: Optional[tt_lib.tensor.Tensor] = None,
        encoder_hidden_states: Optional[tt_lib.tensor.Tensor] = None,
        encoder_attention_mask: Optional[tt_lib.tensor.Tensor] = None,
        past_key_value: Optional[Tuple[Tuple[tt_lib.tensor.Tensor]]] = None,
        output_attentions: Optional[bool] = False,
    ) -> Tuple[tt_lib.tensor.Tensor]:
        mixed_query_layer = self.query_linear(hidden_states)

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        is_cross_attention = encoder_hidden_states is not None

        if is_cross_attention and past_key_value is not None:
            # reuse k,v, cross_attentions
            key_layer = past_key_value[0]
            value_layer = past_key_value[1]
            attention_mask = encoder_attention_mask
        elif is_cross_attention:
            key_layer = self.transpose_for_scores(
                self.key_linear(encoder_hidden_states)
            )
            value_layer = self.transpose_for_scores(
                self.value_linear(encoder_hidden_states)
            )
            attention_mask = encoder_attention_mask
        elif past_key_value is not None:
            key_layer = self.transpose_for_scores(self.key_linear(hidden_states))
            value_layer = self.transpose_for_scores(self.value_linear(hidden_states))
            key_layer = fallback_ops.concat([past_key_value[0], key_layer], dim=2)
            value_layer = fallback_ops.concat([past_key_value[1], value_layer], dim=2)
        else:
            key_layer = self.transpose_for_scores(self.key_linear(hidden_states))
            value_layer = self.transpose_for_scores(self.value_linear(hidden_states))

        query_layer = self.transpose_for_scores(mixed_query_layer)

        use_cache = past_key_value is not None
        if self.is_decoder:
            # if cross_attention save Tuple(torch.Tensor, torch.Tensor) of all cross attention key/value_states.
            # Further calls to cross_attention layer can then reuse all cross-attention
            # key/value_states (first "if" case)
            # if uni-directional self-attention (decoder) save Tuple(torch.Tensor, torch.Tensor) of
            # all previous decoder key/value_states. Further calls to uni-directional self-attention
            # can concat previous decoder key/value_states to current projected key/value_states (third "elif" case)
            # if encoder bi-directional self-attention `past_key_value` is always `None`
            past_key_value = (key_layer, value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        key_layer_transposed = tt_lib.tensor.transpose(key_layer)

        attention_scores = tt_lib.tensor.bmm(query_layer, key_layer_transposed, output_mem_config = self.mem_config)

        if (
            self.position_embedding_type == "relative_key"
            or self.position_embedding_type == "relative_key_query"
        ):
            """
            TODO: This block is in pytorch and currently not used ,
            bc model config self.position_embedding_type = absolute
            Implemented in torch bc of missing ops and embeddings.
            """
            query_length, key_length = query_layer.shape()[-1], key_layer.shape()[-1]

            torch_query_layer = tt2torch_tensor(query_layer)
            torch_key_layer = tt2torch_tensor(key_layer)

            if use_cache:
                position_ids_l = torch.tensor(
                    key_length - 1, dtype=torch.long, device=hidden_states.device
                ).view(-1, 1)
            else:
                position_ids_l = torch.arange(
                    query_length, dtype=torch.long, device=hidden_states.device
                ).view(-1, 1)
            position_ids_r = torch.arange(
                key_length, dtype=torch.long, device=hidden_states.device
            ).view(1, -1)
            distance = position_ids_l - position_ids_r

            positional_embedding = self.distance_embedding(
                distance + self.max_position_embeddings - 1
            )
            positional_embedding = positional_embedding.to(
                dtype=torch_query_layer.dtype
            )  # fp16 compatibility

            if self.position_embedding_type == "relative_key":
                relative_position_scores = torch.einsum(
                    "bhld,lrd->bhlr", torch_query_layer, positional_embedding
                )
                attention_scores = attention_scores + relative_position_scores
            elif self.position_embedding_type == "relative_key_query":
                relative_position_scores_query = torch.einsum(
                    "bhld,lrd->bhlr", torch_query_layer, positional_embedding
                )
                relative_position_scores_key = torch.einsum(
                    "bhrd,lrd->bhlr", torch_key_layer, positional_embedding
                )
                attention_scores = (
                    attention_scores
                    + relative_position_scores_query
                    + relative_position_scores_key
                )
            # back to tt
            attention_scores = torch2tt_tensor(attention_scores, self.device)

        div_const = tt_lib.tensor.full(
            attention_scores.shape(),
            1.0 / math.sqrt(self.attention_head_size),
        )
        attention_scores = tt_lib.tensor.mul(attention_scores, div_const, output_mem_config = self.mem_config)

        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in RobertaModel forward() function)
            # attention_scores = tt_lib.tensor.add(attention_scores, attention_mask)
            if attention_mask.shape()[0] > 1:
                torch_attention_mask = tt2torch_tensor(attention_mask)
                torch_attention_scores = tt2torch_tensor(attention_scores)
                torch_attention_scores = torch_attention_scores + torch_attention_mask
                attention_scores = torch2tt_tensor(torch_attention_scores, self.device)
            else:
                tt_lib.tensor.bcast(
                    attention_scores,
                    attention_mask,
                    tt_lib.tensor.BcastOpMath.ADD,
                    tt_lib.tensor.BcastOpDim.H,
                    self.mem_config
                )
        # Normalize the attention scores to probabilities.

        # Fallback softmax drops PCC a bit from 0.9999 to 0.998
        # Device softmax only support tile shape/layout
        attention_probs = fallback_ops.softmax(attention_scores, dim=-1)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.

        # TODO add when training is supported
        # attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = tt_lib.tensor.mul(attention_probs, head_mask, output_mem_config = self.mem_config)

        context_layer = tt_lib.tensor.bmm(attention_probs, value_layer, self.mem_config)
        context_layer = tt_lib.tensor.permute(context_layer, 0, 2, 1, 3)

        # TODO left here. Finish porting and re-test everything. See other TODO s
        # context_layer = context_layer.permute(0, 2, 1, 3). contiguous() TODO: CHECK contiguous

        new_context_layer_shape = (
            [
                1,
            ]
            + context_layer.shape()[:-2]
            + [
                self.all_head_size,
            ]
        )
        context_layer = fallback_ops.reshape(context_layer, *new_context_layer_shape)

        outputs = (
            (context_layer, attention_probs) if output_attentions else (context_layer,)
        )

        if self.is_decoder:
            outputs = outputs + (past_key_value,)
        return outputs
