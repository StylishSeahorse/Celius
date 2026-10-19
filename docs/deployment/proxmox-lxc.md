# Installing on a Proxmox LXC container

Target: a Debian 12 (or Ubuntu 24.04) LXC running Docker. Celius runs as the Compose
stack in `infra/compose.yaml`. The model server (llama.cpp on the RTX 3060 PC) stays
on its own machine.

## 1. Prepare the container (on the Proxmox host)

Docker inside an unprivileged LXC needs nesting and keyctl:

```sh
pct set <CTID> -features nesting=1,keyctl=1
pct reboot <CTID>
```

Suggested resources: 2 cores, 4 GB RAM, 30 GB disk. The first image build runs Node
and Python builds; after that, usage is light.

## 2. Install Docker and tools (inside the container)

```sh
apt update && apt install -y ca-certificates curl git make openssl
curl -fsSL https://get.docker.com | sh
docker run --rm hello-world        # confirms Docker works inside the LXC
```

## 3. Get the code

The repository is private, so use a GitHub personal access token with read access, or a deploy key:

```sh
git clone -b claude/phase1-foundation https://github.com/StylishSeahorse/Celius.git /opt/celius
cd /opt/celius
```

## 4. Configure

```sh
cp .env.example .env
chmod 600 .env
sed -i "s|REPLACE_WITH_RANDOM_SECRET|$(openssl rand -hex 32)|" .env
sed -i "s|REPLACE_WITH_RANDOM_PASSWORD|$(openssl rand -hex 24)|" .env
nano .env
```

Set these in `.env`:

| Variable | Value |
| --- | --- |
| `APP_PUBLIC_URL` | `https://ai.yourdomain.com` |
| `BOOTSTRAP_USERNAME` / `BOOTSTRAP_PASSWORD` | Your login (password of 12+ characters) |
| `MODEL_BASE_URL` | `http://<windows-pc-ip>:8080/v1` |
| `MODEL_API_KEY` | The `--api-key` you give llama-server (recommended) |
| `MODEL_DEFAULT_ID` | Any name; llama-server ignores it when one model is loaded |
| `API_BIND_ADDRESS` | `127.0.0.1` if the reverse proxy runs in this container; otherwise this container's LAN IP |

## 5. Start

```sh
make up                 # builds the image (several minutes the first time), migrates, starts
docker compose -f infra/compose.yaml --env-file .env ps
curl -s http://127.0.0.1:8000/api/v1/health/ready      # or the API_BIND_ADDRESS IP
```

`ready` with `"database": {"ok": true}` means the app is up. `model_server` shows
whether llama-server is reachable.

After the first successful login, delete `BOOTSTRAP_PASSWORD` from `.env`.

## 6. Model server on the Windows PC

```powershell
llama-server -m C:\models\your-model.gguf --host 0.0.0.0 --port 8080 `
  -ngl 99 -c 8192 --parallel 1 --jinja --api-key YOUR_KEY

# Allow only the Celius container to reach it:
New-NetFirewallRule -DisplayName "llama-server (Celius)" -Direction Inbound `
  -Protocol TCP -LocalPort 8080 -RemoteAddress <lxc-ip> -Action Allow
```

Check from the container: `curl -H "Authorization: Bearer YOUR_KEY" http://<windows-pc-ip>:8080/v1/models`.
Keep `MODEL_PARALLEL_REQUESTS` in `.env` equal to `--parallel`.

## 7. Reverse proxy

Point your domain at `API_BIND_ADDRESS:8000`. Disable buffering for `/api/v1/runs/`
(see [reverse-proxy.md](reverse-proxy.md)).

## Updating

```sh
cd /opt/celius && git pull && make up      # migrations run automatically before the API starts
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `docker run` fails with permission or `sysctl` errors | `nesting=1,keyctl=1` not set, or the container wasn't restarted |
| `migrate` exits non-zero | `docker compose -f infra/compose.yaml --env-file .env logs migrate`; usually `POSTGRES_PASSWORD` contains URL-unsafe characters (the generated hex is safe) |
| Model server shows offline | Firewall, wrong IP or port, or a missing `/v1` in `MODEL_BASE_URL`; test with curl from the container |
| Replies appear all at once, not streaming | Reverse proxy buffering; see reverse-proxy.md |
| Logs | `make logs` (API); `docker compose -f infra/compose.yaml --env-file .env logs -f worker` |
