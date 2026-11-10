import itertools
import threading
from queue import PriorityQueue, Empty
from concurrent.futures import Future


class PriorityPool:
    def __init__(self, workers: int = 4):
        self.queue = PriorityQueue()
        self.counter = itertools.count()
        self.lock = threading.Lock()
        self.closed = False
        self.running_count = 0
        self.running_lock = threading.Lock()

        self.workers = [
            threading.Thread(
                target=self._worker,
                name=f"PriorityWorker-{i}",
            )
            for i in range(workers)
        ]

        for worker in self.workers:
            worker.start()

    @property
    def pending(self) -> int:
        return self.queue.qsize()

    @property
    def running(self) -> int:
        with self.running_lock:
            return self.running_count

    @property
    def total(self) -> int:
        return self.pending + self.running

    def submit(self, fn, *args, priority=10, **kwargs) -> Future:
        future = Future()

        with self.lock:
            if self.closed:
                raise RuntimeError("Pool is shut down")

            self.queue.put((
                priority,
                next(self.counter),
                future,
                fn,
                args,
                kwargs,
            ))

        return future

    def _worker(self):
        while True:
            priority, order, future, fn, args, kwargs = self.queue.get()

            try:
                if future is None:
                    return

                if future.set_running_or_notify_cancel():
                    with self.running_lock:
                        self.running_count += 1

                    try:
                        result = fn(*args, **kwargs)
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
                    finally:
                        with self.running_lock:
                            self.running_count -= 1
            finally:
                self.queue.task_done()

    def shutdown(self, wait=True, cancel_futures=False):
        with self.lock:
            if not self.closed:
                self.closed = True

                if cancel_futures:
                    while True:
                        try:
                            item = self.queue.get_nowait()
                        except Empty:
                            break

                        item[2].cancel()
                        self.queue.task_done()

                for _ in self.workers:
                    self.queue.put((
                        float("inf"),
                        next(self.counter),
                        None, None, (), {},
                    ))

        if wait:
            for worker in self.workers:
                worker.join()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
