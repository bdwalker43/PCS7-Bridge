FROM frangoteam/fuxa:1.2.11

RUN rm -rf /usr/src/app/FUXA/server/_appdata \
           /usr/src/app/FUXA/server/_db \
           /usr/src/app/FUXA/server/_images \
           /usr/src/app/FUXA/server/_logs \
    && mkdir -p /data/appdata /data/db /data/images /data/logs \
    && ln -s /data/appdata /usr/src/app/FUXA/server/_appdata \
    && ln -s /data/db /usr/src/app/FUXA/server/_db \
    && ln -s /data/images /usr/src/app/FUXA/server/_images \
    && ln -s /data/logs /usr/src/app/FUXA/server/_logs

LABEL io.hass.version="1.2.11" \
      io.hass.type="app" \
      io.hass.arch="amd64"
