# Python backend

- `heron/mission/`: MAVLink controller, desktop API, manual inputs, simulator,
  raster tiles, and lake geometry.
- `heron/mission/legacy/`: emergency Textual controller.
- `heron/utils/`: MCAP telemetry logging and vehicle network configuration.
- `heron/examples/`: sample mission files.
- `tests/`: backend and MCAP integration tests.
- `requirements.txt`: pinned dependencies shipped with the desktop app.

The Python project manifest and `uv.lock` stay at the repository root. Run
commands there:

```sh
uv sync
uv run heron-mission --no-ip-setup
uv run heron-controller --no-ip-setup
uv run python -m unittest discover -s backend/tests -v
```

The desktop starts the backend automatically. The browser UI lives in
`desktop/ui/`; native process management lives in `desktop/src-tauri/`.
