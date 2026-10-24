FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir paho-mqtt==2.1.0
COPY devices.py .
ENTRYPOINT ["python", "-u", "devices.py"]