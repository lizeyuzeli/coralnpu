// Copyright 2026 lizeyu

#include "tape_out/sw/opt/litert-micro/conv_1x1.h"

#include <riscv_vector.h>

#include <algorithm>
#include <cstdint>
#include <cstdlib>

#include "sw/opt/litert-micro/accumulator_util.h"
#include "sw/opt/litert-micro/memory_util.h"
#include "sw/opt/rvv_opt.h"
#include "tensorflow/lite/kernels/internal/common.h"
#include "tensorflow/lite/kernels/internal/reference/integer_ops/conv.h"
#include "tensorflow/lite/kernels/kernel_util.h"
#include "tensorflow/lite/micro/kernels/kernel_util.h"
#ifdef USE_TFLM_COMPRESSION
#error "USE_TFLM_COMPRESSION is not supported"
#endif  // USE_TFLM_COMPRESSION

// Leverage compiler register allocator, but inline assembly MAC
#define CONV_1x1_MAC(fil, idx)                                          \
  fil_vec_ext = __riscv_vsext_vf2_i16m2(fil, vl);                  \
  mul_vec = __riscv_vwmul_vv_i32m4(in_vec_ext, fil_vec_ext, vl);  \
  acc_val = __riscv_vmv_x_s_i32m1_i32(                                  \
            __riscv_vredsum_vs_i32m4_i32m1(mul_vec,                     \
            __riscv_vmv_v_x_i32m1(0, 1), vl));                          \
  accs_buf[block_base_ptr + idx] += acc_val;

