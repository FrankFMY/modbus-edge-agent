# Security Model

## Credentials

MQTT username/password and per-device tokens should come from environment variables. The JSON config stores variable names, not values. The agent injects a device token only immediately before MQTT publication.

The retry buffer stores topic, credential-free payload, and buffer time. File mode is forced to `0600` after writes and atomic replay replacement.

## Transport

Use MQTT TLS with a trusted CA in production. Broker ACLs should restrict a principal to exact topic prefixes. Payload credentials complement broker policy; they do not replace TLS or ACLs.

## Serial boundary

The service account requires access to the selected serial device, commonly through `dialout`. The agent implements Modbus read functions only. Adding write/control functions is a separate safety-sensitive protocol and is intentionally outside this repository.

## Failure handling

A required register failure fails that device poll while allowing other devices/cycles to continue. MQTT failure buffers the credential-free payload. Corrupt buffer rows are dropped with warnings rather than blocking all future polling.

## Scope exclusions

This package does not provide broker deployment, cloud ingestion, device provisioning, remote update signing, fleet management, or field safety approval.
