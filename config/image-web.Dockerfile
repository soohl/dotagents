FROM node:22.23.2-alpine@sha256:b6f26b36c8ff49624cfdac716b8ea1138d606df02586a77d364bb5536a634f85 AS frontend
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY config/image-web-requirements.txt /app/config/
RUN pip install --no-cache-dir -r config/image-web-requirements.txt
COPY src/image_web.py src/image_jobs.py src/async_tasks.py src/image_backend.py src/image_history.py src/image_edit.py /app/src/
COPY config/image.json /app/config/
COPY --from=frontend /web/dist /app/web/dist
COPY web/LICENSE.cook-4090 web/LICENSE.shadcn /app/licenses/
RUN mkdir -p /data/images && chown 1000:1000 /data/images && chmod 700 /data/images
USER 1000:1000
CMD ["python", "-m", "src.image_web"]
