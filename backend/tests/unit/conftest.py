import os

# Importing Celery tasks configures logging; outside containers /var/log/packagehub is not writable.
os.environ.setdefault("LOG_TO_FILE", "false")
