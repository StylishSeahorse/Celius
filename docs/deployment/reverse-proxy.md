# Reverse proxy and deployment

The stack exposes the API only on `127.0.0.1:8000` (`API_BIND_ADDRESS`/`API_PORT`).
Your existing reverse proxy terminates TLS for your domain and forwards to it.
PostgreSQL and Redis sit on an `internal` Docker network with no published ports.

## First deployment

```sh
cp .env.example .env        # fill in secrets; chmod 600 .env
make up                     # builds the image, runs migrations, starts the API
curl -s http://127.0.0.1:8000/api/v1/health/ready
```

Remove `BOOTSTRAP_PASSWORD` from `.env` once the first login works.

## Proxy requirements

- Forward `X-Forwarded-For` / `X-Forwarded-Proto` headers. Uvicorn runs with `--proxy-headers`.
- **Disable buffering** on `/api/v1/runs/*/events` (SSE). Allow idle reads of at least 300 s.
- Set the request body limit to at least `MAX_UPLOAD_BYTES`.
- Do not expose the llama.cpp server through the proxy.

### Caddy

```caddy
ai.example.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
        transport http { read_timeout 310s }
    }
    request_body { max_size 60MB }
}
```

### nginx

```nginx
location /api/v1/runs/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 310s;
    proxy_set_header Connection "";
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
location / {
    proxy_pass http://127.0.0.1:8000;
    client_max_body_size 60m;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

If the proxy runs on a different host from Docker, set `API_BIND_ADDRESS` to the
Docker host's private IP. Firewall that port so only the proxy can reach it.

## Model server (Windows / RTX 3060)

- Start `llama-server` with `--host <private IP> --port 8080`. Add `--api-key` when it
  is reachable from anything except the app host, and `--jinja` if you want tool calling.
- Allow inbound 8080 in Windows Firewall **only** from the Docker host's IP.
- Check from the Docker host: `curl http://MODEL_SERVER_HOST:8080/health`.
- After login, `GET /api/v1/models` shows what the app detected. Run
  `POST /api/v1/models/{id}/verify-tools` before you rely on tool calling.
