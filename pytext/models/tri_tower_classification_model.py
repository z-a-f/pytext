#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

from typing import Dict, List, Optional, Tuple, Union

import torch
from pytext.common.constants import Stage
from pytext.config import ConfigBase
from pytext.config.component import create_loss
from pytext.data.roberta_tensorizer import RoBERTaTensorizer
from pytext.data.tensorizers import FloatListTensorizer, LabelTensorizer, Tensorizer
from pytext.loss import BinaryCrossEntropyLoss, MultiLabelSoftMarginLoss
from pytext.models.decoders.mlp_decoder_n_tower import MLPDecoderNTower
from pytext.models.decoders.mlp_decoder_tri_tower import MLPDecoderTriTower
from pytext.models.model import BaseModel
from pytext.models.module import create_module
from pytext.models.output_layers import ClassificationOutputLayer
from pytext.models.output_layers.doc_classification_output_layer import (
    BinaryClassificationOutputLayer,
    MulticlassOutputLayer,
    MultiLabelOutputLayer,
)
from pytext.models.roberta import RoBERTaEncoder, RoBERTaEncoderBase
from pytext.torchscript.module import ScriptPyTextTriTowerModuleWithDense
from pytext.utils.label import get_label_weights
from pytext.utils.usage import log_class_usage
from torch.quantization import convert_jit, get_default_qconfig, prepare_jit


