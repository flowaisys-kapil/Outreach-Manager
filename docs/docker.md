# Outreach Manager Docker Deployment Guide

This guide covers building, running, and managing **Outreach Manager v1.0** using Docker and Docker Compose.

---

## 1. Quick Start with Docker Compose (Recommended)

Outreach Manager includes a Docker Compose specification (`local.yml`) designed for development and containerized production deployment.

### Run Outreach Manager Container

```bash
docker compose -f local.yml up --build -d
```

Follow live application logs:
```bash
docker compose -f local.yml logs -f
```

Stop containerized services:
```bash
docker compose -f local.yml stop
```

---

## 2. Live Browser Remote Viewing (noVNC)

Outreach Manager containers include a pre-configured VNC stack (Xvfb + x11vnc + noVNC) allowing you to view browser automation in real-time or solve manual LinkedIn security checkpoints when needed.

### Accessing noVNC Web Viewer

Open your browser to:
```
http://localhost:6080/
```

- **Web Port**: `6080` (noVNC web application)
- **VNC Port**: `5900` (Native VNC viewer support, e.g. `vinagre vnc://127.0.0.1:5900`)
- **Activation Flag**: Controlled via `ENABLE_VNC=true` in `local.yml`.

---

## 3. Container Architecture & Dockerfile Design

Outreach Manager uses a two-stage build pipeline defined in `compose/linkedin/Dockerfile`:

```
+-------------------------------------------------------------+
| Stage 1: Build Dependencies (python:3.12-slim-bookworm)     |
| - Installs uv package manager                               |
| - Compiles requirements/production.txt into site-packages   |
+-------------------------------------------------------------+
                               |
                               v
+-------------------------------------------------------------+
| Stage 2: Runtime Image (python:3.12-slim-bookworm)          |
| - Installs Playwright Chromium into /opt/pw-browsers        |
| - Installs Xvfb, x11vnc, noVNC stack                        |
| - Configures non-root ubuntu user (UID 1000)                |
| - Executes entrypoint & start launcher                      |
+-------------------------------------------------------------+
```

### Volume Persistence & Environment Variables

| Variable / Volume | Purpose | Default |
|:---|:---|:---|
| `ENABLE_VNC` | Starts Xvfb and VNC server. | `true` |
| `HOST_UID` | Matches container user ID to host user (prevents Linux file permission issues). | `${HOST_UID:-1000}` |
| `HOST_GID` | Matches container group ID to host group. | `${HOST_GID:-1000}` |
| `./:/app` | Mounts workspace into container for persistent database (`data/db.sqlite3`) and logs (`data/outreach.log`). | Active in `local.yml` |

---

## 4. Useful Docker Commands

### Run Test Suite Inside Docker

```bash
docker compose -f local.yml run --remove-orphans app pytest -vv
```

### Access Container Shell

```bash
docker compose -f local.yml exec app /bin/bash
```

### Re-run Interactive Onboarding Inside Container

```bash
docker compose -f local.yml run --rm app python manage.py rundaemon
```

---

## 5. Troubleshooting Common Docker Issues

### 1. Port 6080 or 5900 Already in Use
If port `6080` is occupied by another application, edit `local.yml` to remap the external port:
```yaml
ports:
  - "6081:6080"
```

### 2. Linux Host Permission Denied on `data/db.sqlite3`
Ensure `HOST_UID` and `HOST_GID` match your local user ID:
```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose -f local.yml up --build -d
```

### 3. Display Lock Error (`/tmp/.X99-lock`)
The container startup script `compose/linkedin/start` automatically removes stale locks upon startup. If a lock persists after an abrupt termination, restart the container:
```bash
docker compose -f local.yml restart
```
