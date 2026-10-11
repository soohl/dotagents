# DotAgents rules

- Keep this personal stack lightweight. The host is a Mac with Tailscale.
  Discover optional nodes; do not assume hardware, addresses, or a tailnet.
- Expose only `./run.sh` and `./run.sh --verify`. Require readiness checks
  before startup. Keep the full test suite in `./run.sh --verify`.
  Do not add interactive setup or a separate CLI.
- Keep runtime choices in `config.yaml`, credentials in ignored `.env`,
  implementation in `src/`, and pins in `config/`.
- Keep one dashboard with service status and job output. Use functional labels.
- Run applications in Docker and local inference with native Metal. Applications
  own their data. Do not add a central application database.
- Keep local browser and management listeners on loopback. Protect remote
  browser access with the shared password and FIDO2 security key login.
- Give Chat and Agent separate `sessions/chat` and `sessions/agent` directories.
  Mount only each application's own workspace. Reject symlink escapes.
- Load one large worker per device. Never stop a process owned by another server.
  Preserve credentials, sessions, model pins, and upstream licenses.
- Keep weights, caches, host details, and recovery data out of Git.
- Keep essential setup and usage in `README.md`; do not add separate docs.
  Public capabilities live in `config/stack.json`.
- Run `./run.sh --verify` after changes. Engine changes also need targeted tests
  and matched inference checks. Do not claim speed gains without measurements.
- Work on the current branch. Do not commit, push, rewrite history, or make
  system-wide changes without explicit authorization.