namespace coralnpu_v2::opt::litert_micro {

using tflite::ConvParams;
using tflite::kConvBiasTensor;
using tflite::kConvInputTensor;
using tflite::kConvOutputTensor;
using tflite::kConvWeightsTensor;
using tflite::NumInputs;
using tflite::OpDataConv;
using tflite::RuntimeShape;
using tflite::micro::GetEvalInput;
using tflite::micro::GetEvalOutput;
using tflite::micro::GetOptionalTensorData;
using tflite::micro::GetTensorData;
using tflite::micro::GetTensorShape;

// Tiled 1x1 kernel: out_block_size = 16, in_chunk_size = 16
void Conv_1x1_Tiled16(
    const ConvParams& params, const OpDataConvCustom& data,
    const int32_t* output_multiplier, const uint8_t* shift_left,
    const uint8_t* shift_right, TfLiteContext* context,
    const RuntimeShape& input_shape, const int8_t* input_data,
    const RuntimeShape& filter_shape, const int8_t* filter_data,
    const RuntimeShape& bias_shape, const int32_t* bias_data,
    const RuntimeShape& output_shape, int8_t* output_data) {

  const int batches = MatchingDim(input_shape, 0, output_shape, 0);
  const int input_depth = input_shape.Dims(3);
  const int output_height = output_shape.Dims(1);
  const int output_width = output_shape.Dims(2);
  const int output_depth = output_shape.Dims(3);

  const int32_t output_offset = params.output_offset;
  const int32_t output_activation_min = params.quantized_activation_min;
  const int32_t output_activation_max = params.quantized_activation_max;
  const int filter_col_stride = input_depth;
  const int32_t input_offset = params.input_offset;

  int32_t* accs_buf = static_cast<int32_t*>(
      context->GetScratchBuffer(context, data.accs_buffer_index));
  TFLITE_DCHECK_NE(accs_buf, nullptr);
  // zero accumulator buffer
  Memset(accs_buf, 0,
         batches * output_height * output_width * output_depth * sizeof(int32_t));

  const int OUT_BLOCK_SIZE = 16;
  const int IN_CHUNK_SIZE = 16;

  // Process output channels in blocks of 16
  for (int out_channel = 0; out_channel < output_depth; out_channel += OUT_BLOCK_SIZE) {
    for (int in_channel = 0; in_channel < input_depth; in_channel += IN_CHUNK_SIZE) {
      // set vl for this chunk (number of 8-bit elements)
      size_t vl = __riscv_vsetvl_e8m1(IN_CHUNK_SIZE);

      // Load filter vectors for all outputs in this out-block for this chunk.
      const int8_t* filter_base_ptr = filter_data + Offset(filter_shape, out_channel, 0, 0, in_channel);

      vint8m1_t fil_vec0 = __riscv_vle8_v_i8m1(filter_base_ptr, vl);
      vint8m1_t fil_vec1 = __riscv_vle8_v_i8m1(filter_base_ptr + 1 * filter_col_stride, vl);
      vint8m1_t fil_vec2 = __riscv_vle8_v_i8m1(filter_base_ptr + 2 * filter_col_stride, vl);
      vint8m1_t fil_vec3 = __riscv_vle8_v_i8m1(filter_base_ptr + 3 * filter_col_stride, vl);
      vint8m1_t fil_vec4 = __riscv_vle8_v_i8m1(filter_base_ptr + 4 * filter_col_stride, vl);
      vint8m1_t fil_vec5 = __riscv_vle8_v_i8m1(filter_base_ptr + 5 * filter_col_stride, vl);
      vint8m1_t fil_vec6 = __riscv_vle8_v_i8m1(filter_base_ptr + 6 * filter_col_stride, vl);
      vint8m1_t fil_vec7 = __riscv_vle8_v_i8m1(filter_base_ptr + 7 * filter_col_stride, vl);
      vint8m1_t fil_vec8 = __riscv_vle8_v_i8m1(filter_base_ptr + 8 * filter_col_stride, vl);
      vint8m1_t fil_vec9 = __riscv_vle8_v_i8m1(filter_base_ptr + 9 * filter_col_stride, vl);
      vint8m1_t fil_vec10 = __riscv_vle8_v_i8m1(filter_base_ptr + 10 * filter_col_stride, vl);
      vint8m1_t fil_vec11 = __riscv_vle8_v_i8m1(filter_base_ptr + 11 * filter_col_stride, vl);
      vint8m1_t fil_vec12 = __riscv_vle8_v_i8m1(filter_base_ptr + 12 * filter_col_stride, vl);
      vint8m1_t fil_vec13 = __riscv_vle8_v_i8m1(filter_base_ptr + 13 * filter_col_stride, vl);
      vint8m1_t fil_vec14 = __riscv_vle8_v_i8m1(filter_base_ptr + 14 * filter_col_stride, vl);
      vint8m1_t fil_vec15 = __riscv_vle8_v_i8m1(filter_base_ptr + 15 * filter_col_stride, vl);

      for (int batch = 0; batch < batches; ++batch) {
        for (int out_y = 0; out_y < output_height; ++out_y) {
          for (int out_x = 0; out_x < output_width; ++out_x) {
            const int8_t* in_ptr_chunk = input_data + Offset(input_shape, batch, out_y, out_x, in_channel);
            vint8m1_t in_vec = __riscv_vle8_v_i8m1(in_ptr_chunk, vl);
            vint16m2_t in_vec_ext = __riscv_vsext_vf2_i16m2(in_vec, vl);
            in_vec_ext = __riscv_vadd_vx_i16m2(in_vec_ext, input_offset, vl);

            vint16m2_t fil_vec_ext;
            vint32m4_t mul_vec;
            int32_t acc_val;
            const int block_base_ptr = Offset(output_shape, batch, out_y, out_x, out_channel);

            CONV_1x1_MAC(fil_vec0, 0);
            CONV_1x1_MAC(fil_vec1, 1);
            CONV_1x1_MAC(fil_vec2, 2);
            CONV_1x1_MAC(fil_vec3, 3);
            CONV_1x1_MAC(fil_vec4, 4);
            CONV_1x1_MAC(fil_vec5, 5);
            CONV_1x1_MAC(fil_vec6, 6);
            CONV_1x1_MAC(fil_vec7, 7);
            CONV_1x1_MAC(fil_vec8, 8);
            CONV_1x1_MAC(fil_vec9, 9);
            CONV_1x1_MAC(fil_vec10, 10);
            CONV_1x1_MAC(fil_vec11, 11);
            CONV_1x1_MAC(fil_vec12, 12);
            CONV_1x1_MAC(fil_vec13, 13);
            CONV_1x1_MAC(fil_vec14, 14);
            CONV_1x1_MAC(fil_vec15, 15);
          }    // out_x
        }      // out_y
      }        // batch
    }  // in-chunk loop
  }  // out-block loop

  // Post process whole buffer (same as other kernels)
  PostprocessAcc(accs_buf, bias_data, shift_left, output_multiplier, shift_right,
                 output_offset, output_activation_min, output_activation_max,
                 output_data, batches * output_height * output_width, output_depth);
}

#undef CONV_1x1_MAC

void ConvPerChannel(const ConvParams& params, const OpDataConvCustom& data,
                    const int32_t* output_multiplier,
                    const int32_t* output_shift, TfLiteContext* context,
                    const RuntimeShape& input_shape, const int8_t* input_data,
                    const RuntimeShape& filter_shape, const int8_t* filter_data,
                    const RuntimeShape& bias_shape, const int32_t* bias_data,
                    const RuntimeShape& output_shape, int8_t* output_data) {
  const int32_t output_activation_min = params.quantized_activation_min;
  const int32_t output_activation_max = params.quantized_activation_max;

  // Consistency check.
  TFLITE_DCHECK_LE(output_activation_min, output_activation_max);
  TFLITE_DCHECK_EQ(input_shape.DimensionsCount(), 4);
  TFLITE_DCHECK_EQ(filter_shape.DimensionsCount(), 4);
  TFLITE_DCHECK_EQ(output_shape.DimensionsCount(), 4);
  const int input_depth = input_shape.Dims(3);
  const int output_depth = MatchingDim(filter_shape, 0, output_shape, 3);

  if (bias_data) {
    TFLITE_DCHECK_EQ(bias_shape.FlatSize(), output_depth);
  }

  // Check dimensions of the tensors.
  const int stride_height = params.stride_height;
  const int stride_width = params.stride_width;
  const int pad_height = params.padding_values.height;
  const int pad_width = params.padding_values.width;
  const int filter_height = filter_shape.Dims(1);
  const int filter_width = filter_shape.Dims(2);
  const int filter_input_depth = filter_shape.Dims(3);

  const int groups = input_depth / filter_input_depth;
  TFLITE_DCHECK_NE(groups, 0);
  TFLITE_DCHECK_EQ(input_depth % filter_input_depth, 0);
  const int filters_per_group = output_depth / groups;
  TFLITE_DCHECK_NE(filters_per_group, 0);

  // Copy filter and bias to dtcm.
  auto filter_data_copy =
      make_aligned_array<int8_t>(16, filter_shape.FlatSize(), filter_data);
  // TODO(davidgao): if allocation fails, don't copy, use orig
  TFLITE_DCHECK_NE(filter_data_copy, nullptr);

  aligned_array<int32_t> bias_data_copy;
  if (bias_data) {
    bias_data_copy = make_aligned_array<int32_t>(16, output_depth, bias_data);
    // TODO(davidgao): if allocation fails, don't copy, use orig
    TFLITE_DCHECK_NE(bias_data_copy, nullptr);
  }

  // Shifting from quantization params for vectorization
  auto shift_left = make_aligned_array<uint8_t>(16, output_depth);
  TFLITE_DCHECK_NE(shift_left, nullptr);
  auto shift_right = make_aligned_array<uint8_t>(16, output_depth);
  TFLITE_DCHECK_NE(shift_right, nullptr);
  PrepareShiftParams(shift_left.get(), shift_right.get(), output_shift,
                     output_depth);

  if (filter_height == 1 && filter_width == 1 
    && stride_height == 1 && stride_width == 1 
    && pad_height == 0 && pad_width == 0 && groups == 1 
    && input_depth % 16 == 0 && output_depth % 16 == 0) {
    Conv_1x1_Tiled16(params, data, output_multiplier, shift_left.get(),
                    shift_right.get(), context, input_shape, input_data,
                    filter_shape, filter_data_copy.get(), bias_shape,
                    bias_data_copy.get(), output_shape, output_data);
  } else {
    tflite::reference_integer_ops::ConvPerChannel(
        params, output_multiplier, output_shift, input_shape, input_data,
        filter_shape, filter_data, bias_shape, bias_data, output_shape,
        output_data);
  }
}

TfLiteStatus ConvEval(TfLiteContext* context, TfLiteNode* node) {
  TFLITE_DCHECK(node->user_data != nullptr);
  TFLITE_DCHECK(node->builtin_data != nullptr);

  const auto& params =
      *(reinterpret_cast<TfLiteConvParams*>(node->builtin_data));
  const auto& data = *(static_cast<const OpDataConvCustom*>(node->user_data));

  TfLiteEvalTensor* output = GetEvalOutput(context, node, kConvOutputTensor);
  const TfLiteEvalTensor* input = GetEvalInput(context, node, kConvInputTensor);
  const TfLiteEvalTensor* filter =
      GetEvalInput(context, node, kConvWeightsTensor);
  const TfLiteEvalTensor* bias =
      (NumInputs(node) == 3) ? GetEvalInput(context, node, kConvBiasTensor)
                             : nullptr;

  switch (input->type) {  // Already know in/out types are same.
    case kTfLiteInt8: {
      switch (filter->type) {
        case kTfLiteInt8: {
          ConvPerChannel(
              tflite::ConvParamsQuantized(params, data), data,
              data.per_channel_output_multiplier, data.per_channel_output_shift,
              context, GetTensorShape(input), GetTensorData<int8_t>(input),
              GetTensorShape(filter), GetTensorData<int8_t>(filter),
              GetTensorShape(bias), GetOptionalTensorData<int32_t>(bias),
              GetTensorShape(output), GetTensorData<int8_t>(output));
          break;
        }
        default:
          MicroPrintf("Filter type %s (%d) for input type %s not supported.",
                      TfLiteTypeGetName(filter->type), filter->type,
                      TfLiteTypeGetName(input->type));
          return kTfLiteError;
      }
      break;
    }
    default:
      MicroPrintf("Input type %s (%d) not supported.",
                  TfLiteTypeGetName(input->type), input->type);
      return kTfLiteError;
  }
  return kTfLiteOk;
}

void* ConvInit(TfLiteContext* context, const char* buffer, size_t length) {
  // Default tflite::ConvInit as a custom structure (OpDataConvCustom) is used
  // to store the scratch buffer index for our full-tensor accumulator buffering
  // strategy, so we cannot use the default tflite::ConvInit.
  TFLITE_DCHECK(context->AllocatePersistentBuffer != nullptr);
  return context->AllocatePersistentBuffer(context, sizeof(OpDataConvCustom));
}

TfLiteStatus ConvPrepare(TfLiteContext* context, TfLiteNode* node) {
  TF_LITE_ENSURE_OK(context, tflite::ConvPrepare(context, node));

  // A custom Prepare to allocate the full-tensor accumulator buffer used for
  // vectorized post-processing, saving the index in our custom data.
  OpDataConvCustom* data = static_cast<OpDataConvCustom*>(node->user_data);
  tflite::MicroContext* micro_context = tflite::GetMicroContext(context);
  TfLiteTensor* output =
      micro_context->AllocateTempOutputTensor(node, kConvOutputTensor);
  TF_LITE_ENSURE(context, output != nullptr);

  const int batches = output->dims->data[0];
  const int output_height = output->dims->data[1];
  const int output_width = output->dims->data[2];
  const int output_depth = output->dims->data[3];

  size_t required_bytes =
      batches * output_height * output_width * output_depth * sizeof(int32_t);

  TF_LITE_ENSURE_STATUS(context->RequestScratchBufferInArena(
      context, required_bytes, &data->accs_buffer_index));

  micro_context->DeallocateTempTfLiteTensor(output);

  return kTfLiteOk;
}

TFLMRegistration Register_CONV_2D() {
  auto registration = tflite::Register_CONV_2D();
  registration.init = ConvInit;
  registration.prepare = ConvPrepare;
  registration.invoke = ConvEval;
  return registration;
}

}  // namespace coralnpu_v2::opt::litert_micro
