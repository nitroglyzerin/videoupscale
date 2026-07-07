FROM python:3.12-slim

# rsync + openssh-client für den ausgehenden Transfer zu den Vast-Nodes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync openssh-client tmux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vhsorch/ ./vhsorch/

# Nicht-Root wäre schöner, aber der SSH-Key wird als Secret gemountet; wir
# lesen ihn read-only. State-/Datenpfade kommen per Volume-Mapping.
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "vhsorch"]
CMD ["run"]
