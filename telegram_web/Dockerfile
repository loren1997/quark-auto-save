FROM python:3.13-slim-bookworm
ARG BUILD_SHA
LABEL org.opencontainers.image.revision=$BUILD_SHA
ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app
RUN pip install --no-cache-dir playwright==1.64.1 && \
    playwright install --with-deps --only-shell chromium && \
    rm -rf /var/lib/apt/lists/*
COPY telegram_web /app/telegram_web
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3).read()"
CMD ["python", "-m", "telegram_web.server"]
