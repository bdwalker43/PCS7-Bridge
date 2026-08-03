FROM mcr.microsoft.com/dotnet/aspnet:8.0-bookworm-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://rapidscada.org/download/rapidscada_6.4.7_linux_en.zip -o /tmp/rapidscada.zip \
    && curl -fsSL https://rapidscada.org/download/plg_mim_extra_comp/plg_mim_extra_comp_6.0.2.1_en.zip -o /tmp/mim-extra.zip \
    && unzip -q /tmp/rapidscada.zip 'scada/*' -d /opt \
    && unzip -q /tmp/mim-extra.zip -d /tmp/mim-extra \
    && cp -a /tmp/mim-extra/SCADA/. /opt/scada/ \
    && rm -rf /tmp/rapidscada.zip /tmp/mim-extra.zip /tmp/mim-extra \
    && chmod +x /opt/scada/make_executable.sh \
    && /opt/scada/make_executable.sh \
    && mv /opt/scada /opt/rapidscada-seed

COPY run.sh /usr/local/bin/rapid-scada-run
COPY starter-mimic.mim /usr/local/share/rapid-scada/starter-mimic.mim
RUN chmod +x /usr/local/bin/rapid-scada-run

CMD ["/usr/local/bin/rapid-scada-run"]
