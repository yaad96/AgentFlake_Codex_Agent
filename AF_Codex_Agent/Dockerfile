FROM maven:3.8.6-openjdk-8

WORKDIR /app

RUN apt-get update && \
    apt-get install -y autoconf automake libtool curl make g++ unzip python3 python3-pip && \
    ln -s /usr/bin/python3 /usr/bin/python && \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update && \
    apt-get install -y xmlstarlet && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install beautifulsoup4 lxml

# Codex CLI — used by the agentic_codex_cli driver. Installs to
# /root/.local/bin. Auth is supplied at run time: the driver passes
# OPENAI_API_KEY into `docker exec`, and the in-container script runs
# `codex login --with-api-key` against a per-run CODEX_HOME.
RUN curl -fsSL https://chatgpt.com/codex/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
RUN codex --version

ENV MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository"

ENV MODULE=""
ENV DIR_TO_PYTHON_SCRIPT=""
ENV FULL_TEST_NAME=""
ENV ITERATIONS="5"

CMD ["tail", "-f", "/dev/null"]
