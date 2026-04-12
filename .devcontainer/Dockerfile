FROM espressif/idf:release-v5.2

# Set locale to prevent Python encoding errors during IDF builds
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    locales \
    && locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US.UTF-8
ENV LC_ALL=en_US.UTF-8

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    nano \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

RUN echo 'source /opt/esp/idf/export.sh' >> /root/.bashrc

WORKDIR /workspace