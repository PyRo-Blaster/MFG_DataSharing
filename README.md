# MFG Data Sharing

Lightweight web app for sharing manufacturing process data through a simple login-protected dashboard.

## Features

- Shared username/password login
- CSV upload for data refresh
- Dashboard charts backed by server-side JSON storage
- Docker and Docker Compose deployment
- Optional image-based release flow for slow or unstable servers

## Quick Start

1. Copy the example environment file:

```bash
cp .env.example .env
```

2. Edit `.env` and set your own credentials:

```bash
MFG_USERNAME=your-username
MFG_PASSWORD=your-strong-password
```

3. Start the app locally:

```bash
docker compose up -d --build
```

4. Open:

```text
http://127.0.0.1:8025
```

## Files

- `app.py`: FastAPI backend
- `static/`: dashboard, login page, CSV template
- `compose.yaml`: local compose setup
- `compose.server.yaml`: server deployment using a prebuilt image
- `scripts/package_image.sh`: build and export a release image tarball

## Notes

- Runtime data is stored under `data/` and is ignored by Git.
- Internal planning documents and private requirement materials are intentionally excluded from the public repository.
- For server image deployment, see [server_deploy_image_mode.md](file:///Users/chenji/Desktop/CodeSpace/mfg_datalogger/docs/server_deploy_image_mode.md).
