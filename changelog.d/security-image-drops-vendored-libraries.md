**The image no longer ships a wheel's vendored copy of libpq's dependency chain.**
`psycopg[binary]`'s manylinux wheels bundle their own builds of pcre2, krb5, ldap, sasl,
selinux and OpenSSL, and declare them in an auditwheel SBOM — so a scanner reads, correctly,
that the image carries RHEL 8's `pcre2 10.32` and **OpenSSL 1.1.1k**, long EOL. `apt-get
upgrade` in the Dockerfile cannot reach inside a Python wheel, so those stayed at whatever
version the wheel was built against no matter how current the base image was.

It surfaced as a release failure that looked like flakiness and was not: 0.3.0's scan failed
on `linux/arm64` with six pcre2 findings (three CRITICAL) while `linux/amd64` passed off the
same Dockerfile and the same pinned base digest — the two wheels repair different library
sets, and only the aarch64 one bundles pcre2. The Debian `libpcre2-8-0` in that same image
was already patched.

The image now installs `psycopg[c]`, which links the system libpq and vendors nothing: every
one of those libraries becomes a Debian package that the existing `apt-get upgrade` already
patches. The scan passes with **no suppression** — the findings went away because the library
did.

Deliberately a Dockerfile change and not a dependency change. `pyproject.toml` keeps
`psycopg[binary]`, so `pip install felix-harness` and a contributor's `make install` still
need no compiler and no libpq headers; only the image, which is the only artifact whose
supply chain is a user's problem, pays the build cost. The builder stage gains `gcc`,
`libc6-dev` and `libpq-dev`, none of which reach the runtime stage — it installs `libpq5`
alone.

The image got **smaller**: 577 MB against 587 MB, because the vendored libraries outweighed
`libpq5`. Verified end to end rather than assumed — `psycopg.pq.__impl__` reports `c`, all
sixteen migrations run to head against a real Postgres 17.11, and a query round-trips.
