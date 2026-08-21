FROM docker.io/library/python:3.9.7-slim-buster

# Copy application code
COPY . .

# Install system dependencies (Debian/Ubuntu based)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    build-essential \
    ffmpeg \
    aria2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

# Default command (adjust according to your bot's entry point)
CMD ["python", "main.py"]
