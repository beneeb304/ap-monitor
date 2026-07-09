FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py poller.py db.py mqtt_out.py vendor.py oui.csv.gz ./
COPY static/ ./static/

EXPOSE 8088

# config.yaml and the SSH key are provided at runtime via volume mounts.
# History is persisted by mounting a volume at /data (set db_file: /data/history.db).
CMD ["python", "app.py"]
