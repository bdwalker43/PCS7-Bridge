FROM mcr.microsoft.com/dotnet/aspnet:8.0-bookworm-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://rapidscada.org/download/rapidscada_6.4.7_linux_en.zip -o /tmp/rapidscada.zip \
    && unzip -q /tmp/rapidscada.zip 'scada/*' -d /tmp/rapidscada \
    && mv /tmp/rapidscada/scada /opt/rapidscada-seed \
    && rm -rf /tmp/rapidscada /tmp/rapidscada.zip \
    && chmod +x /opt/rapidscada-seed/make_executable.sh \
    && /opt/rapidscada-seed/make_executable.sh

COPY run.sh /usr/local/bin/rapid-scada-run
RUN chmod +x /usr/local/bin/rapid-scada-run

CMD ["/usr/local/bin/rapid-scada-run"]
