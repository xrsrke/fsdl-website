from typing import Iterable, List, Tuple, Annotated, Optional, Generator, Dict, Callable, Any
from dataclasses import dataclass
from contextlib import contextmanager
from queue import Queue
from threading import Thread
import time

import torch
from torch import nn


@dataclass
class QueueOutput:
    task: Callable
    output: Any
    is_done: bool = False


def wait_and_execute(device: torch.device, in_queue: Queue, out_queue: Queue):
    """Wait for a task and execute it."""
    while True:
        task = in_queue.get()

        if task.is_done is True:
            break

        try:
            output = task.compute()
        except Exception:
            raise RuntimeError(f"Failed to execute a task on {device}")
            out_queue.put(QueueOutput(task=task, output=None, is_done=False))
            continue

        out_queue.put(QueueOutput(task=task, output=output, is_done=True))


@contextmanager
def spawn_worker(
    devices: List[torch.device],
) -> Generator[
    Tuple[
        Annotated[List[Queue], "A list of tasks to be executed"],
        Annotated[List[Queue], "A list of tasks has been executed"],
    ],
    None,
    None,
]:
    """Spawn new worker threads."""
    in_queues: List[Queue] = []
    out_queues: List[Queue] = []

    workers: Dict[torch.device, Tuple[Queue, Queue]] = {}

    for device in devices:
        try:
            in_queue, out_queue = workers[device]
        except KeyError:
            in_queue = Queue()
            out_queue = Queue()
            workers[device] = (in_queue, out_queue)

            thread = Thread(target=wait_and_execute, args=(device, in_queue, out_queue), daemon=True)
            thread.start()

        in_queues.append(in_queue)
        out_queues.append(out_queue)

    yield (in_queues, out_queues)


class DetermisticScheduler:
    """
    torchgpipe: On-the-fly Pipeline Parallelism for Training Giant Models
    https://arxiv.org/abs/2004.09910

    Section 3.2.1: Forward Dependency: Deterministic Clock-cycle
    """

    def generate(
        self,
        n_microbatches: int,
        n_partitions: int,
    ) -> Iterable[List[Tuple[Annotated[int, "batch_idx"], Annotated[int, "partition_idx"]]]]:
        assert (
            n_microbatches > 0
        ), "The number of microbatches must be \
            greater than 0"
        assert (
            n_partitions > 0
        ), "The number of partitions must be \
            greater than 0"

        n_partitions = n_partitions
        n_microbatches = n_microbatches
        n_clock_cycles = n_partitions + n_microbatches - 1

        for clock_idx in range(n_clock_cycles):
            start_partrition = max(clock_idx + 1 - n_microbatches, 0)
            end_partition = min(clock_idx + 1, n_partitions)

            tasks = []
            for partition_idx in range(start_partrition, end_partition):
                microbatch_idx = clock_idx - partition_idx
                tasks.append((microbatch_idx, partition_idx))

            yield tasks


class Task:
    def __init__(self, compute: Callable[[], torch.Tensor], is_done: bool = False):
        self._compute = compute
        self.is_done = is_done

    def compute(self) -> torch.Tensor:
        return self._compute()


class Pipeline:
    """A base class for pipeline."""

    def __init__(
        self,
        batches: List[torch.Tensor],
        partitions: List[nn.Sequential],
        devices: Optional[List[torch.device]] = None,
        scheduler=DetermisticScheduler(),
    ) -> None:
        """Initialize the pipeline.

        Args:
            batches (List[Batch]): A list of micro-batches.
            partitions (List[nn.Sequential]): A partitioned model.
            devices (Optional[List[torch.device]], optional): A list of devices. Defaults to None.
            scheduler (BaseScheduler, optional): _description_. Defaults to DetermisticScheduler().
        """
        self.batches = batches
        self.partitions = partitions
        self.devices = devices
        self.scheduler = scheduler

    def fit(self):
        batches = self.batches
        partitions = self.partitions
        devices = self.devices
        scheduler = self.scheduler

        n_batches = len(batches)
        n_partitions = len(partitions)

        with spawn_worker(devices) as (in_queues, out_queues):
            for schedule in scheduler.generate(n_batches, n_partitions):
                # _depend(schedule)
                self._compute(schedule, in_queues, out_queues)

    # def _depend(self, schedule: List[Tuple[int, int]]):
    #     """Enforce the dependency between batches and partitions."""
    #     batches = batches

    #     for microbatch_idx, partition_idx in schedule:
    #         if microbatch_idx != 0:
    #             create_backward_dependency(batches[microbatch_idx - 1], batches[microbatch_idx])

    def _compute(self, schedule: List[Tuple[int, int]], in_queues: List[Queue], out_queues: List[Queue]):
        """Compute the partitions."""
        batches = self.batches
        partitions = self.partitions

        for microbatch_idx, partition_idx in schedule:
            batch = batches[microbatch_idx]
            partrition = partitions[partition_idx]

            def compute(batch, partrition):
                def wrapper():
                    return partrition(batch)

                return wrapper

            task = Task(compute=compute(batch, partrition))
            in_queues[partition_idx].put(task)

        for microbatch_idx, partition_idx in schedule:
            queue_output = out_queues[partition_idx].get()
            task, output = queue_output.task, queue_output.output

            # put the output back to the batch
            batches[microbatch_idx] = output


