FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY src/browser_proxy.py /app/browser_proxy.py
USER 1000:1000
CMD ["python", "browser_proxy.py"]

FROM base AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers HOME=/tmp
COPY config/browser-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && patchright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
COPY src/browser.py /app/browser.py
USER 1000:1000
CMD ["uvicorn", "browser:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
