# TradeMind on the shared box

Production deployment of `https://trademind.store`, first installed on 2026-10-09 from
commit `ebc4e0d`. The box (`47.77.196.181`, Ubuntu 24.04, 2 vCPU, 1.6 GB) also runs the
AML memory service from another repository, and that service's Caddy container owns
ports 80 and 443. On the box, `/opt/trademind/app/DEPLOYED_COMMIT` names the release that
is running, and this directory is copied to `/opt/trademind/deploy/` (files under `kit/`).

## How a request travels

    browser --HTTPS--> shared Caddy (container deploy-caddy-1, ports 80/443, certificates)
            --HTTP over the Docker bridge--> hop Caddy (container trademind-hop-1, 172.17.0.1:8080)
            --loopback--> backend (systemd unit trademind-backend, 127.0.0.1:8000)

- The hop serves the built frontend from `/var/www/trademind`, asks for the workspace
  password on `/api/*`, and hands the request to the backend as a plain loopback request.
  It runs in the host's network namespace so that the backend sees `127.0.0.1` as the client.
- The backend runs in `local_only`, its single-operator mode. Since the September hardening
  it answers 403 to anything that looks proxied (a forwarding header, or a `Host` that is
  not loopback), so a conventional reverse proxy in front of it no longer works.
  `trusted_gateway` is the mode meant for a deployed, multi-user control plane, and it needs
  a sign-in gateway that signs every request; the product does not have one yet.
- The landing page is public. The workspace needs the password because whoever is inside
  holds operator powers: they can run tasks on the model key and export it from Settings.

## Files in this directory

| File | Installed on the box as |
|---|---|
| `trademind-backend.service` | `/etc/systemd/system/trademind-backend.service` |
| `hop.Caddyfile.tmpl` | rendered into `/etc/trademind/hop/Caddyfile` (holds the password hash) |
| `hop-compose.yml` | `/opt/trademind/deploy/hop-compose.yml` (compose project `trademind`) |
| `trademind-ingress-sync` | `/usr/local/sbin/trademind-ingress-sync` |
| `trademind-ingress-sync.service`, `.timer` | `/etc/systemd/system/` |
| `bwrap.apparmor` | `/etc/apparmor.d/bwrap` |
| `requirements.lock` | installed into `/opt/trademind/venv` |
| `sandbox_acceptance.py` | run from `/opt/trademind/deploy/kit/` |

## Where things are on the box

| What | Where |
|---|---|
| Code, one release | `/opt/trademind/app` |
| Python 3.11 environment | `/opt/trademind/venv`, uv in `/opt/trademind/bin` |
| Data: projects, runs, Provider profiles, key vault | `/var/lib/trademind/data` |
| Built frontend | `/var/www/trademind` |
| Backend settings | `/etc/trademind/backend.env`, optional `backend.local.env` read after it |
| Workspace login | `/etc/trademind/login.txt`, readable by root only |
| Site block in the shared Caddy | `/root/aml-memory/deploy/sites/trademind.caddy`, generated |

## Everyday commands

    systemctl status trademind-backend          # is it up
    journalctl -u trademind-backend -f          # backend log
    systemctl restart trademind-backend         # restart; running tasks are stopped
    systemctl stop trademind-backend            # free its memory, e.g. during the memory service's Full run
    cat /etc/trademind/login.txt                # workspace user name and password
    docker logs --tail 50 trademind-hop-1       # hop log

The backend and every agent worker it starts share one cgroup. It is throttled above
900 MB and stopped at 1.2 GB, so a runaway task cannot take the other service down. The
box has a 2 GB swap file at `/swapfile`.

## Hostnames and DNS

`trademind-ingress-sync` writes the site block. It adds a hostname only once every
authoritative nameserver answers with this box's address, so no certificate request fails
while a record still points elsewhere. `trademind.47-77-196-181.sslip.io` is always served
and needs no DNS. The timer runs every minute and switches itself off once
`trademind.store` and `www.trademind.store` both point here, which happened on 2026-10-09.
Names are only ever added; the list is in `/var/lib/trademind-ingress/hosts`.

The shared Caddy reads the site block through `import sites/*.caddy`, added to the memory
service's repository in its commit `f981e51`. That hook has to stay committed there: the
memory stack is updated with `git archive HEAD | tar -x`, which would otherwise put its
previous proxy files back and take this site offline. `docker compose ... down -v` on the
memory stack also drops TradeMind's certificates; Caddy requests them again on the next start.

## Bash sandbox on Linux

The backend requires the Claude Agent SDK's Bash sandbox (`failIfUnavailable`). On macOS
the system provides it. On Linux it is bubblewrap plus socat, and Ubuntu 24.04 also needs
`bwrap.apparmor`: without it bubblewrap stops with
`loopback: Failed RTM_NEWADDR: Operation not permitted`.

`sandbox_acceptance.py` checks the whole host side without a model key. It drives one Run
through the real application runtime and the pinned CLI; only the model endpoint is a
loopback fixture, which asks for a single Bash call. On the box:

    systemd-run --quiet --collect --wait --pipe -p User=trademind -p WorkingDirectory=/opt/trademind/app \
      -p Environment=HOME=/var/lib/trademind /opt/trademind/venv/bin/python /opt/trademind/deploy/kit/sandbox_acceptance.py

`"passed": true` means the sandboxed command ran and its output came back. It says nothing
about a real Provider. From a checkout: `.venv/bin/python deploy/shared-box/sandbox_acceptance.py`.

## Releasing a new version

