# Modbus Edge Agent

A conservative read-only Modbus RTU edge agent with semantic register decoding, durable local buffering, and MQTT QoS 1 publishing.

I built this agent for industrial telemetry pilots where field devices must remain local, control authority must stay outside the cloud path, and dry-run evidence must never be confused with observed hardware data.

## Safety boundary

- Modbus reads only; no write coils/registers or remote-control path.
- Raw Modbus is never tunneled over MQTT.
- Dry-run payloads are explicitly marked `meta.synthetic=true`.
- A configuration may also force synthetic provenance for lab fixtures using real transport.
- Device credentials are injected only into in-memory wire payloads.
- Device tokens never enter the durable JSONL retry buffer or no-MQTT debug output.
- Existing system configuration is preserved on upgrade by default.
- The installer never enables the service automatically.

## Data path

```text
RS-485 / Modbus RTU
  → typed register decoder
  → semantic params + profile digest
  → versioned telemetry envelope
  → MQTT QoS 1
  → local fsync JSONL buffer on failure
  → replay after broker recovery
```

Default topic:

```text
telemetry/<tenant>/<site>/<device>/measurements
```

## Supported mappings

- coils and discrete inputs;
- holding and input registers;
- `bool`, `uint16`, `int16`, `uint32`, `int32`, `float32`, and `float64`;
- configurable word and byte order;
- scale, offset, and precision;
- integer bit slices;
- enum maps;
- `uint16` named bit expansion;
- required versus optional parameters;
- deterministic profile digests.

## Quick start without hardware

```bash
git clone https://github.com/FrankFMY/modbus-edge-agent.git
cd modbus-edge-agent
python3 -m venv .venv
.venv/bin/pip install .

.venv/bin/modbus-edge-agent   --config examples/config.example.json   --validate-config

.venv/bin/modbus-edge-agent   --config examples/config.example.json   --dry-run   --no-mqtt   --once
```

The example is synthetic and uses generated identities only.

## Configuration

Start with [examples/config.example.json](examples/config.example.json). Keep secrets in environment variables referenced by `username_env`, `password_env`, and `auth_token_env`; do not put production credentials into shared JSON.

Config protocol: `modbus-edge-agent.config.v1`.

## Linux installation

Review the script and example configuration first, then run as root:

```bash
packaging/install.sh --config=examples/config.example.json
```

The installer:

- creates the locked `modbus-edge` service account;
- grants serial access through `dialout`;
- installs a dedicated virtual environment;
- preserves `/etc/modbus-edge-agent/config.json` unless `--overwrite-config` is explicit;
- validates configuration as the service account;
- restarts only if the service was already active;
- does not enable a first installation.

## Hardware acceptance

Software dry-run is not hardware evidence. Before enabling a field service, complete [docs/FIELD_ACCEPTANCE.md](docs/FIELD_ACCEPTANCE.md).

## Wire contract

See [docs/WIRE_PROTOCOL.md](docs/WIRE_PROTOCOL.md) and [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## Package status

The GitHub repository is public. PyPI publication and container images are intentionally deferred until a separate release gate.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
bash -n packaging/install.sh
```

## Author

**Artem Prianishnikov**

- GitHub: https://github.com/FrankFMY
- Website: https://frankfmy.com
- Email: Pryanishnikovartem@gmail.com

## License

Apache-2.0 © 2026 Artem Prianishnikov. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
