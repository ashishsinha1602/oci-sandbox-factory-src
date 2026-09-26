# The Sandbox Factory worker, for running inside OCI as a Container Instance.
# Published as <region>.ocir.io/<ns>/sandbox-factory/worker:release by factory/release_image.py
# (built inside OCI, linux/amd64 for the E4 workers).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir oci oracledb requests kafka-python-ng
WORKDIR /app
# all of the factory code, so a new module can never be left out of the image
COPY factory/*.py factory/*.txt factory/apex_export_customized.sql factory/
COPY factory/apex_home/ factory/apex_home/
COPY factory/prompts/ factory/prompts/
COPY stacks/sandbox/ stacks/sandbox/
# the end-to-end suite and the example workloads, so a test run happens inside OCI too
COPY tests/e2e.py tests/
COPY examples/ examples/
ENV SBX_WORKER_KIND=oci SBX_BUILD_MODE=kaniko PYTHONUNBUFFERED=1
WORKDIR /app/factory
CMD ["python", "worker.py"]