class TriTowerClassificationModel(BaseModel):

    SUPPORT_FP16_OPTIMIZER = True

    class Config(BaseModel.Config):
        class InputConfig(ConfigBase):
            right_tokens: RoBERTaTensorizer.Config = RoBERTaTensorizer.Config()
            middle_tokens: RoBERTaTensorizer.Config = RoBERTaTensorizer.Config()
            left_tokens: RoBERTaTensorizer.Config = RoBERTaTensorizer.Config()
            right_dense: Optional[FloatListTensorizer.Config] = None
            middle_dense: Optional[FloatListTensorizer.Config] = None
            left_dense: Optional[FloatListTensorizer.Config] = None

            labels: LabelTensorizer.Config = LabelTensorizer.Config()

        inputs: InputConfig = InputConfig()
        right_encoder: RoBERTaEncoderBase.Config = RoBERTaEncoder.Config()
        middle_encoder: RoBERTaEncoderBase.Config = RoBERTaEncoder.Config()
        left_encoder: RoBERTaEncoderBase.Config = RoBERTaEncoder.Config()
        decoder: Union[
            MLPDecoderTriTower.Config, MLPDecoderNTower.Config
        ] = MLPDecoderNTower.Config()
        output_layer: ClassificationOutputLayer.Config = (
            ClassificationOutputLayer.Config()
        )
        use_shared_embedding: Optional[bool] = False
        vocab_size: Optional[int] = 250002
        hidden_dim: Optional[int] = 768
        padding_idx: Optional[int] = 1

    def trace(self, inputs):
        return torch.jit.trace(self, inputs)

    def torchscriptify(self, tensorizers, traced_model):
        """Using the traced model, create a ScriptModule which has a nicer API that
        includes generating tensors from simple data types, and returns classified
        values according to the output layer (eg. as a dict mapping class name to score)
        """
        right_script_tensorizer = tensorizers["right_tokens"].torchscriptify()
        middle_script_tensorizer = tensorizers["middle_tokens"].torchscriptify()
        left_script_tensorizer = tensorizers["left_tokens"].torchscriptify()

        return ScriptPyTextTriTowerModuleWithDense(
            model=traced_model,
            output_layer=self.output_layer.torchscript_predictions(),
            right_tensorizer=right_script_tensorizer,
            middle_tensorizer=middle_script_tensorizer,
            left_tensorizer=left_script_tensorizer,
            right_normalizer=tensorizers["right_dense"].normalizer,
            middle_normalizer=tensorizers["middle_dense"].normalizer,
            left_normalizer=tensorizers["left_dense"].normalizer,
        )

    def graph_mode_quantize(self, inputs, data_loader, calibration_num_batches=64):
        """Quantize the model during export with graph mode quantization for linformer encoder."""
        if (
            isinstance(self.right_encoder, RoBERTaEncoder)
            and self.right_encoder.use_linformer_encoder
            and isinstance(self.middle_encoder, RoBERTaEncoder)
            and self.middle_encoder.use_linformer_encoder
            and isinstance(self.left_encoder, RoBERTaEncoder)
            and self.left_encoder.use_linformer_encoder
        ):
            trace = self.trace(inputs)
            qconfig = get_default_qconfig("fbgemm")
            qconfig_dict = {"": qconfig}
            prepare_m = prepare_jit(trace, qconfig_dict, inplace=False)
            prepare_m.eval()
            with torch.no_grad():
                for i, (_, batch) in enumerate(data_loader):
                    print("Running calibration with batch {}".format(i))
                    input_data = self.onnx_trace_input(batch)
                    prepare_m(*input_data)
                    if i == calibration_num_batches - 1:
                        break
            trace = convert_jit(prepare_m, inplace=True)
        else:
            super().quantize()
            trace = self.trace(inputs)

        return trace

    def arrange_model_inputs(self, tensor_dict):
        model_inputs = (
            tensor_dict["right_tokens"],
            tensor_dict["middle_tokens"],
            tensor_dict["left_tokens"],
            tensor_dict["right_dense"],
            tensor_dict["middle_dense"],
            tensor_dict["left_dense"],
        )

        return model_inputs

    def arrange_targets(self, tensor_dict):
        return tensor_dict["labels"]

    def forward(
        self,
        right_encoder_inputs: Tuple[torch.Tensor, ...],
        middle_encoder_inputs: Tuple[torch.Tensor, ...],
        left_encoder_inputs: Tuple[torch.Tensor, ...],
        *args
    ) -> List[torch.Tensor]:

        if self.right_encoder.output_encoded_layers:
            # if encoded layers are returned, discard them
            right_representation = self.right_encoder(right_encoder_inputs)[1]
        else:
            right_representation = self.right_encoder(right_encoder_inputs)[0]
        if self.middle_encoder.output_encoded_layers:
            # if encoded layers are returned, discard them
            middle_representation = self.middle_encoder(middle_encoder_inputs)[1]
        else:
            middle_representation = self.middle_encoder(middle_encoder_inputs)[0]
        if self.left_encoder.output_encoded_layers:
            # if encoded layers are returned, discard them
            left_representation = self.left_encoder(left_encoder_inputs)[1]
        else:
            left_representation = self.left_encoder(left_encoder_inputs)[0]
        return self.decoder(
            right_representation, middle_representation, left_representation, *args
        )

    def caffe2_export(self, tensorizers, tensor_dict, path, export_onnx_path=None):
        raise NotImplementedError

    @classmethod
    def from_config(cls, config: Config, tensorizers: Dict[str, Tensorizer]):
        labels = tensorizers["labels"].vocab
        if not labels:
            raise ValueError("Labels were not created, see preceding errors")

        if config.use_shared_embedding:
            token_embedding = torch.nn.Embedding(
                config.vocab_size, config.hidden_dim, padding_idx=config.padding_idx
            )
        else:
            token_embedding = None

        right_vocab = tensorizers["right_tokens"].vocab
        right_encoder = create_module(
            config.right_encoder,
            token_embedding=token_embedding,
            padding_idx=right_vocab.get_pad_index(),
            vocab_size=len(right_vocab),
        )
        middle_vocab = tensorizers["middle_tokens"].vocab
        middle_encoder = create_module(
            config.middle_encoder,
            token_embedding=token_embedding,
            padding_idx=middle_vocab.get_pad_index(),
            vocab_size=len(middle_vocab),
        )
        left_vocab = tensorizers["left_tokens"].vocab
        left_encoder = create_module(
            config.left_encoder,
            token_embedding=token_embedding,
            padding_idx=left_vocab.get_pad_index(),
            vocab_size=len(left_vocab),
        )

        right_dense_dim = tensorizers["right_dense"].dim
        middle_dense_dim = tensorizers["middle_dense"].dim
        left_dense_dim = tensorizers["left_dense"].dim
        decoder = None
        if isinstance(config.decoder, MLPDecoderTriTower.Config):
            decoder = create_module(
                config.decoder,
                right_dim=right_encoder.representation_dim + right_dense_dim,
                middle_dim=middle_encoder.representation_dim + middle_dense_dim,
                left_dim=left_encoder.representation_dim + left_dense_dim,
                to_dim=len(labels),
            )
        elif isinstance(config.decoder, MLPDecoderNTower.Config):
            decoder = create_module(
                config.decoder,
                tower_dims=[
                    right_encoder.representation_dim + right_dense_dim,
                    middle_encoder.representation_dim + middle_dense_dim,
                    left_encoder.representation_dim + left_dense_dim,
                ],
                to_dim=len(labels),
            )

        label_weights = (
            get_label_weights(labels.idx, config.output_layer.label_weights)
            if config.output_layer.label_weights
            else None
        )

        loss = create_loss(config.output_layer.loss, weight=label_weights)

        if isinstance(loss, BinaryCrossEntropyLoss):
            output_layer_cls = BinaryClassificationOutputLayer
        elif isinstance(loss, MultiLabelSoftMarginLoss):
            output_layer_cls = MultiLabelOutputLayer
        else:
            output_layer_cls = MulticlassOutputLayer

        output_layer = output_layer_cls(list(labels), loss)
        return cls(
            right_encoder,
            middle_encoder,
            left_encoder,
            decoder,
            output_layer,
            config.use_shared_embedding,
            config.vocab_size,
            config.hidden_dim,
            config.padding_idx,
        )

    def __init__(
        self,
        right_encoder,
        middle_encoder,
        left_encoder,
        decoder,
        output_layer,
        use_shared_embedding,
        vocab_size,
        hidden_dim,
        padding_idx,
        stage=Stage.TRAIN,
    ) -> None:
        super().__init__(stage=stage)
        self.right_encoder = right_encoder
        self.middle_encoder = middle_encoder
        self.left_encoder = left_encoder
        self.decoder = decoder
        self.module_list = [right_encoder, middle_encoder, left_encoder, decoder]
        self.output_layer = output_layer
        self.use_shared_embedding = use_shared_embedding
        self.vocab_size = vocab_size
        self.hidden_dim = hidden_dim
        self.padding_idx = padding_idx
        self.stage = stage
        log_class_usage(__class__)
