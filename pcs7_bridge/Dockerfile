FROM python:3.12-slim

LABEL io.hass.type="app" \
      io.hass.arch="amd64" \
      io.hass.version="0.1.0"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py command_runtime.py run.sh seed.json ./
RUN chmod a+x /app/run.sh

CMD ["/app/run.sh"]
