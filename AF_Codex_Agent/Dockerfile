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

# Claude Code CLI — used by the agentic_claude_cli driver (the "Claude Code
# agent" repair variant). Installed to /root/.local/bin. Auth is supplied at
# run time via the ANTHROPIC_API_KEY env var passed to `docker exec`.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}"
RUN claude --version

ENV MAVEN_OPTS="-Dmaven.repo.local=/root/.m2/repository"

ENV MODULE=""
ENV DIR_TO_PYTHON_SCRIPT=""
ENV FULL_TEST_NAME=""
ENV ITERATIONS="5"

CMD ["tail", "-f", "/dev/null"]