1. Laptop, in a clean export of the commit (`git archive <commit> | tar -x -C <dir>`):
   `npm ci && npm run build` in `frontend/`. Leave `VITE_OPHIRA_API_URL` unset, the site is same-origin.
2. Box: `systemctl stop trademind-backend`, move `/opt/trademind/app` aside, unpack
   `git archive <commit>` into a new `/opt/trademind/app`, `chown -R trademind:trademind` it,
   and write the commit id into `DEPLOYED_COMMIT`.
3. Only if `backend/pyproject.toml` changed: rebuild `requirements.lock` on the laptop from
   the virtualenv the tests passed with, then install it on the box as `trademind`.

       uv pip freeze --python .venv/bin/python | grep -v -E '^(-e |ophira[-_]backend)' > /tmp/tested.txt
       uv pip compile backend/pyproject.toml -c /tmp/tested.txt --python-platform x86_64-manylinux_2_39 \
         --python-version 3.11 --no-header --no-annotate -o deploy/shared-box/requirements.lock

       uv pip install --python /opt/trademind/venv/bin/python -r /opt/trademind/deploy/kit/requirements.lock
       uv pip install --python /opt/trademind/venv/bin/python --no-deps -e /opt/trademind/app/backend

4. `systemctl start trademind-backend`, then `curl http://127.0.0.1:8000/health`.
5. `rsync -a --delete` the new `dist/` into `/var/www/trademind/`. The hop serves it at once.
6. Run the sandbox acceptance above.

## Installing on a fresh box

What was done once, as root, on a box that already had Docker and a Caddy container on 80/443.

1. Swap: a 2 GB `/swapfile` (`fallocate`, `chmod 600`, `mkswap`, `swapon`), an `/etc/fstab`
   line, and `vm.swappiness=10` in `/etc/sysctl.d/60-trademind-swap.conf`.
2. Sandbox: `apt-get install --no-install-recommends bubblewrap socat`, install
   `bwrap.apparmor` as `/etc/apparmor.d/bwrap`, `apparmor_parser -r /etc/apparmor.d/bwrap`.
3. User and directories: `useradd --system --user-group --create-home --home-dir /var/lib/trademind
   --shell /bin/bash trademind`; `/opt/trademind`, `/opt/trademind/bin`, `/var/lib/trademind/data`
   owned by it; `/var/www/trademind`, `/opt/trademind/deploy/kit`, `/etc/trademind/hop` owned by root.
4. Python: put the `uv` binary from its GitHub release (checked against the published
   `.sha256`) into `/opt/trademind/bin`, then as `trademind`:
   `uv venv --python 3.11 /opt/trademind/venv` and the two `uv pip install` lines above.
5. Settings in `/etc/trademind/backend.env`:

       OPHIRA_DATA_DIR=/var/lib/trademind/data
       OPHIRA_HOST=127.0.0.1
       OPHIRA_PORT=8000
       OPHIRA_AUTH_MODE=local_only
       OPHIRA_CORS_ORIGINS=https://trademind.store,https://www.trademind.store,https://trademind.47-77-196-181.sslip.io

   The origins list is what lets the browser's `Origin` through the backend's source check.
6. Backend: install the unit, `systemctl daemon-reload`, `systemctl enable --now trademind-backend`.
7. Password and hop: write `username: trademind` and `password: <24 random characters>` into
   `/etc/trademind/login.txt` (mode 600), then render the template and start the container.

       hash=$(sed -n 's/^password: //p' /etc/trademind/login.txt | docker run --rm -i caddy:2 caddy hash-password --algorithm bcrypt --bcrypt-cost 6)
       sed -e 's|__PORT__|8080|g' -e 's|__BIND__|172.17.0.1|g' -e 's|__BACKEND__|127.0.0.1:8000|g' \
           -e 's|__WWW__|/srv/www|g' -e 's|__USER__|trademind|g' hop.Caddyfile.tmpl \
         | awk -v hash="$hash" '{ i = index($0, "__HASH__"); if (i) $0 = substr($0, 1, i - 1) hash substr($0, i + 8); print }' \
         > /etc/trademind/hop/Caddyfile
       docker compose -f /opt/trademind/deploy/hop-compose.yml up -d

   The cost is low on purpose: the hash is checked on every API request, and the password is
   long and random. `/etc/trademind/hop` is mode 700 so the service user cannot read the hash.
8. Shared Caddy: its Caddyfile needs `import sites/*.caddy` and its container a read-only
   mount of `deploy/sites` at `/etc/caddy/sites`; recreate that one container.
9. Hostnames: `/etc/trademind/ingress.conf` with `BOX_IP`, `ALWAYS_HOST`, `APEX`, `WWW`,
   `UPSTREAM=172.17.0.1:8080`, `SITE_FILE=/root/aml-memory/deploy/sites/trademind.caddy` and
   `STATE_FILE=/var/lib/trademind-ingress/hosts`; install the sync script and units;
   `systemctl enable --now trademind-ingress-sync.timer`; then point the DNS records at the box.
10. Run the sandbox acceptance, open the site, add a model Provider in Settings and run its preflight.

## Changing the workspace password

Put the new password into `/etc/trademind/login.txt`, repeat the render in step 7, then
`docker restart trademind-hop-1`.

## Not available in this mode

Connector sign-ins that redirect back to the site (remote MCP OAuth, Gmail, Outlook,
Shopify, TikTok Shop) build their callback from the loopback origin and need
`trusted_gateway`. Browser tools (Playwright) and local OCR are not installed on the box.