def create_non_parallel_model(partitions):
    non_parallel_model = nn.Sequential(*[AddOne(partition_idx=x, is_logging=False) for x in range(N_PARTITIONS)])
    for non_parallel_layer, original_partition in zip(non_parallel_model, partitions):
        non_parallel_layer.load_state_dict(original_partition[0].state_dict())
    return non_parallel_model


def create_non_parallel_batch(batch):
    non_parallel_batch = batch.detach().clone()
    non_parallel_batch.grad = None
    return non_parallel_batch


if __name__ == "__main__":
    N_MICROBATCHES = 3
    N_PARTITIONS = 2

    forward_timeline = []
    backward_timeline = []

    def backward_hook(module, grad_input, grad_output):
        backward_timeline.append((module.microbatch_idx - 1, module.partition_idx))
        module.microbatch_idx -= 1

    class AddOne(nn.Module):
        def __init__(self, partition_idx, is_logging):
            super().__init__()
            self.microbatch_idx = 0
            self.partition_idx = partition_idx
            self.is_logging = is_logging
            self.net = nn.Linear(1, 1)
            self.register_backward_hook(backward_hook)

        def forward(self, x):
            if self.is_logging:
                time.sleep(0.5)
                forward_timeline.append((self.microbatch_idx, self.partition_idx))
                self.microbatch_idx += 1

            return self.net(x)

    forward_timeline = forward_timeline
    backward_timeline = backward_timeline
    batch = torch.arange(0, N_MICROBATCHES, dtype=torch.float32, requires_grad=True)
    microbatches = [x.unsqueeze(0) for x in batch.unbind()]
    partitions = [nn.Sequential(AddOne(partition_idx=x, is_logging=True)) for x in range(N_PARTITIONS)]
    devices = [torch.device("cpu") for _ in range(N_PARTITIONS)]
    non_parallel_model = create_non_parallel_model(partitions)
    non_parallel_batch = create_non_parallel_batch(batch)

    results = {}

    def loss_func(x):
        return x.mean()

    pipeline = Pipeline(microbatches, partitions, devices)

    assert pipeline.batches == microbatches
    assert pipeline.partitions == partitions

    pipeline.fit()

    assert forward_timeline == [(0, 0), (1, 0), (0, 1), (2, 0), (1, 1), (2, 1)]

    outputs = microbatches
    non_parallel_outputs = [non_parallel_model(x.unsqueeze(0)) for x in non_parallel_batch.unbind()]

    for x, y in zip(outputs, non_parallel_outputs):
        assert torch.allclose(x, y)

    for x in outputs:
        loss = loss_func(x)
        loss.backward()

    assert backward_timeline == [(2, 1), (2, 0), (1, 1), (1, 0), (0, 1), (0, 0)] or backward_timeline == [
        (2, 1),
        (2, 0),
        (1, 1),
        (0, 1),
        (1, 0),
        (0, 0),
    ]

    for x in non_parallel_outputs:
        loss = loss_func(x)
        loss.backward()

    assert batch.grad is not None

    for partition in partitions:
        for param in partition.parameters():
            assert param.grad is not None

    for partition_idx in range(N_PARTITIONS):
        for w1, w2 in zip(partitions[partition_idx].parameters(), non_parallel_model[partition_idx].parameters()):
            assert torch.allclose(w1.grad, w2.grad)
