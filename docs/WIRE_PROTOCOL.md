# Wire Protocol

## Topic

```text
telemetry/<tenant_slug>/<site_slug>/<device_id>/measurements
```

Topic segments are configuration identities. Broker ACLs should bind a gateway identity to only its permitted publish topics.

## Payload

```json
{
  "ts": "2026-01-01T12:00:00Z",
  "device_id": "pump-01",
  "params": {
    "pressure_bar": 12.4,
    "alarm_pressure_high": 0
  },
  "meta": {
    "source": "edge_agent",
    "synthetic": false,
    "gateway_id": "gateway-01",
    "profile_version": 1,
    "profile_digest": "sha256-hex"
  }
}
```

A device authentication token may be added to the in-memory MQTT wire payload. It is deliberately absent from the durable local buffer and debug output.

## Provenance

`meta.synthetic` is mandatory evidence context:

- `true` for `--dry-run`;
- `true` when the configuration declares `gateway.synthetic_source=true`;
- `false` only for real polling under a non-synthetic configuration.

Consumers must exclude synthetic telemetry from operational, SLA, alert, maintenance, and go-live evidence unless a separate lab view explicitly requests it.

## Profile digest

If a digest is not supplied, the agent computes SHA-256 over canonical JSON containing the profile version and register definitions. A wire-level map change therefore changes profile identity.
