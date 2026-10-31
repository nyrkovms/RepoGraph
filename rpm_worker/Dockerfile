# rpm-worker (SPEC-MULTI-ECOSYSTEM.md §3.2): libsolv and libmodulemd have no wheels, so the worker runs on Rocky Linux
# 9's system Python with the distribution's bindings.
FROM rockylinux:9

RUN dnf -y install python3 python3-pip python3-solv python3-libmodulemd python3-gobject-base createrepo_c openssl \
    && dnf clean all

WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY rpm_worker ./rpm_worker
COPY tests ./tests

ENV RPM_CACHE_DIR=/var/cache/rpm-worker PYTHONUNBUFFERED=1
VOLUME /var/cache/rpm-worker
# Two workers: the daily batch (detect_changes, sync_repo) never takes the slots of what a person waits on.
CMD ["sh", "-c", "python3 -m celery -A rpm_worker.worker:app worker -Q rpm-batch -c 1 -n batch@%h --loglevel=INFO & exec python3 -m celery -A rpm_worker.worker:app worker -Q rpm -c 2 -n interactive@%h --loglevel=INFO"]
