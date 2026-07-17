FROM python:3.12-slim
WORKDIR /srv/healthvoucher
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Non-root runtime user
RUN useradd -r appuser && chown -R appuser /srv/healthvoucher
USER appuser
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8000", "--access-logfile", "-", "app:create_app()"]
