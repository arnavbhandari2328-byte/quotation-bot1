web: gunicorn app:app -w 1 -k gthread --threads 8 --worker-tmp-dir /dev/shm --max-requests 25 --max-requests-jitter 10 --timeout 120
