# Field Acceptance

Do not enable the service on a live industrial bus until these checks are complete.

## Before connection

- Confirm gateway model, OS, serial device, and recovery access.
- Confirm RS-485 A/B/GND, isolation, termination, and topology.
- Confirm that adding another Modbus master is permitted.
- Confirm slave IDs, baud rate, parity, stop bits, timeout, and polling interval.
- Review the exact register map and allowed read function codes.
- Confirm address base and byte/word order.

## Bench proof

1. Validate configuration.
2. Run `--dry-run --no-mqtt --once` and confirm `synthetic=true`.
3. Read one known real register without MQTT.
4. Compare the value with an independent display or trusted instrument.
5. Validate integer, float, bitfield, scale, and unit mappings.
6. Publish to a test MQTT principal with exact ACLs.
7. Interrupt broker connectivity and prove buffer/replay.
8. Reboot and prove service/config/buffer ownership.
9. Review logs for repeated timeout, CRC, reconnect, or permission failures.
10. Enable the service only after the read-only boundary is accepted.

## Not proven by dry-run

Dry-run does not prove wiring, slave identity, serial timing, real units, byte order, bus non-interference, LTE/VPN reachability, broker ACLs, reboot behavior, or field accuracy.
