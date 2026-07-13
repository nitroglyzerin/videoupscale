FROM python:3.12-slim

# rsync + openssh-client für den ausgehenden Transfer zu den Vast-Nodes.
# ffmpeg liefert ffprobe: Frame-Messung der Rohclips (Warteschlangen-Priorität
# "teuerste zuerst" + Kosten-/Hochrechnungs-Seite der TUI).
RUN apt-get update \
    && apt-get install -y --no-install-recommends rsync openssh-client tmux ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vhsorch/ ./vhsorch/

# Branch, aus dem gebaut wurde (menu.sh reicht ihn durch). Bestimmt, von wo
# die Nodes bootstrap.sh/process.sh laden (config.py: repo_raw_url-Default).
ARG REPO_BRANCH=main
ENV REPO_BRANCH=${REPO_BRANCH}

# Nicht-Root wäre schöner, aber der SSH-Key wird als Secret gemountet; wir
# lesen ihn read-only. State-/Datenpfade kommen per Volume-Mapping.
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "vhsorch"]
CMD ["run"]
