FROM eclipse-temurin:23-jdk

# ---- system packages --------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ccache \
    curl git git-lfs make autoconf zip unzip file \
    libfreetype6-dev libcups2-dev libasound2-dev \
    libx11-dev libxext-dev libxrender-dev libxtst-dev libxt-dev \
    libgtk-3-dev && \
    rm -rf /var/lib/apt/lists/*

# ---- Boot JDK (JDK 24) ------------------------------------------------
# JDK 25 requires Boot JDK 24+
ENV BOOT_JDK=/opt/java/jdk-24
RUN curl -L "https://download.java.net/java/GA/jdk24.0.2/fdc5d0102fe0414db21410ad5834341f/12/GPL/openjdk-24.0.2_linux-x64_bin.tar.gz" \
        -o /tmp/jdk24.tar.gz && \
    mkdir -p /opt/java && \
    tar -xzf /tmp/jdk24.tar.gz -C /opt/java && \
    mv /opt/java/jdk-24* /opt/java/jdk-24 && \
    rm /tmp/jdk24.tar.gz

# ---- jtreg 8 ----------------------------------------------------------
RUN mkdir -p /opt/jtreg && \
    curl -L "https://builds.shipilev.net/jtreg/jtreg-8.1+1.zip" -o /tmp/jtreg.zip && \
    unzip -q /tmp/jtreg.zip -d /opt/ && \
    rm /tmp/jtreg.zip && \
    chmod +x /opt/jtreg/bin/jtreg /opt/jtreg/bin/jtdiff && \
    printf '#!/bin/sh\nJT_HOME=/opt/jtreg exec /opt/jtreg/bin/jtreg "$@"\n' > /usr/local/bin/jtreg && \
    chmod +x /usr/local/bin/jtreg && \
    jtreg -version

RUN echo "Boot JDK is at ${BOOT_JDK}" && ${BOOT_JDK}/bin/java -version

ENV JTREG_HOME=/opt/jtreg

WORKDIR /repo

RUN git config --global --add safe.directory /repo

RUN git lfs install --system
