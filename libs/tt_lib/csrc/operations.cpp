#include "tt_dnn/op_library/bmm/bmm_op.hpp"

#include "operations.hpp"


namespace tt {
namespace operations {
namespace primary {

void py_module(py::module& m_primary) {
    py::class_<MatmulMultiCoreReuseProgramConfig>(m_primary, "MatmulMultiCoreReuseProgramConfig")
        .def(
            py::init<>(
                [] (
                    std::tuple<std::size_t, std::size_t> compute_with_storage_grid_size,
                    std::size_t in0_block_w,
                    std::size_t out_subblock_h,
                    std::size_t out_subblock_w,
                    std::size_t per_core_M,
                    std::size_t per_core_N
                ) {

                    return MatmulMultiCoreReuseProgramConfig{
                        .compute_with_storage_grid_size={std::get<0>(compute_with_storage_grid_size), std::get<1>(compute_with_storage_grid_size)},
                        .in0_block_w=in0_block_w,
                        .out_subblock_h=out_subblock_h,
                        .out_subblock_w=out_subblock_w,
                        .per_core_M=per_core_M,
                        .per_core_N=per_core_N,
                    };

                }
            ),
            py::kw_only(),
            py::arg("compute_with_storage_grid_size").noconvert(),
            py::arg("in0_block_w").noconvert(),
            py::arg("out_subblock_h").noconvert(),
            py::arg("out_subblock_w").noconvert(),
            py::arg("per_core_M").noconvert(),
            py::arg("per_core_N").noconvert()
        );
    py::class_<MatmulMultiCoreReuseMultiCastProgramConfig>(m_primary, "MatmulMultiCoreReuseMultiCastProgramConfig")
        .def(
            py::init<>(
                [] (
                    std::tuple<std::size_t, std::size_t> compute_with_storage_grid_size,
                    std::size_t in0_block_w,
                    std::size_t out_subblock_h,
                    std::size_t out_subblock_w,
                    std::size_t per_core_M,
                    std::size_t per_core_N,
                    bool fuse_gelu_activation
                ) {

                    return MatmulMultiCoreReuseMultiCastProgramConfig{
                        .compute_with_storage_grid_size={std::get<0>(compute_with_storage_grid_size), std::get<1>(compute_with_storage_grid_size)},
                        .in0_block_w=in0_block_w,
                        .out_subblock_h=out_subblock_h,
                        .out_subblock_w=out_subblock_w,
                        .per_core_M=per_core_M,
                        .per_core_N=per_core_N,
                        .fuse_gelu_activation=fuse_gelu_activation,
                    };

                }
            ),
            py::kw_only(),
            py::arg("compute_with_storage_grid_size").noconvert(),
            py::arg("in0_block_w").noconvert(),
            py::arg("out_subblock_h").noconvert(),
            py::arg("out_subblock_w").noconvert(),
            py::arg("per_core_M").noconvert(),
            py::arg("per_core_N").noconvert(),
            py::arg("fuse_gelu_activation").noconvert()
        );

    m_primary.def(
        "matmul",
        [](const Tensor& input_tensor_a, const Tensor& input_tensor_b, const MemoryConfig& out_mem_config, std::optional<DataType> output_dtype) {
            return matmul(input_tensor_a, input_tensor_b, MatmulDefaultProgramConfig{}, out_mem_config, output_dtype);
        },
        py::arg("input_tensor_a").noconvert(),
        py::arg("input_tensor_b").noconvert(),
        py::kw_only(),
        py::arg("output_mem_config").noconvert() = operation::DEFAULT_OUTPUT_MEMORY_CONFIG,
        py::arg("output_dtype").noconvert() = std::nullopt,
        R"doc(
            Perform a matrix multiplication ``input_tensor_a x input_tensor_b``.

            .. csv-table::
                :header: "Argument", "Description", "Data type", "Valid range", "Required"

                "input_tensor_a", "First tensor to multiply", "Tensor", "Tensor of shape [B_a, C_a, M, K]", "Yes"
                "input_tensor_b", "Second tensor to multiply", "Tensor", "Tensor of shape [B_b, C_b, K, N]", "Yes"
                "program_config", "", "", ""
                "output_mem_config", "Layout of tensor in TT Accelerator device memory banks", "MemoryConfig", "Default is interleaved in DRAM", "No"
                "output_dtype", "Output Data Type", "DataType", "By default it will be set to the data type of `input_tensor_a`", "No"
        )doc"
    );

