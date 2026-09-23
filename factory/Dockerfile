# The Sandbox Factory worker, for running inside OCI as a Container Instance.
# Build from the repository root:  docker buildx build --platform linux/arm64 -f factory/Dockerfile .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir oci oracledb
WORKDIR /app
COPY factory/sandbox_factory.py factory/worker.py factory/controldb.py factory/oci_build.py factory/
COPY stacks/sandbox/ stacks/sandbox/
ENV SBX_WORKER_KIND=oci SBX_BUILD_MODE=kaniko PYTHONUNBUFFERED=1
WORKDIR /app/factory
CMD ["python", "worker.py"]
