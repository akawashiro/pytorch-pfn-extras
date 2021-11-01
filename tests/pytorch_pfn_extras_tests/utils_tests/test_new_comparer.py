import pytest
import torch

import pytorch_pfn_extras as ppe


class Model(torch.nn.Module):
    def __init__(self, device, ret_val):
        super().__init__()
        self.w = torch.nn.Parameter(torch.zeros(10))
        self.device = device
        self.ret_val = ret_val
        self.iter = 0

    def forward(self, x):
        a = torch.tensor(self.ret_val, device=self.device)
        a.requires_grad = True
        self.iter += 1
        return {"a": a, "iter": self.iter}


def _get_trainer(device, ret_val):
    model = Model(device, ret_val)
    optimizer = torch.optim.SGD(model.parameters(), lr=1.0)
    trainer = ppe.engine.create_trainer(model, optimizer, 1, device=device)
    return trainer


def _get_evaluator(device, ret_val):
    model = Model(device, ret_val)
    evaluator = ppe.engine.create_evaluator(model, device=device)
    return evaluator


def _get_trainer_with_evaluator(device, ret_val):
    model = Model(device, ret_val)
    optimizer = torch.optim.SGD(model.parameters(), lr=1.0)
    evaluator = ppe.engine.create_evaluator(model, device=device)
    trainer = ppe.engine.create_trainer(
        model, optimizer, 1, device=device, evaluator=evaluator)
    return trainer


@pytest.mark.parametrize("engine_fn", [
    _get_trainer, _get_evaluator, _get_trainer_with_evaluator])
def test_compare_every_epoch(engine_fn):
    engine_cpu = engine_fn("cpu", 1.0)
    engine_gpu = engine_fn("cuda:0", 1.0)
    comp = ppe.utils.comparer.Comparer(outputs=["a"])
    train_1 = list(torch.ones(10) for _ in range(10))
    train_2 = list(torch.ones(10) for _ in range(10))
    if engine_fn is _get_trainer_with_evaluator:
        eval_1 = list(torch.ones(10) for _ in range(10))
        eval_2 = list(torch.ones(10) for _ in range(10))
        comp.add_engine("cpu", engine_cpu, train_1, eval_1)
        comp.add_engine("gpu", engine_gpu, train_2, eval_2)
    else:
        comp.add_engine("cpu", engine_cpu, train_1)
        comp.add_engine("gpu", engine_gpu, train_2)
    comp.compare()


@pytest.mark.parametrize("engine_fn", [
    _get_trainer, _get_evaluator, _get_trainer_with_evaluator])
def test_comparer_wrong(engine_fn):
    engine_cpu = engine_fn("cpu", 1.0)
    engine_gpu = engine_fn("cuda:0", 0.5)
    comp = ppe.utils.comparer.Comparer(outputs=["a"])
    train_1 = list(torch.ones(10) for _ in range(10))
    train_2 = list(torch.ones(10) for _ in range(10))
    if engine_fn is _get_trainer_with_evaluator:
        eval_1 = list(torch.ones(10) for _ in range(10))
        eval_2 = list(torch.ones(10) for _ in range(10))
        comp.add_engine("cpu", engine_cpu, train_1, eval_1)
        comp.add_engine("gpu", engine_gpu, train_2, eval_2)
    else:
        comp.add_engine("cpu", engine_cpu, train_1)
        comp.add_engine("gpu", engine_gpu, train_2)
    with pytest.raises(AssertionError):
        comp.compare()


class _CustomComparer:
    def __init__(self, n_iters=None):
        self.times_called = 0
        self.n_iters = n_iters

    def __call__(self, eng_name_1, eng_name_2, out_name, out_1, out_2):
        assert out_name in ("output/a", "output/iter",)
        assert eng_name_1 in ("cpu", "gpu")
        assert eng_name_1 != eng_name_2
        if out_name == "output/iter":
            self.times_called += 1
            assert out_1 == out_2
            assert out_1 == self.times_called * self.n_iters
        else:
            assert out_1.cpu() == out_2.cpu()


@pytest.mark.parametrize("engine_fn", [
    _get_trainer, _get_trainer_with_evaluator])
