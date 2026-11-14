import os

from celery import Celery
from celery.schedules import crontab

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


# UTC schedule. Keep exactly one beat service for this application.
celery_app.conf.timezone = "UTC"
celery_app.conf.beat_schedule = {
    "aws-on-demand-prices-daily": {
        "task": "spulti.market.collect",
        "schedule": crontab(minute=50, hour=0),
        "args": ("AWS", "on_demand"),
    },
    "gcp-on-demand-prices-daily": {
        "task": "spulti.market.collect",
        "schedule": crontab(minute=5, hour=1),
        "args": ("GCP", "on_demand"),
    },
    "reconcile-machines": {
        "task": "spulti.reconcile_machines",
        "schedule": 15.0,
    },
    "aws-spot-prices-every-15-min": {
        "task": "spulti.market.collect",
        "schedule": crontab(minute="*/15"),
        "args": ("AWS", "prices"),
    },
    "gcp-spot-prices-every-15-min": {
        "task": "spulti.market.collect",
        "schedule": crontab(minute="5,20,35,50"),
        "args": ("GCP", "prices"),
    },
    "aws-spot-risks-daily": {
        "task": "spulti.market.collect",
        "schedule": crontab(minute=20, hour=0),
        "args": ("AWS", "risks"),
    },
    "gcp-spot-risks-daily": {
        "task": "spulti.market.collect",
        "schedule": crontab(minute=35, hour=0),
        "args": ("GCP", "risks"),
    },
}
