FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Coolify provides the public HTTPS; the app listens on 0.0.0.0:8000 inside.
# STATE_DIR -> mount a persistent volume here so alerts survive redeploys.
ENV HOST=0.0.0.0 PORT=8000 STATE_DIR=/data
EXPOSE 8000
CMD ["python", "server.py"]
