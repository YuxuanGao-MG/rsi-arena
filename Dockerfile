# The reader, explicitly.
#
# Nixpacks kept failing before it printed a reason, and guessing at a builder is
# a poor use of anyone's afternoon. This says what runs: one Python process and
# two files. No dependencies, because the page talks to Supabase from the
# browser and the server only fills two placeholders into it.

FROM python:3.11-slim

WORKDIR /app
COPY web/ /app/web/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "web/server.py"]
