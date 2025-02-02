import logging
import time
import threading
import random
from queue import PriorityQueue
from typing import Union

from prometheus_client import Gauge, Counter, Histogram

logger = logging.getLogger(__name__)


class Message:
    def __init__(self, body: str, send_at: float, queued_at: float, attempt: int = 0):
        self.body = body
        self.send_at = send_at
        self.queued_at = queued_at
        self.attempt = attempt

    def make_next_attempt(self, delay: Union[int, float]):
        send_at = time.monotonic() + delay  # schedule send at now + delay sec
        return Message(self.body, send_at, self.queued_at, self.attempt + 1)

    def __lt__(self, other: "Message"):
        return self.send_at < other.send_at

    def __repr__(self):
        body = f"{self.body[:17]}..." if len(self.body) > 20 else self.body
        delta = self.send_at - time.monotonic()
        return f"Message({body}, attempt={self.attempt}, send_at=now+{delta}s)"


class SendQueue:
    def __init__(self, maxsize: int):
        self._queue = PriorityQueue()
        self._maxsize = maxsize

    def __len__(self):
        return self._queue.qsize()

    def get(self):
        return self._queue.get()

    def put(self, msg):
        return self._queue.put_nowait(msg)

    def accept(self, now: float, body: str):
        message = Message(body, send_at=now, queued_at=now)
        if self._queue.qsize() < self._maxsize:
            self._queue.put_nowait(message)
            return True
        return False


# https://en.wikipedia.org/wiki/Token_bucket
class RateLimiter:
    def __init__(self, bucket_size: int, window_secs: int):
        self.bucket_size = bucket_size
        self.last_tick = time.monotonic()
        self.available = bucket_size
        self.window_secs = window_secs

    def is_allowed(self):
        now = time.monotonic()
        passed = now - self.last_tick
        self.last_tick = now
        self.available += passed * self.bucket_size / self.window_secs
        if self.available > self.bucket_size:
            self.available = self.bucket_size
        return self.available >= 1


class SenderThread(threading.Thread):
    def __init__(self, queue, registry):
        super().__init__(name="Sender")
        self.setDaemon(True)
        self.queue = queue
        self.registry = registry
        self.rate_limiter = RateLimiter(10, 5)  # 10 msg per 5s
        queue_size = Gauge(
            "sender_queue_size", "Send queue size in messages", registry=registry
        )
        queue_size.set_function(lambda: len(queue))
        self.rate_limited = Counter(
            "sender_rate_limited", "Rate limited events count", registry=registry
        )
        self.send_time_histogram = Histogram(
            "sender_send_time_seconds", "Message sending time", registry=registry
        )
        self.lag_histogram = Histogram(
            "sender_lag_seconds",
            "Sent message lag",
            registry=registry,
            buckets=(0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, float("inf")),
        )
        self.attempt_histogram = Histogram(
            "sender_failed_attempts",
            "Failed attempts taken before successful send",
            registry=registry,
            buckets=(0, 1, 2, 3, 4, 5, 7, 10, 15, 20, 30, 50, 70, 100, float("inf")),
        )

    def run(self):
        while True:
            try:
                item = self.queue.get()
                now = time.monotonic()
                logger.debug("Dequeued %s at queued_at%+gs", item, now - item.queued_at)
                if item.send_at > now:
                    logger.debug("Too early, enqueued %s back", item)
                    self.queue.put(item)
                    continue
                # if not self.rate_limiter.is_allowed():
                #     self.rate_limited.inc()
                #     continue
                self.try_send(item)
            except Exception as e:
                logger.error("Unhandled exception in consumer loop", e)

            time.sleep(0.1)

    def try_send(self, item: Message):
        logger.debug("Trying to send %s", item)
        try:
            if self.send(item):
                logger.info("Sent %s", item)
            else:
                # Send failed, schedule retry
                delay = (
                    item.attempt * 1.5 if item.attempt > 0 else 0.75
                ) + 0.5 * random.random()
                item = item.make_next_attempt(delay)
                self.queue.put(item)
                logging.info("Postponed message %s", item)
        except Exception as e:
            logger.error("Exception in try_send", e)

    def send(self, item: Message):
        # /dev/null is a proper place for spam
        if not self.rate_limiter.is_allowed():
            self.rate_limited.inc()
            return False
        self.rate_limiter.available -= 1
        lag = time.monotonic() - item.queued_at
        logger.debug("Sending %s with lag %gs", item, lag)
        send_time = 0.2 + 0.3 * random.random()
        time.sleep(send_time)
        self.lag_histogram.observe(lag)
        self.send_time_histogram.observe(send_time)
        self.attempt_histogram.observe(item.attempt)
        return True
