import sys
import pytest
from pathlib import Path
from loguru import logger

f = f"{Path(__file__).parent}"
sys.path.append(f"{f}/../..")


import tt_lib as ttl
from python_api_testing.models.utility_functions import (
    comp_pcc,
)
import torch


def run_move_op(test_id, shape, dtype, in0_mem_config, output_mem_config):
    """
    For overlap, every test case should (for now) default to single-core.
    For non_overlap, multi-core is run for num_tiles > 1.
    """
    torch.manual_seed(1234)
    # Initialize the device
    device = ttl.device.CreateDevice(ttl.device.Arch.GRAYSKULL, 0)
    ttl.device.InitializeDevice(device)

    # Dummy tensor to shift input tensor in memory
    if test_id == 0:
        dummy_shape = [1, 1, 32, 32]
    elif test_id == 1:
        dummy_shape = shape  # This will allow output and input buffers to not overlap
    else:
        raise NotImplementedError(f"Unknown test id: {test_id}!")

    dummy_tensor = torch.randn(dummy_shape)
    tt_dummy_tensor = (
        ttl.tensor.Tensor(
            dummy_tensor.flatten().tolist(),
            dummy_shape,
            dtype,
            ttl.tensor.Layout.ROW_MAJOR,
        )
        .to(ttl.tensor.Layout.TILE)
        .to(device, in0_mem_config)
    )

    torch_tensor = torch.randn(shape)
    tt_tensor = (
        ttl.tensor.Tensor(
            torch_tensor.flatten().tolist(),
            shape,
            dtype,
            ttl.tensor.Layout.ROW_MAJOR,
        )
        .to(ttl.tensor.Layout.TILE)
        .to(device, in0_mem_config)
    )

    # Free up dummy tensor from memory to make available to move
    tt_dummy_tensor.deallocate()

    output = ttl.tensor.move(tt_tensor, output_mem_config)

    tt_host_rm = output.cpu().to(ttl.tensor.Layout.ROW_MAJOR)
    pyt_got_back_rm = torch.Tensor(tt_host_rm.data()).reshape(tt_host_rm.shape())

    passing_pcc, output_pcc = comp_pcc(pyt_got_back_rm, torch_tensor, 0.99)
    logger.info(f"Passing={passing_pcc}")
    logger.info(f"Output pcc={output_pcc}")

    ttl.device.CloseDevice(device)
    assert passing_pcc


@pytest.mark.parametrize(
    "in0_mem_config, output_mem_config",
    (
        (
            ttl.tensor.MemoryConfig(True, ttl.tensor.BufferType.DRAM),
            ttl.tensor.MemoryConfig(True, ttl.tensor.BufferType.DRAM),
        ),
        (
            ttl.tensor.MemoryConfig(True, ttl.tensor.BufferType.L1),
            ttl.tensor.MemoryConfig(True, ttl.tensor.BufferType.L1),
        ),
    ),
    ids=["DRAM", "L1"],
)
@pytest.mark.parametrize(
    "dtype",
    (ttl.tensor.DataType.BFLOAT8_B, ttl.tensor.DataType.BFLOAT16),
    ids=["BFLOAT8_B", "BFLOAT16"],
)
@pytest.mark.parametrize("shape", ([1, 1, 32, 32], [1, 3, 320, 384]))
@pytest.mark.parametrize("test_id", (0, 1), ids=["overlap", "non_overlap"])
def test_move_op(test_id, shape, dtype, in0_mem_config, output_mem_config):
    run_move_op(test_id, shape, dtype, in0_mem_config, output_mem_config)


def test_move_op_with_program_cache(use_program_cache):
    in0_mem_config = ttl.tensor.MemoryConfig(True, ttl.tensor.BufferType.L1)
    output_mem_config = ttl.tensor.MemoryConfig(True, ttl.tensor.BufferType.L1)
    dtype = ttl.tensor.DataType.BFLOAT16
    shape = [1, 3, 320, 384]

    # Single core because of overlap
    for _ in range(2):
        run_move_op(0, shape, dtype, in0_mem_config, output_mem_config)

    # Multi-core
    for _ in range(2):
        run_move_op(1, shape, dtype, in0_mem_config, output_mem_config)

    assert ttl.program_cache.num_entries() == 2
