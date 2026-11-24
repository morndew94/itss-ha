ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Bump ITSS_BUILD_VERSION on every release to force Docker to invalidate cached app layers
ENV ITSS_BUILD_VERSION="2.6.1"
LABEL itss.version="2.6.1"

COPY app /app/app
COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh

EXPOSE 8099

CMD ["/app/run.sh"]

