FROM python:3.13-slim
WORKDIR /app
COPY app/ .
COPY app/requirements.txt .
RUN apt-get update && apt-get install -y wget unzip curl \
    && pip install --no-cache-dir -r requirements.txt
# Expose FastAPI and ngrok dashboard
EXPOSE 8000 4040
ENV NGROK_AUTHTOKEN=${NGROK_AUTHTOKEN}
ENV DHAN_ACCESS_TOKEN=${DHAN_ACCESS_TOKEN}
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port 8000"
