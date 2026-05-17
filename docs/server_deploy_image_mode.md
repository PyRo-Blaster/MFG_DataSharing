# Server Image Deployment

This guide documents the image-based deployment flow:

1. Build the Docker image locally
2. Export the image as a `tar` archive
3. Upload the archive and compose files to the server
4. Load the image on the server
5. Start the app with `compose.server.yaml`

## Platform

The release image should target:

```bash
linux/amd64
```

This is important when building from an Apple Silicon workstation and deploying to an Ubuntu x86_64 server.

## 1. Build the Release Image

From the project root:

```bash
chmod +x scripts/package_image.sh
./scripts/package_image.sh
```

Default output:

```bash
mfg-data-sharing-latest.tar
```

Expected output includes:

```bash
==> Building mfg-data-sharing:latest for linux/amd64
==> Inspecting image platform
OS/Arch: linux/amd64
```

To set a custom tag:

```bash
IMAGE_TAG=1.0 ./scripts/package_image.sh
```

## 2. Upload to the Server

Replace the placeholders before running:

```bash
scp mfg-data-sharing-latest.tar <USER>@<SERVER_IP>:/opt/mfg-data-sharing/
scp compose.server.yaml .env <USER>@<SERVER_IP>:/opt/mfg-data-sharing/
```

Create the target directory first if needed:

```bash
ssh <USER>@<SERVER_IP> "mkdir -p /opt/mfg-data-sharing"
```

## 3. Load the Image

```bash
ssh <USER>@<SERVER_IP>
cd /opt/mfg-data-sharing
docker load -i mfg-data-sharing-latest.tar
```

Verify the platform:

```bash
docker image inspect mfg-data-sharing:latest --format '{{.Os}}/{{.Architecture}}'
```

Expected:

```bash
linux/amd64
```

## 4. Start the Service

```bash
docker compose -f compose.server.yaml up -d
docker compose -f compose.server.yaml ps
docker compose -f compose.server.yaml logs --tail=100
```

## 5. Update an Existing Deployment

Rebuild and re-upload:

```bash
./scripts/package_image.sh
scp mfg-data-sharing-latest.tar <USER>@<SERVER_IP>:/opt/mfg-data-sharing/
```

Reload and restart:

```bash
ssh <USER>@<SERVER_IP>
cd /opt/mfg-data-sharing
docker load -i mfg-data-sharing-latest.tar
docker compose -f compose.server.yaml up -d
```

## 6. Maintenance

Stop:

```bash
docker compose -f compose.server.yaml down
```

Logs:

```bash
docker compose -f compose.server.yaml logs -f
```

Restart:

```bash
docker compose -f compose.server.yaml restart
```
