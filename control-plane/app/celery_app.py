import os

from celery import Celery

celery_app = Celery(
    "spulti",
    broker=os.getenv(
        "CELERY_BROKER_URL",
        "redis://redis:6379/0",
    ),
    backend=os.getenv(
        "CELERY_RESULT_BACKEND",
        "redis://redis:6379/1",
    ),
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1,
)
