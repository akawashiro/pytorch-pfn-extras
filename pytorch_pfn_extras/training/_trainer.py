import queue
import time
from typing import Any, Iterable, List, Optional, Tuple, Union, TYPE_CHECKING

import torch

import pytorch_pfn_extras.engine
import pytorch_pfn_extras.training
from pytorch_pfn_extras.training import extension as extension
from pytorch_pfn_extras.training import trigger as trigger_module
from pytorch_pfn_extras.training import manager as manager_module
import pytorch_pfn_extras.reporting as reporting
from pytorch_pfn_extras.profiler import record

from pytorch_pfn_extras.training.trigger import Trigger, TriggerLike

if TYPE_CHECKING:
    import pytorch_pfn_extras.handler as handler_module
    from pytorch_pfn_extras.training._evaluator import _Evaluator
    from pytorch_pfn_extras.profiler._time_summary import _ReportNotification


class _Trainer(pytorch_pfn_extras.engine._Engine):
    def __init__(
            self,
            handler: 'handler_module.BaseHandler',
            *,
            evaluator: Optional[Union['_Evaluator', Tuple['_Evaluator', TriggerLike]]],
            **kwargs: Any,
    ):
        super().__init__(handler, **kwargs)
        if isinstance(evaluator, tuple):
            self.evaluator: Optional['_Evaluator'] = None
            self.evaluator, trigger = evaluator
            self.evaluator_trigger = trigger_module.get_trigger(trigger)
        else:
            self.evaluator = evaluator
            self.evaluator_trigger = trigger_module.get_trigger((1, 'epoch'))
        self.val_loader = None

    @property
    def epoch(self) -> int:
        return self.manager.epoch

    @property
    def epoch_detail(self) -> float:
        return self.manager.epoch_detail

    @property
    def iteration(self) -> int:
        return self.manager.iteration

    @property
    def is_before_training(self) -> bool:
        return self.manager.iteration == 0

    @property
    def stop_trigger(self) -> Trigger:
        return self._stop_trigger

    @stop_trigger.setter
    def stop_trigger(self, trigger: Trigger) -> None:
        self._stop_trigger = trigger

    def get_optimizer(self, name: str) -> torch.optim.Optimizer:
        return self.manager.optimizers[name]

    def set_optimizer(self, name: str, optimizer: torch.optim.Optimizer) -> None:
        self.manager.optimizers[name] = optimizer

    def is_epoch_last_iter(self, idx: int) -> bool:
        return (idx + 1) == (self.manager._iters_per_epoch)

    def _complete_step(
            self,
            idx: int,
            outs: Any,
            *,
            is_deferred: bool = False,
    ) -> None:
        self._deferred = False  # notify that the function was called
        c_idx = self._idxs.get()
        # Asure that iterations complete in order
        if c_idx != idx:
            raise RuntimeError(
                'Completed a not expected iteration. '
                '{} was expected but completion of {} happened'.format(
                    c_idx, idx)
            )
        x = self._inputs.get()
        begin = self._times.get()
        observed = self._observed.get()
        (
            record_iteration,
            record_run_iteration,
            record_train_step,
        ) = self._profile_records.get()
        # If the iteration was not deferred this is still under the
        # `manager.run_iteration` scope
        # Change the current reporter observation
        # To be the one to be completed
        if is_deferred:
            # Complete profiler record of `train_step`
            record_train_step.complete()
            # We want to report the previously obtained values in `train_step`
            cm_iter = self.manager.complete_iteration(observation=observed)
            cm_iter.__enter__()
        else:
            reporting.get_current_reporter().observation = observed
            self.manager.observation = observed
        self.handler.train_post_step(self, idx, x, outs)
        reporting.report({"elapsed_time": time.time() - begin})
        if is_deferred:
            cm_iter.__exit__(None, None, None)
            # Complete profiler record of `run_iteration` and iteration
            record_run_iteration.complete()
            record_iteration.complete()

    def _run_evaluator(self) -> None:
        assert self.evaluator is not None
        if self._val_loader is None:
            raise ValueError('"val_loader" is not given.')
        self.evaluator.handler.train_validation_begin(self, self.evaluator)
        self.evaluator.run(self._val_loader, eval_len=self._eval_len)
        self.evaluator.handler.train_validation_end(self, self.evaluator)

    def run(self,  # type: ignore[override]
            train_loader: Iterable[Any],
            val_loader: Optional[Iterable[Any]] = None,
            *,
            train_len: Optional[int] = None,
            eval_len: Optional[int] = None) -> None:
        """Executes the training loop.

        Args:
            train_loader (torch.utils.data.DataLoader):
                A data loader for training.
            val_loader (torch.utils.data.DataLoader, optional):
                A data loader passed to ``Evaluator.run()``.
            train_len (int, optional):
                The number of iterations per one training epoch. The default
                value is inferred from the size of training data loader.
            eval_len (int, optional):
                The number of iterations per one evaluation epoch, passed
                to ``Evaluator.run()``

        .. seealso::
            - :meth:`pytorch_pfn_extras.training._evaluator._Evaluator`
        """
        if train_len is None:
            train_len = len(train_loader)  # type: ignore[arg-type]

        self._val_loader = val_loader
        self._eval_len = eval_len

        class _EvaluatorExt:
            def __init__(self, trainer: '_Trainer') -> None:
                self.name = 'Evaluator'
                self.needs_model_state = True
                self._trainer = trainer

            def __call__(self, manager: manager_module._BaseExtensionsManager) -> None:
                self._trainer._run_evaluator()

        if self._manager is None:
            self._setup_manager(train_len)
            assert isinstance(self._manager, manager_module._BaseExtensionsManager)
            if self.evaluator is not None:  # type: ignore[unreachable]
                # Register the evaluator as an extension to the manager
                # To be triggered with the correct timing
                self._manager.extend(
                    _EvaluatorExt(self),
                    trigger=self.evaluator_trigger,
                    priority=extension.PRIORITY_WRITER,
                )
            self.handler.train_setup(self, train_loader)
            if self.evaluator is not None:
                self.evaluator.handler.eval_setup(self.evaluator, val_loader)

        while not self.manager.stop_trigger:
            self.handler.train_epoch_begin(self, train_loader)

            # When iterations are completed in the callback
            # This is needed to avoid being constantly passing parameters
            self._idxs: 'queue.Queue[int]' = queue.Queue()
            self._inputs: 'queue.Queue[Any]' = queue.Queue()
            self._times: 'queue.Queue[float]' = queue.Queue()
            self._observed: 'queue.Queue[reporting.Observation]' = queue.Queue()
            # Iterator must be created after `train_epoch_begin` as it may be
            #  using a DistributedSampler.
            loader_iter = iter(train_loader)
            self._profile_records: 'queue.Queue[List[_ReportNotification]]' \
                = queue.Queue()
            for idx in range(train_len):
                with record(
                    "pytorch_pfn_extras.training.Trainer:iteration",
                    use_cuda=torch.cuda.is_available()
                ) as ntf0:
                    try:
                        with record(
                            "pytorch_pfn_extras.training.Trainer:get_data"
                        ):
                            x = next(loader_iter)
                    except StopIteration:
                        loader_iter = iter(train_loader)
                        with record(
                            "pytorch_pfn_extras.training.Trainer:get_data"
                        ):
                            x = next(loader_iter)
                    begin = time.time()
                    self._idxs.put(idx)
                    self._inputs.put(x)
                    self._times.put(begin)
                    self._deferred = True
                    with record(
                        "pytorch_pfn_extras.training.Trainer:run_iteration",
                        use_cuda=torch.cuda.is_available()
                    ) as ntf1, \
                            self.manager.run_iteration() as iter_notifier:
                        self._observed.put(self.manager.observation)
                        with record(
                            "pytorch_pfn_extras.training.Trainer:train_step",
                            use_cuda=torch.cuda.is_available(),
                        ) as ntf2:
                            self._profile_records.put([ntf0, ntf1, ntf2])
                            self.handler.train_step(
                                self, idx, x, complete_fn=self._complete_step)
                            # Check if the callback was called
                            if self._deferred:
                                # The iteration will be completed later
                                ntf0.defer()
                                ntf1.defer()
                                ntf2.defer()
                                iter_notifier.defer()
                    # In some cases, DataLoaders are continuos
                    # And will keep yielding results even if the epoch
                    # is completed. We forcefully exit at the end of
                    # every epoch
                    if (
                        self.is_epoch_last_iter(idx)
                        or self.manager.stop_trigger
                    ):
                        break
            # In handlers that support a completely Async model train_epoch_end
            # Will take care of completing pending work
            self.handler.train_epoch_end(self)