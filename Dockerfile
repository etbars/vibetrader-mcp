FROM python:3.11-slim

WORKDIR /app

# Force cache bust
ARG CACHEBUST=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED=1

# Run with uvicorn to use the wrapped app with middleware
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
