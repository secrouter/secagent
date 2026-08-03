"""Background worker that consumes jobs from a Redis queue."""

import os

import redis

from common.models import Job

QUEUE_NAME = os.environ["QUEUE_NAME"]


def process(job: Job) -> None:
    print(f"processing {job.id}")


def run() -> None:
    client = redis.Redis()
    while True:
        item = client.blpop(QUEUE_NAME)
        if item is None:
            break


if __name__ == "__main__":
    run()
