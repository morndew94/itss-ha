# HA 공식 amd64 Python 베이스 이미지 (날짜 태그로 고정하여 재빌드 재현성 확보)
# Supervisor 2026.04 이후 BUILD_FROM 기본값이 주입되지 않으므로 FROM을 직접 명시
FROM ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.23-2026.10.3

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# 릴리스마다 ITSS_BUILD_VERSION을 올려 Docker 앱 레이어 캐시를 무효화
ENV ITSS_BUILD_VERSION="2.8.0"
LABEL itss.version="2.8.0"

COPY app /app/app
COPY run.sh /app/run.sh
RUN chmod a+x /app/run.sh

EXPOSE 8099

CMD ["/app/run.sh"]
