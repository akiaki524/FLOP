# Observer container isolation — preparation

Update: the user supplied a PASS result for container
`observer-isolation-d07a7daff61b`, image ID
`sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea`.
All reported configuration and process checks passed. This confirms the reported
base-image probe, not yet the Observer-code smoke. The next bounded procedure is
in [live-smoke-plan.md](live-smoke-plan.md), targeting Technocore 0.12.1.

Current policy: Docker Desktop per-user + WSL2 integration is installed by the user.
Do not install Docker Engine/CLI inside Ubuntu, use sudo, or change socket permissions.
Do not combine Desktop with a separately installed WSL Engine.

The ordinary Ubuntu terminal can reach the Linux Docker server. This Codex session
cannot, even on the approved retry outside its command sandbox. Its UID/GID maps
each contain one mapping and socket IDs are displayed as 65534, whereas the user
reports root:1001 with mode 0660 in the normal terminal. The namespace difference
is confirmed; the complete access-denial cause is not established. Do not infer
that the actual host socket should be changed to group 65534.

## Offline check

Run from the ordinary Ubuntu terminal, in the Observer Component directory (`~/FLOP/components/observer`):

```sh
python3 -B tests/container_isolation.py
python3 -B tests/container_isolation.py --execute
```

The first command prints the plan and checks the embedded probe's Python syntax.
The second lists the local `python:3.12-slim` image, uses its immutable image ID,
creates one container with `--pull=never`, inspects its configuration, and starts
it only if the checks pass. All Docker calls explicitly use
`--host=unix:///var/run/docker.sock` for the local Desktop daemon. The helper
creates a fresh private `tests/.isolation-client-<random>/` directory, uses it as
Docker's `--config` directory and HOME, and passes only PATH and that HOME to the
CLI. It does not use existing Docker credentials, contexts, or DOCKER_* variables.
The private client directory is retained for review. This HOME setting selects
client configuration only; the Observer isolation boundary is the container.
It does not invoke image pull/build, package installation, sudo, live
Technocore requests, or automatic removal. If the image is missing it exits with
`LOCAL_IMAGE_MISSING`; obtaining an image requires a separate network decision.

The container runs Python with UID/GID 65532, readonly root, no capabilities,
no-new-privileges, PID/memory limits, network `none`, no host namespaces, no devices,
no bind mounts, no published ports, and one private tmpfs at `/state`. The only
explicit environment values are HOME=/nonexistent and PYTHONDONTWRITEBYTECODE=1.
Inherited environment names outside the known Python base-image set are rejected
before startup. Environment values and full Docker inspection data are not printed.

The source of the public probe is supplied through stdin, so no project or HOME
bind mount is required. `tests/isolation_canary.txt` is public and must exist on
the host but remain unreadable at its host absolute path inside the container.
No real seed/key/wallet/SSH file is opened, listed, or copied. The test also checks
UID/GID, capabilities, seccomp, no-new-privileges, network interfaces, absence of
host paths/control socket paths, root write denial, and state write permissions.

Successful output includes the local image ID and a stopped container name.
The stopped container is retained for review. On an error, do not automatically
retry or remove it. A timeout can leave a container present; inspect its state
before deciding on cleanup.

This is an isolation check of the existing base image under the proposed runtime
flags. It does not build/import the Observer, attest to image supply-chain trust,
prove resistance to kernel exploits, or establish the final Observer image's
isolation. The final image and actual mounts must be checked again after build.

## Live test gate

Live smoke has not been started. Before it is started, present the concrete image
build/run commands and network scope to the user after isolation checks succeed.
Keep registry/image acquisition separate from Technocore HTTP traffic. Image pulls
may contact authentication and CDN endpoints as well as a registry; that access
has not been implicitly approved by the isolation check.

The pending smoke room is `mb-047f3d88ef38`; scope remains Specification v0.3 §70.
The normal Observer client accepts only configured room reads and `/config`.
The separate smoke plan must explicitly describe the `limit=201` diagnostic and
the additional valid, presumed-nonexistent room read, with a finite request count.
Preserve TLS verification, disable redirect/proxy discovery, and include DNS in
the network disclosure. No write requests, stress tests, deliberate 429, or soak.
