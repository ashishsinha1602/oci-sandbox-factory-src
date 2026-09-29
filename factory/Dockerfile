# The Sandbox Factory worker, for running inside OCI as a Container Instance.
# Published as <region>.ocir.io/<ns>/sandbox-factory/worker:release by factory/release_image.py
# (built inside OCI, linux/amd64 for the E4 workers).
# Copyright 2026 Ashish Sinha. Licensed under the Apache License, Version 2.0; see LICENSE.
FROM python:3.12-slim AS build
WORKDIR /app
COPY factory/*.py factory/*.txt factory/apex_export_customized.sql factory/
COPY factory/prompts/ factory/prompts/
COPY tests/e2e.py tests/
# ship compiled code only: bytecode beside where each module was, no sources
RUN python -m compileall -b -q factory tests \
 && find factory tests -name "*.py" -delete \
 && find factory tests -name "__pycache__" -prune -exec rm -rf {} +

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir oci==2.187.1 oracledb==26.0.1 requests==2.34.2 kafka-python-ng==2.2.3 pillow==12.3.0
WORKDIR /app
COPY LICENSE /app/LICENSE
COPY --from=build /app/factory/ factory/
COPY --from=build /app/tests/ tests/
# the page's server code: it is in every install's APEX application anyway, and the suite calls it
COPY factory/apex_home/ajax.plsql factory/apex_home/ajax.plsql
COPY stacks/sandbox/ stacks/sandbox/
COPY examples/ examples/
ENV SBX_WORKER_KIND=oci SBX_BUILD_MODE=kaniko PYTHONUNBUFFERED=1
WORKDIR /app/factory
CMD ["python", "worker.pyc"]
