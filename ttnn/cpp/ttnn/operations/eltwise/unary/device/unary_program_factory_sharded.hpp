// SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.
//
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <algorithm>

#include "tt_dnn/op_library/operation.hpp"
#include "tt_dnn/op_library/work_split.hpp"
#include "tt_metal/common/constants.hpp"
#include "tt_metal/detail/util.hpp"
#include "tt_metal/host_api.hpp"
#include "unary_op.hpp"

namespace ttnn::operations::unary::detail {

using namespace tt::constants;

operation::ProgramWithCallbacks unary_sharded(const Tensor &input, Tensor &output, const std::vector<UnaryWithParam> op_chain, bool fp32_dest_acc_en, bool preserve_fp32_precision){

    tt::tt_metal::Program program = CreateProgram();
    tt::tt_metal::Device *device = input.device();

    auto shard_spec = input.shard_spec().value();
    auto all_cores = shard_spec.grid;
    uint32_t ncores = shard_spec.num_cores();

    auto out_shard_spec = output.shard_spec().value();
    TT_FATAL(
        out_shard_spec.num_cores() == ncores,
        "Output tensor should have same number of cores {} as input tensor {}",
        out_shard_spec.num_cores(),
        ncores);

    tt::DataFormat act_df = tt::tt_metal::datatype_to_dataformat_converter(input.get_dtype());
    tt::DataFormat out_df = tt::tt_metal::datatype_to_dataformat_converter(output.get_dtype());

    uint32_t input_tile_size = tt::tt_metal::detail::TileSize(act_df);
    uint32_t output_tile_size = tt::tt_metal::detail::TileSize(out_df);

    TT_FATAL(input_tile_size == output_tile_size, "Input and output tile size should be same");

    uint32_t num_tile_per_core = 0;

    if (input.get_dtype() == DataType::BFLOAT8_B) {
        uint32_t ntiles_along_width = ceil(shard_spec.shape[1] / (float) tt::constants::TILE_WIDTH);
        uint32_t ntiles_along_height = ceil(shard_spec.shape[0] / (float) tt::constants::TILE_HEIGHT);
        num_tile_per_core = ntiles_along_width * ntiles_along_height;
    } else {
        TT_FATAL(
            (shard_spec.shape[1] * datum_size(act_df)) % L1_ALIGNMENT == 0,
            "Shard width should be multiple of L1_ADRESS_ALIGNMENT");
        size_t shard_height = shard_spec.shape[0];
        size_t shard_width = round_up_to_mul16(
            shard_spec.shape[1]);  // rounding up is done to aligned with  --> tt-metal/tt_metal/detail/util.hpp:31
        size_t shard_size_in_bytes = shard_height * shard_width * datum_size(act_df);
        TT_FATAL(shard_size_in_bytes % input_tile_size == 0, "Shard Size must be multiple of input_tile_size");
        num_tile_per_core = (shard_size_in_bytes + input_tile_size - 1) / input_tile_size;  // ceil value
    }

    uint32_t in_cb_id = tt::CB::c_in0;
    uint32_t buffering_factor = 1;  // data is already fully buffered in the CBs since its sharded
    uint32_t aligned_input_tile_nbytes =
        round_up_to_mul32(input_tile_size);  // will have issue if the page is not multiple of 32
    uint32_t in_cb_pagesize = aligned_input_tile_nbytes;
    uint32_t in_cb_npages = num_tile_per_core * buffering_factor;
    tt::tt_metal::CircularBufferConfig cb_src0_config = tt::tt_metal::CircularBufferConfig(
                                            in_cb_pagesize * in_cb_npages,
                                            {{in_cb_id, act_df}})
                                          .set_page_size(in_cb_id, in_cb_pagesize)
                                          .set_globally_allocated_address(*input.buffer());
    auto cb_src0 = tt::tt_metal::CreateCircularBuffer(program, all_cores, cb_src0_config);

    // output sharded CB
    uint32_t out_cb_id = tt::CB::c_out0;
    tt::tt_metal::CircularBufferConfig out_cb_config = tt::tt_metal::CircularBufferConfig(
                                            in_cb_pagesize * in_cb_npages,
                                            {{out_cb_id, out_df}})
                                          .set_page_size(out_cb_id, in_cb_pagesize)
                                          .set_globally_allocated_address(*output.buffer());
    auto out_cb = tt::tt_metal::CreateCircularBuffer(program, all_cores, out_cb_config);

    log_debug(tt::LogOp, "input_cb: {}, npages: {}, pagesize: {}", in_cb_id, in_cb_npages, in_cb_pagesize);
    log_debug(tt::LogOp, "input_tile_size: {}", input_tile_size);

    auto src_buffer = input.buffer();
    auto dst_buffer = output.buffer();

    bool src_is_dram = src_buffer->buffer_type() == tt::tt_metal::BufferType::DRAM ? 1 : 0;
    TT_FATAL(src_is_dram == 0, "Input buffer should be in L1");
    std::vector<uint32_t> reader_compile_time_args = {
        in_cb_id,
    };

    bool dst_is_dram = dst_buffer->buffer_type() == tt::tt_metal::BufferType::DRAM ? 1 : 0;
    TT_FATAL(dst_is_dram == 0, "Output buffer should be in L1");

    std::map<string, string> kernel_defines;
    tt::tt_metal::KernelHandle unary_reader_kernel_id = tt::tt_metal::CreateKernel(
        program,
        "ttnn/cpp/ttnn/operations/eltwise/unary/device/kernels/dataflow/reader_unary_sharded.cpp",
        all_cores,
        tt::tt_metal::ReaderDataMovementConfig(reader_compile_time_args, kernel_defines));

    vector<uint32_t> compute_kernel_args_group_1 = {
        1,                 // per_core_block_cnt
        num_tile_per_core  // per_core_block_size
    };

    bool math_approx_mode = std::all_of(
        op_chain.begin(), op_chain.end(), [](const auto &u) { return utils::get_op_approx_mode(u.op_type); });
    std::map<string, string> unary_defines = utils::get_block_defines(op_chain);
    auto eltwise_unary_kernel_group_1_id = tt::tt_metal::CreateKernel(
        program,
        "tt_metal/kernels/compute/eltwise_sfpu.cpp",
        all_cores,
        tt::tt_metal::ComputeConfig{
            .math_fidelity = MathFidelity::HiFi4,
            .fp32_dest_acc_en = fp32_dest_acc_en,
            .preserve_fp32_precision = preserve_fp32_precision,
            .math_approx_mode = math_approx_mode,
            .compile_args = compute_kernel_args_group_1,
            .defines = unary_defines});

    tt::tt_metal::SetRuntimeArgs(
        program,
        unary_reader_kernel_id,
        all_cores,
        {
            (uint32_t)(num_tile_per_core),
        });

    auto override_runtime_args_callback = [unary_reader_kernel_id, cb_src0, out_cb](
                                              const void *operation,
                                              Program &program,
                                              const std::vector<Tensor> &input_tensors,
                                              const std::vector<std::optional<const Tensor>> &,
                                              const std::vector<Tensor> &output_tensors) {
        auto src_buffer = input_tensors.at(0).buffer();
        auto dst_buffer = output_tensors.at(0).buffer();
        tt::tt_metal::UpdateDynamicCircularBufferAddress(program, cb_src0, *src_buffer);
        tt::tt_metal::UpdateDynamicCircularBufferAddress(program, out_cb, *dst_buffer);
    };

    return {.program = std::move(program), .override_runtime_arguments_callback = override_runtime_args_callback};
}

}  // namespace ttnn::operations::unary
