import onnx
from typing import List, NamedTuple, Optional
import torch
import logging

_logger = logging.getLogger(__name__)


class _Output(NamedTuple):
    name: str
    value: torch.Tensor


class _Outputs:
    _values: List[_Output]

    def __init__(self) -> None:
        self._values = []

    @property
    def values(self) -> List[_Output]:
        return self._values

    def add(self, name: str, value: torch.Tensor) -> None:
        self._values.append(_Output(name, value))

    def add_outputs_to_model(self, onnx_graph: onnx.ModelProto) -> None:
        if len(self.values) == 0:
            return

        old_name_to_new_name = {}
        n_output = len(onnx_graph.graph.output)
        assert n_output >= len(self.values)

        # Rename last len(self.values) outputs
        for i, additional_output in enumerate(onnx_graph.graph.output[-len(self.values):]):
            name = self.values[i].name
            orig_name = additional_output.name
            old_name_to_new_name[orig_name] = name
            additional_output.name = name

        # Rename names in graph
        for node in onnx_graph.graph.node:
            for i, v in enumerate(node.input):
                if v in old_name_to_new_name:
                    node.input[i] = old_name_to_new_name[v]
            for i, v in enumerate(node.output):
                if v in old_name_to_new_name:
                    node.output[i] = old_name_to_new_name[v]

        for v in onnx_graph.graph.input:
            if v.name in old_name_to_new_name:
                v.name = old_name_to_new_name[v.name]


_outputs: Optional[_Outputs] = None


class _ModuleWithAdditionalOutputs(torch.nn.Module):
    def __init__(self, module: torch.nn.Module, outputs: _Outputs) -> None:
        super().__init__()
        self.module = module
        self.outputs = outputs

    def forward(self, *args, **kwargs):
        out = self.module(*args, **kwargs)
        if len(self.outputs.values) == 0:
            return out
        if isinstance(out, torch.Tensor):
            out = [out]
        elif not isinstance(out, list):
            out = list(out)
        out.extend([value for _, value in self.outputs.values])
        return out

    def state_dict(self, *args, **kwargs):
        return self.module.state_dict(*args, **kwargs)

    def load_state_dict(self, *args, **kwargs):
        return self.module.load_state_dict(*args, **kwargs)


def _start_trace(module: torch.nn.Module):
    global _outputs
    assert _outputs is None
    _outputs = _Outputs()
    return _ModuleWithAdditionalOutputs(module, _outputs)


def _end_trace(onnx_graph: onnx.ModelProto) -> onnx.ModelProto:
    global _outputs
    if _outputs is not None:
        _logger.warning(
            f"Old outputs remains with the output of {[v for v, _ in _outputs.values]}. "
            "The previous export might fail with exceptions."
        )

    onnx_graph = _outputs.add_outputs_to_model(onnx_graph)
    _outputs = None
    return onnx_graph


def as_output(name: str, value: torch.Tensor) -> torch.Tensor:
    global _outputs
    if _outputs is not None:
        _outputs.add(name, value)
    return value
