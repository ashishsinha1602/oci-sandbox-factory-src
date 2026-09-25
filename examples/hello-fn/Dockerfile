FROM fnproject/python:3.11-dev AS build
WORKDIR /function
ADD requirements.txt /function/
RUN pip3 install --target /python/ --no-cache --no-cache-dir -r requirements.txt && rm -rf /root/.cache
ADD func.py /function/

FROM fnproject/python:3.11
WORKDIR /function
COPY --from=build /python /python
COPY --from=build /function /function
ENV PYTHONPATH=/function:/python
ENTRYPOINT ["/python/bin/fdk", "/function/func.py", "handler"]
