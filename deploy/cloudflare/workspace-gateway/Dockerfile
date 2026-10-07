# The sandbox a workspace scope's files live in. Python for the `felix-fs` helper, which is all
# the gateway runs inside it; the Durable Object starts it with the internet off and an empty
# environment.
FROM --platform=linux/amd64 python:3.14-slim-trixie

COPY helper/felix_fs.py /opt/felix-fs/felix_fs.py
RUN mkdir /workspace

CMD ["sleep", "infinity"]
