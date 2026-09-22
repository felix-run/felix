**The builder image builds.** `.dockerignore` excluded `deploy/`, so `Dockerfile.builder`'s `COPY` of
its own entrypoint failed with "not found" the first time anyone ran `make up-self`; the one file an
image needs from `deploy/` is now let through.
