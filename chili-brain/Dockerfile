# Brain HTTP service — build from repository root:
#   docker build -f chili-brain/Dockerfile -t chili-brain:local .
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Shared application package (strangler — same ORM/models as CHILI)
COPY app ./app
COPY chili-brain/chili_brain_service ./chili_brain_service

RUN mkdir -p /app/data

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8090

# HTTP on the Docker network (worker → brain). Use HTTPS on the CHILI service for the browser.
CMD ["uvicorn", "chili_brain_service.main:app", "--host", "0.0.0.0", "--port", "8090"]