def test_comparer_n_iters(engine_fn):
    n_iters = 3
    engine_cpu = engine_fn("cpu", 1.0)
    engine_gpu = engine_fn("cuda:0", 1.0)
    compare_fn = _CustomComparer(n_iters)
    comp = ppe.utils.comparer.Comparer(
        trigger=(n_iters, "iteration"), compare_fn=compare_fn)
    train_1 = list(torch.ones(10) for _ in range(10))
    train_2 = list(torch.ones(10) for _ in range(10))
    if engine_fn is _get_trainer_with_evaluator:
        eval_1 = list(torch.ones(10) for _ in range(10))
        eval_2 = list(torch.ones(10) for _ in range(10))
        comp.add_engine("cpu", engine_cpu, train_1, eval_1)
        comp.add_engine("gpu", engine_gpu, train_2, eval_2)
    else:
        comp.add_engine("cpu", engine_cpu, train_1)
        comp.add_engine("gpu", engine_gpu, train_2)
    comp.compare()
    assert compare_fn.times_called == 3


@pytest.mark.parametrize("engine_fn", [
    _get_trainer, _get_evaluator, _get_trainer_with_evaluator])
def test_comparer_kwargs(engine_fn):
    engine_cpu = engine_fn("cpu", 1.0)
    engine_gpu = engine_fn("cuda:0", 0.991)
    compare_fn = ppe.utils.comparer.get_default_comparer(rtol=1e-2, atol=1e-2)
    comp = ppe.utils.comparer.Comparer(outputs=["a"], compare_fn=compare_fn)
    train_1 = list(torch.ones(10) for _ in range(10))
    train_2 = list(torch.ones(10) for _ in range(10))
    if engine_fn is _get_trainer_with_evaluator:
        eval_1 = list(torch.ones(10) for _ in range(10))
        eval_2 = list(torch.ones(10) for _ in range(10))
        comp.add_engine("cpu", engine_cpu, train_1, eval_1)
        comp.add_engine("gpu", engine_gpu, train_2, eval_2)
    else:
        comp.add_engine("cpu", engine_cpu, train_1)
        comp.add_engine("gpu", engine_gpu, train_2)
    comp.compare()


def test_comparer_incompat_trigger():
    model_cpu = Model("cpu", 1.0)
    optimizer_cpu = torch.optim.SGD(model_cpu.parameters(), lr=1.0)
    trainer_cpu = ppe.engine.create_trainer(
        model_cpu, optimizer_cpu, 1, device="cpu",
    )

    model_gpu = Model("cuda:0", 1.0)
    optimizer_gpu = torch.optim.SGD(model_gpu.parameters(), lr=1.0)
    trainer_gpu = ppe.engine.create_trainer(
        model_gpu, optimizer_gpu, 1, device="cuda:0",
        stop_trigger=(1, "iteration"),
    )

    comp = ppe.utils.comparer.Comparer(outputs=["a"])
    train_1 = list(torch.ones(10) for _ in range(10))
    train_2 = list(torch.ones(10) for _ in range(10))
    comp.add_engine("cpu", trainer_cpu, train_1)
    comp.add_engine("gpu", trainer_gpu, train_2)
    with pytest.raises(ValueError):
        comp.compare()


@pytest.mark.parametrize("engine_fn", [
    _get_trainer, _get_evaluator, _get_trainer_with_evaluator])
def test_compare_concurrency(engine_fn):
    engine_cpu = engine_fn("cpu", 1.0)
    engine_gpu = engine_fn("cuda:0", 1.0)
    comp = ppe.utils.comparer.Comparer(outputs=["a"], concurrency=1)
    train_1 = list(torch.ones(10) for _ in range(10))
    train_2 = list(torch.ones(10) for _ in range(10))
    if engine_fn is _get_trainer_with_evaluator:
        eval_1 = list(torch.ones(10) for _ in range(10))
        eval_2 = list(torch.ones(10) for _ in range(10))
        comp.add_engine("cpu", engine_cpu, train_1, eval_1)
        comp.add_engine("gpu", engine_gpu, train_2, eval_2)
    else:
        comp.add_engine("cpu", engine_cpu, train_1)
        comp.add_engine("gpu", engine_gpu, train_2)
    comp.compare()


