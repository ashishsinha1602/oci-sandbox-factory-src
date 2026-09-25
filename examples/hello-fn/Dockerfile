# Plain Python image with the Fn FDK on top: builds for arm64 (the sandbox's
# GENERIC_ARM function shape) without depending on fnproject base images.
FROM python:3.11-slim
WORKDIR /function
COPY requirements.txt func.py /function/
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONPATH=/function
ENTRYPOINT ["python", "-m", "fdk", "/function/func.py", "handler"]
