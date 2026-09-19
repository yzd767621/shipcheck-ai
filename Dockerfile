FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080 \
    SHIPCHECK_DB=/tmp/shipcheck/shipcheck.db SHIPCHECK_UPLOADS=/tmp/shipcheck/uploads
WORKDIR /app

# Tesseract OCR: free, local reading of scanned SI / BL pages (no API key needed)
RUN apt-get update  && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shipcheck/ shipcheck/
COPY web/ web/
COPY data/ data/
COPY app.py .

EXPOSE 8080
# Cloud Run / Render / Railway inject $PORT
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
