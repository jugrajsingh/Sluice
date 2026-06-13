ARG BASE=sluice-worker-base:test
FROM ${BASE}
COPY handler.py /app/handler.py
ENV WORKER__HANDLER=handler:SegHandler
CMD ["python", "-m", "sluice_worker.run"]
