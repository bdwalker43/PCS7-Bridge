FROM mcr.microsoft.com/dotnet/aspnet:8.0-bookworm-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://rapidscada.org/download/rapidscada_6.4.7_linux_en.zip -o /tmp/rapidscada.zip \
    && unzip -q /tmp/rapidscada.zip 'scada/*' -d /opt \
    && rm -f /tmp/rapidscada.zip \
    && chmod +x /opt/scada/make_executable.sh \
    && /opt/scada/make_executable.sh \
    && mv /opt/scada /opt/rapidscada-seed

COPY run.sh /usr/local/bin/rapid-scada-run
COPY starter-mimic.mim /usr/local/share/rapid-scada/starter-mimic.mim
RUN chmod +x /usr/local/bin/rapid-scada-run

CMD ["/usr/local/bin/rapid-scada-run"]
