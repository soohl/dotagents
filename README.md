# DotAgents

Private local Chat and Agent applications with a terminal dashboard.

The host runs macOS with Tailscale installed. Applications run in Docker.
Native inference uses Metal on Apple Silicon. Remote nodes are optional.

Prepare a Python 3.11 `.venv` from `config/verification-requirements.txt`.
Install Node.js for verification. Copy `config/env.example` to `.env`
and restrict it with `chmod 600 .env`.
Install Docker and the enabled engines and models.
Prepare the application images and private Harness profiles before starting.
The verifier reports missing requirements; startup installs nothing.

Set models, optional integrations, and the gateway port in [config.yaml](config.yaml).
Keep credentials in `.env`. Chat supports optional [Things](mcp/mcp-things-3/README.md)
and read-only [Calendar](mcp/calendar-mcp/README.md) connections.

```sh
./run.sh --verify
./run.sh
```

Both commands check readiness. `--verify` also runs the test suite, then exits.
Run it after changes. Startup discovers Tailscale model APIs on the configured
gateway port (default `8888`). No responding nodes is valid.

Open [localhost:3000](http://localhost:3000) for the applications.
Put project files in `sessions/chat/workspace` or `sessions/agent/workspace`.
Press `q` to quit. Docker applications keep running.

Keep credentials, personal IDs, host details, weights, and sessions out of Git.
See [config/stack.json](config/stack.json) for capabilities and attribution.
Upstream licenses still apply.