    m_primary.def(
        "matmul",
        [](const Tensor& input_tensor_a, const Tensor& input_tensor_b, const MatmulMultiCoreReuseProgramConfig& program_config, const MemoryConfig& out_mem_config, std::optional<DataType> output_dtype) {
            return matmul(input_tensor_a, input_tensor_b, program_config, out_mem_config, output_dtype);
        },
        py::arg("input_tensor_a").noconvert(),
        py::arg("input_tensor_b").noconvert(),
        py::kw_only(),
        py::arg("program_config").noconvert(),
        py::arg("output_mem_config").noconvert() = operation::DEFAULT_OUTPUT_MEMORY_CONFIG,
        py::arg("output_dtype").noconvert() = std::nullopt,
        R"doc(
            Perform a matrix multiplication ``input_tensor_a x input_tensor_b``.

            .. csv-table::
                :header: "Argument", "Description", "Data type", "Valid range", "Required"

                "input_tensor_a", "First tensor to multiply", "Tensor", "Tensor of shape [B_a, C_a, M, K]", "Yes"
                "input_tensor_b", "Second tensor to multiply", "Tensor", "Tensor of shape [B_b, C_b, K, N]", "Yes"
                "program_config", "", "MatmulMultiCoreReuseProgramConfig", "", "Yes"
                "output_mem_config", "Layout of tensor in TT Accelerator device memory banks", "MemoryConfig", "Default is interleaved in DRAM", "No"
                "output_dtype", "Output Data Type", "DataType", "By default it will be set to the data type of `input_tensor_a`", "No"
        )doc"
    );

    m_primary.def(
        "matmul",
        [](const Tensor& input_tensor_a, const Tensor& input_tensor_b, std::optional<const Tensor> bias, const MatmulMultiCoreReuseMultiCastProgramConfig& program_config, const MemoryConfig& out_mem_config, std::optional<DataType> output_dtype) {
            return matmul(input_tensor_a, input_tensor_b, bias, program_config, out_mem_config, output_dtype);
        },
        py::arg("input_tensor_a").noconvert(),
        py::arg("input_tensor_b").noconvert(),
        py::kw_only(),
        py::arg("bias").noconvert() = std::nullopt,
        py::arg("program_config").noconvert(),
        py::arg("output_mem_config").noconvert() = operation::DEFAULT_OUTPUT_MEMORY_CONFIG,
        py::arg("output_dtype").noconvert() = std::nullopt,
        R"doc(
            Perform a matrix multiplication ``input_tensor_a x input_tensor_b``.

            .. csv-table::
                :header: "Argument", "Description", "Data type", "Valid range", "Required"

                "input_tensor_a", "First tensor to multiply", "Tensor", "Tensor of shape [B_a, C_a, M, K]", "Yes"
                "input_tensor_b", "Second tensor to multiply", "Tensor", "Tensor of shape [B_b, C_b, K, N]", "Yes"
                "bias", "Bias to add", "Tensor", "Tensor of shape [1, 1, 1, N]", "Yes"
                "program_config", "", "MatmulMultiCoreReuseMultiCastProgramConfig", "", "Yes"
                "output_mem_config", "Layout of tensor in TT Accelerator device memory banks", "MemoryConfig", "Default is interleaved in DRAM", "No"
                "output_dtype", "Output Data Type", "DataType", "By default it will be set to the data type of `input_tensor_a`", "No"
        )doc"
    );

}

}  // namespace primary

void py_module(py::module& m_operations) {
    py::module_ m_primary = m_operations.def_submodule("primary", "Primary operations");
    primary::py_module(m_primary);
}

}  // namespace operations

}  // namespace tt
