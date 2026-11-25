#!/bin/sh
set -eu
umask 077
python source_drain.py --state /review/control.sqlite init >/dev/null
exec gunicorn -c python:gunicorn_source_drain -w 4 -b 0.0.0.0:8000 \
    --timeout 30 --graceful-timeout 15 rehearsal_app:app
