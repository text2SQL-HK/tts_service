# Nginx Deployment Guide

## Route layout

- Login UI: `/login`, `/login/chat`, `/login/mypage`
- Login static: `/login/static/*`
- Login API: `/api/auth/*`, `/api/users/*`, `/api/login/*`
- TTS UI: `/tts`
- TTS static: `/tts/static/*`
- TTS API: `/api/tts/*`
- Root redirect: `/` -> `/login`

## Upstream process example

- Login backend: `127.0.0.1:8001`
- TTS backend: `127.0.0.1:8000`

## Nginx server config (example)

```nginx
upstream login_backend {
    server 127.0.0.1:8001;
    keepalive 32;
}

upstream tts_backend {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name _;
    client_max_body_size 100m;

    # Default landing
    location = / {
        return 302 /login;
    }

    # Login service routes
    location ^~ /login {
        proxy_pass http://login_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ^~ /api/auth/ {
        proxy_pass http://login_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ^~ /api/users/ {
        proxy_pass http://login_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ^~ /api/login/ {
        proxy_pass http://login_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # TTS service routes
    location ^~ /tts {
        proxy_pass http://tts_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location ^~ /api/tts/ {
        proxy_pass http://tts_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Runtime checks

1. Open `/` and verify redirect to `/login`.
2. Login succeeds and browser moves to `/tts`.
3. Direct access to `/tts` without login gets redirected to `/login`.
4. API request to `/api/tts/*` without login returns `401`.
5. Static assets load from `/tts/static/*` and `/login/static/*`.
