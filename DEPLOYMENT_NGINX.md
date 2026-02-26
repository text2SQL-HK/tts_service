# Nginx Deployment Guide

## Route Layout

- Login UI: `/login`, `/login/chat`, `/login/mypage`
- Login static: `/login/static/*`
- Login API: `/api/auth/*`, `/api/users/*`, `/api/login/*`
- TTS UI: `/tts`
- TTS static: `/tts/static/*`
- TTS API: `/api/tts/*`
- Root redirect: `/` -> `/login`
- Gateway health check: `/healthz`

## Repository Files

- Nginx config: `deploy/nginx/default.conf`
- Docker Compose (Nginx only): `docker-compose.nginx.yml`

`default.conf` upstreams:

- Login backend: `127.0.0.1:8001`
- TTS backend: `127.0.0.1:8000`
- Nginx listen: `80`

## Prerequisites

1. Login backend와 TTS backend가 각각 실행 가능한 Python 환경을 준비합니다.
2. `tts_service/backend/config.py`의 기본 인증 연동값을 유지하거나 환경변수로 명시합니다.
   - `AUTH_COOKIE_NAME=session_token`
   - `LOGIN_AUTH_ME_URL=http://127.0.0.1:8001/api/auth/me`
3. 서버의 `80` 포트가 비어 있어야 합니다.

## Run Steps

1. Login backend 실행 (`8001`)
2. TTS backend 실행 (`8000`)
3. Nginx gateway 실행

```bash
# terminal 1
cd /tmp/workspace3/login/backend
uvicorn main:app --host 0.0.0.0 --port 8001
```

```bash
# terminal 2
cd /tmp/workspace3/tts_service
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

```bash
# terminal 3
cd /tmp/workspace3/tts_service
docker compose -f docker-compose.nginx.yml up -d
```

## Runtime Checks

```bash
curl -i http://localhost/healthz
curl -I http://localhost/
curl -I http://localhost/login
curl -I http://localhost/tts
curl -i http://localhost/api/tts/info
```

Expected behavior:

1. `GET /healthz` -> `200 OK`
2. `GET /` -> `302` with `Location: /login`
3. 로그인 없이 `GET /tts` -> `307` with `Location: /login`
4. 로그인 없이 `GET /api/tts/*` -> `401`
5. 로그인 후 `/tts` 진입 및 `/tts/static/*`, `/login/static/*` 로딩 정상

## Stop Gateway

```bash
cd /tmp/workspace3/tts_service
docker compose -f docker-compose.nginx.yml down
```

## Notes

- `docker-compose.nginx.yml`는 `network_mode: host`를 사용합니다 (Linux host 기준).
- `default.conf`의 `/api/tts` 라우트는 장시간 요청을 위해 `proxy_read_timeout`/`proxy_send_timeout`을 `600s`로 설정했습니다.