@pytest.mark.parametrize("engine_fn", [
    _get_trainer, _get_evaluator, _get_trainer_with_evaluator])
def test_compare_concurrency_wrong(engine_fn):
    engine_cpu = engine_fn("cpu", 1.0)
    engine_gpu = engine_fn("cuda:0", 0.5)
    comp = ppe.utils.comparer.Comparer(outputs=["a"], concurrency=1)
    train_1 = list(torch.ones(10) for _ in range(10))
    train_2 = list(torch.ones(10) for _ in range(10))
    if engine_fn is _get_trainer_with_evaluator:
        eval_1 = list(torch.ones(10) for _ in range(10))
        eval_2 = list(torch.ones(10) for _ in range(10))
        comp.add_engine("cpu", engine_cpu, train_1, eval_1)
        comp.add_engine("gpu", engine_gpu, train_2, eval_2)
    else:
        comp.add_engine("cpu", engine_cpu, train_1)
        comp.add_engine("gpu", engine_gpu, train_2)
    with pytest.raises(AssertionError):
        comp.compare()


class ModelForComparer(torch.nn.Module):
    def __init__(self, *args):
        super().__init__()
        self.model = torch.nn.Sequential(
            torch.nn.Conv2d(10, 10, 3, 3),
            torch.nn.ReLU(),
            torch.nn.BatchNorm2d(10),
            torch.nn.Linear(3, 1),
        )

    def forward(self, x):
        # The return value depends only on the argument.
        return {"y": x.sum()}


def test_model_comparer():
    model_cpu = ModelForComparer()
    model_gpu = ModelForComparer()
    # Make the models to have the same initial weights
    model_gpu.load_state_dict(model_cpu.state_dict())
    ppe.to(model_gpu, device="cuda:0")

    optimizer_cpu = torch.optim.SGD(model_cpu.parameters(), lr=0.01)
    trainer_cpu = ppe.engine.create_trainer(
        model_cpu, optimizer_cpu, 1, device="cpu")
    optimizer_gpu = torch.optim.SGD(model_gpu.parameters(), lr=0.01)
    trainer_gpu = ppe.engine.create_trainer(
        model_gpu, optimizer_gpu, 1, device="cuda:0")
    compare_fn = ppe.utils.comparer.get_default_comparer(rtol=1e-2, atol=1e-2)
    comp = ppe.utils.comparer.Comparer(compare_fn=compare_fn, params=True)

    train_1 = list(torch.ones(2, 10, 10, 10) for _ in range(10))
    train_2 = list(torch.ones(2, 10, 10, 10) for _ in range(10))
    comp.add_engine("cpu", trainer_cpu, train_1)
    comp.add_engine("gpu", trainer_gpu, train_2)
    comp.compare()


def test_model_comparer_invalid():
    model_cpu = ModelForComparer()
    model_gpu = ModelForComparer()
    ppe.to(model_gpu, device="cuda:0")

    optimizer_cpu = torch.optim.SGD(model_cpu.parameters(), lr=0.01)
    trainer_cpu = ppe.engine.create_trainer(
        model_cpu, optimizer_cpu, 1, device="cpu")
    optimizer_gpu = torch.optim.SGD(model_gpu.parameters(), lr=0.01)
    trainer_gpu = ppe.engine.create_trainer(
        model_gpu, optimizer_gpu, 1, device="cuda:0")
    compare_fn = ppe.utils.comparer.get_default_comparer(rtol=1e-2, atol=1e-2)
    comp = ppe.utils.comparer.Comparer(compare_fn=compare_fn, params=True)

    train_1 = list(torch.ones(2, 10, 10, 10) for _ in range(10))
    train_2 = list(torch.ones(2, 10, 10, 10) for _ in range(10))
    comp.add_engine("cpu", trainer_cpu, train_1)
    comp.add_engine("gpu", trainer_gpu, train_2)
    with pytest.raises(AssertionError):
        comp.compare()
