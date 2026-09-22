# The reader, explicitly.
#
# Nixpacks kept failing before it printed a reason, and guessing at a builder is
# a poor use of anyone's afternoon. This says what runs: one Python process and
# a handful of files. No dependencies, because the page talks to Supabase from
# the browser and the server only fills two placeholders into it.

FROM python:3.11-slim

WORKDIR /app
COPY web/ /app/web/
# The candidate archives are files rather than tables on purpose — they record
# what each topic's search found, which carries no claim — so the reader has
# to carry them: `runs/archive.json` for the first topic, `runs/archive.<topic>.json`
# for every other, all flat so one glob reaches them (see .railwayignore).
COPY runs/archive*.json /app/runs/

# Nothing here needs to write to the filesystem or bind a privileged port, and
# a container that runs as root because nobody said otherwise is a container
# that runs as root.
RUN useradd --system --no-create-home --uid 10001 reader \
 && chown -R reader:reader /app
USER reader

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "web/server.py"]
