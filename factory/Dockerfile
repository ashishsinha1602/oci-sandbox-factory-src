# The Sandbox Factory worker, for running inside OCI as a Container Instance.
# Build from the repository root:  docker buildx build --platform linux/arm64 -f factory/Dockerfile .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir oci oracledb requests kafka-python-ng
WORKDIR /app
COPY factory/sandbox_factory.py factory/worker.py factory/controldb.py factory/oci_build.py factory/app_templates.py factory/prompts.py factory/bootstrap.py factory/apex_customize.py factory/apex_home_inject.py factory/apex_export_customized.sql factory/roll_workers.py factory/
COPY factory/apex_home/ factory/apex_home/
COPY factory/prompts/ factory/prompts/
COPY stacks/sandbox/ stacks/sandbox/
# the end-to-end suite and the example workloads, so a test run happens inside OCI too
COPY tests/e2e.py tests/
COPY examples/ examples/
ENV SBX_WORKER_KIND=oci SBX_BUILD_MODE=kaniko PYTHONUNBUFFERED=1
WORKDIR /app/factory
CMD ["python", "worker.py"]
