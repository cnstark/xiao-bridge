FROM python:3.12-slim

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8091

CMD ["python", "bridge.py"]
