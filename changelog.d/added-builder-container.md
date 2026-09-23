**`make up-self` runs the Felix that builds Felix.** `deploy/docker/compose.self.yml` puts the
api and worker on `Dockerfile.builder` — the lean image plus git, make and uv — with a separate
clone of the repository at `/workspace` that the `contributor` and `triage` manifests edit and
run the gates in. Two trees on purpose: the harness running the loop is the image's own venv;
the harness being edited is the workspace. No Docker socket, no cloud credentials, one tenant —
the host is the shell tool's boundary and this is that host.
