Prometheus file-based service discovery for optional Compose overlays.

An overlay that brings up a scrapable service drops a `*.yml` file in here; the
`overlays` job in `../prometheus.yml` picks it up within 30s without a Prometheus restart.
Empty is the normal state — it means no optional overlay is running, which is why those
services do not appear as permanently-down static targets.

Each file sets its own `job` label, for example `myservice.yml`:

```yaml
- targets: ["myservice:9090"]
  labels:
    job: myservice
```

Why an overlay does not ship one ready-made: a Compose overlay cannot add a volume to a service
the base file does not define, so it cannot mount a file into Prometheus without breaking when
run on its own — and a file committed here unconditionally would leave anyone running only the
observability overlay with a target that never comes up.
