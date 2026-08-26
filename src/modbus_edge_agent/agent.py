#!/usr/bin/env python3
"""
Reference read-only Modbus edge gateway agent.

The agent polls local Modbus RTU devices, maps registers into semantic
semantic telemetry parameters, stores a small retry buffer, and publishes MQTT payloads.

It is intentionally conservative:
- SCADA stays the command/control authority.
- Modbus is polled locally; raw Modbus is not tunneled over WAN.
- MQTT payloads use a strict versioned telemetry contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("modbus-edge-agent")
TOPIC_TEMPLATE = "telemetry/{tenant_slug}/{site_slug}/{device_id}/measurements"
CONFIG_VERSION = "modbus-edge-agent.config.v1"


class ConfigError(Exception):
    pass


class PollError(Exception):
    pass


@dataclass(frozen=True)
class RegisterMapping:
    param_code: str
    function: str
    address: int
    count: int
    data_type: str
    scale: float
    offset: float
    precision: int | None
    required: bool
    word_order: str
    byte_order: str
    bit_offset: int | None = None
    bit_length: int | None = None
    enum_map: dict[str, str] = field(default_factory=dict)
    bit_params: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    slave_id: int
    poll_interval_seconds: int
    auth_token: str
    profile_version: int
    profile_digest: str
    registers: list[RegisterMapping]


@dataclass(frozen=True)
class AgentConfig:
    raw: dict[str, Any]
    tenant_slug: str
    site_slug: str
    gateway_id: str
    default_poll_interval_seconds: int
    buffer_dir: Path
    mqtt: dict[str, Any]
    modbus: dict[str, Any]
    devices: list[DeviceConfig]


class JsonlBuffer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, topic: str, payload: dict[str, Any]) -> None:
        record = {"topic": topic, "payload": payload, "buffered_at": utc_now()}
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(self.path, 0o600)

    def replay(self, publisher: "Publisher") -> int:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0

        remaining: list[dict[str, Any]] = []
        replayed = 0
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    record = json.loads(line)
                    topic = str(record["topic"])
                    payload = record["payload"]
                except Exception as exc:  # noqa: BLE001 - bad buffer rows must not stop field polling
                    LOGGER.warning("dropping invalid buffer row: %s", exc)
                    continue

                if publisher.publish(topic, payload):
                    replayed += 1
                else:
                    remaining.append(record)

        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as file:
            for record in remaining:
                file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        return replayed


class Publisher:
    def __init__(self, config: AgentConfig, no_mqtt: bool) -> None:
        self.config = config
        self.no_mqtt = no_mqtt
        self.client = None
        self.device_tokens = {device.device_id: device.auth_token for device in config.devices if device.auth_token}

    def connect(self) -> None:
        if self.no_mqtt:
            LOGGER.info("MQTT disabled; payloads will be printed only")
            return

        try:
            import paho.mqtt.client as mqtt
        except ImportError as exc:
            raise RuntimeError("paho-mqtt is required for MQTT publishing") from exc

        mqtt_config = self.config.mqtt
        client_id = str(mqtt_config.get("client_id") or self.config.gateway_id)
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
        )

        username = str(mqtt_config.get("username") or os.environ.get(str(mqtt_config.get("username_env") or ""), ""))
        password = str(mqtt_config.get("password") or os.environ.get(str(mqtt_config.get("password_env") or ""), ""))
        if username:
            self.client.username_pw_set(username, password)

        tls_config = mqtt_config.get("tls") or {}
        if bool(tls_config.get("enabled")):
            self.client.tls_set(
                ca_certs=empty_to_none(tls_config.get("ca_file")),
                certfile=empty_to_none(tls_config.get("cert_file")),
                keyfile=empty_to_none(tls_config.get("key_file")),
            )

        host = str(mqtt_config.get("host") or "localhost")
        port = int(mqtt_config.get("port") or 1883)
        keepalive = int(mqtt_config.get("keepalive_seconds") or 60)
        LOGGER.info("connecting to MQTT broker %s:%s", host, port)
        self.client.connect(host, port, keepalive=keepalive)
        self.client.loop_start()

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        if self.no_mqtt:
            print(json.dumps({"topic": topic, "payload": payload}, ensure_ascii=False))
            return True
        if self.client is None:
            return False

        qos = int(self.config.mqtt.get("qos") or 1)
        wire_payload = dict(payload)
        device_id = str(wire_payload.get("device_id") or "")
        token = self.device_tokens.get(device_id, "")
        if token:
            wire_payload["auth_token"] = token
        try:
            info = self.client.publish(topic, json.dumps(wire_payload, ensure_ascii=False), qos=qos)
            info.wait_for_publish(timeout=10)
            return info.is_published()
        except Exception as exc:  # noqa: BLE001 - field agent should buffer and continue
            LOGGER.warning("MQTT publish failed for %s: %s", topic, exc)
            return False


class ModbusPoller:
    def __init__(self, config: AgentConfig, dry_run: bool) -> None:
        self.config = config
        self.dry_run = dry_run
        self.client = None
        self.tick = 0

    def connect(self) -> None:
        if self.dry_run:
            LOGGER.info("dry-run Modbus mode enabled")
            return

        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError as exc:
            raise RuntimeError("pymodbus and pyserial are required for real Modbus RTU polling") from exc

        modbus = self.config.modbus
        self.client = ModbusSerialClient(
            port=str(modbus.get("port") or "/dev/ttyUSB0"),
            baudrate=int(modbus.get("baudrate") or 9600),
            bytesize=int(modbus.get("bytesize") or 8),
            parity=str(modbus.get("parity") or "N"),
            stopbits=int(modbus.get("stopbits") or 1),
            timeout=float(modbus.get("timeout_seconds") or 2),
        )
        if not self.client.connect():
            raise RuntimeError("failed to connect Modbus RTU client")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def poll_device(self, device: DeviceConfig) -> dict[str, Any]:
        self.tick += 1
        params: dict[str, Any] = {}
        errors: list[str] = []

        for mapping in device.registers:
            try:
                values = self._read_mapping(device.slave_id, mapping)
                decoded = decode_mapping(values, mapping)
                if isinstance(decoded, dict):
                    params.update(decoded)
                else:
                    params[mapping.param_code] = decoded
            except Exception as exc:  # noqa: BLE001 - keep polling other params
                message = f"{mapping.param_code}: {exc}"
                if mapping.required:
                    errors.append(message)
                LOGGER.warning("poll error for %s slave=%s %s", device.device_id, device.slave_id, message)

        if errors:
            raise PollError("; ".join(errors))
        return params

    def _read_mapping(self, slave_id: int, mapping: RegisterMapping) -> list[int] | list[bool]:
        if self.dry_run:
            return self._dry_values(slave_id, mapping)
        if self.client is None:
            raise PollError("Modbus client is not connected")

        function = normalized_function(mapping.function)
        if function == "holding_registers":
            result = call_modbus_read(self.client.read_holding_registers, mapping.address, mapping.count, slave_id)
        elif function == "input_registers":
            result = call_modbus_read(self.client.read_input_registers, mapping.address, mapping.count, slave_id)
        elif function == "coils":
            result = call_modbus_read(self.client.read_coils, mapping.address, mapping.count, slave_id)
        elif function == "discrete_inputs":
            result = call_modbus_read(self.client.read_discrete_inputs, mapping.address, mapping.count, slave_id)
        else:
            raise PollError(f"unsupported function {mapping.function}")

        if result.isError():
            raise PollError(str(result))
        if function in {"coils", "discrete_inputs"}:
            return list(result.bits[: mapping.count])
        return list(result.registers[: mapping.count])

    def _dry_values(self, slave_id: int, mapping: RegisterMapping) -> list[int] | list[bool]:
        phase = self.tick + slave_id + mapping.address
        if normalized_function(mapping.function) in {"coils", "discrete_inputs"}:
            return [phase % 2 == 0]

        base = 100 + slave_id * 10 + mapping.address
        if mapping.param_code in {"pressure"}:
            raw = int((17.0 + math.sin(phase / 5.0) * 2.0) / max(mapping.scale, 0.1))
        elif mapping.param_code in {"reagent_level"}:
            raw = int((75.0 - (phase % 20) * 0.4) / max(mapping.scale, 0.1))
        elif mapping.param_code in {"flow_rate"}:
            raw = int(0.8 / max(mapping.scale, 0.01))
        else:
            raw = base
        return encode_raw_for_dry_run(raw, mapping)


def load_config(path: Path) -> AgentConfig:
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    validate_raw_config(raw)

    gateway = raw["gateway"]
    mqtt = raw["mqtt"]
    modbus = raw["modbus"]
    devices: list[DeviceConfig] = []
    default_poll = int(gateway.get("default_poll_interval_seconds") or 10)

    for device in raw["devices"]:
        registers = [
            RegisterMapping(
                param_code=str(item["param_code"]),
                function=str(item.get("function") or "holding_registers"),
                address=int(item["address"]),
                count=int(item.get("count") or default_count_for_type(str(item.get("data_type") or "uint16"))),
                data_type=str(item.get("data_type") or "uint16"),
                scale=float(item.get("scale", 1)),
                offset=float(item.get("offset", 0)),
                precision=none_or_int(item.get("precision")),
                required=bool(item.get("required", True)),
                word_order=str(item.get("word_order") or "big"),
                byte_order=str(item.get("byte_order") or "big"),
                bit_offset=none_or_int(item.get("bit_offset")),
                bit_length=none_or_int(item.get("bit_length")),
                enum_map=parse_enum_map(item.get("enum_map")),
                bit_params=parse_bit_params(item.get("bit_params")),
            )
            for item in device["registers"]
        ]
        token = str(device.get("auth_token") or "")
        token_env = str(device.get("auth_token_env") or "")
        if token_env:
            token = os.getenv(token_env, token)
        profile_version = max(1, int(device.get("profile_version") or 1))
        profile_digest = str(device.get("profile_digest") or "").strip()
        if not profile_digest:
            profile_material = json.dumps(
                {"profile_version": profile_version, "registers": device["registers"]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            profile_digest = hashlib.sha256(profile_material).hexdigest()
        devices.append(
            DeviceConfig(
                device_id=str(device["device_id"]),
                slave_id=int(device["slave_id"]),
                poll_interval_seconds=int(device.get("poll_interval_seconds") or default_poll),
                auth_token=token,
                profile_version=profile_version,
                profile_digest=profile_digest,
                registers=registers,
            )
        )

    return AgentConfig(
        raw=raw,
        tenant_slug=str(gateway["tenant_slug"]),
        site_slug=str(gateway["site_slug"]),
        gateway_id=str(gateway.get("gateway_id") or "modbus-edge-agent"),
        default_poll_interval_seconds=default_poll,
        buffer_dir=Path(str(gateway.get("buffer_dir") or "./edge-buffer")),
        mqtt=mqtt,
        modbus=modbus,
        devices=devices,
    )


def validate_raw_config(raw: dict[str, Any]) -> None:
    if raw.get("version") != CONFIG_VERSION:
        raise ConfigError(f"config version must be {CONFIG_VERSION}")
    for section in ("gateway", "mqtt", "modbus", "devices"):
        if section not in raw:
            raise ConfigError(f"missing config section: {section}")
    gateway = raw["gateway"]
    for key in ("tenant_slug", "site_slug"):
        if not gateway.get(key):
            raise ConfigError(f"gateway.{key} is required")
    if "synthetic_source" in gateway and not isinstance(gateway["synthetic_source"], bool):
        raise ConfigError("gateway.synthetic_source must be a boolean")
    if not isinstance(raw["devices"], list) or not raw["devices"]:
        raise ConfigError("devices must be a non-empty list")
    for idx, device in enumerate(raw["devices"]):
        for key in ("device_id", "slave_id", "registers"):
            if key not in device:
                raise ConfigError(f"devices[{idx}].{key} is required")
        if not isinstance(device["registers"], list) or not device["registers"]:
            raise ConfigError(f"devices[{idx}].registers must be a non-empty list")
        for reg_idx, register in enumerate(device["registers"]):
            for key in ("param_code", "address"):
                if key not in register:
                    raise ConfigError(f"devices[{idx}].registers[{reg_idx}].{key} is required")


def decode_mapping(values: list[int] | list[bool], mapping: RegisterMapping) -> int | float | str | dict[str, int | float | str]:
    data_type = mapping.data_type.lower()
    if data_type in {"bool", "bit"}:
        raw = 1 if bool(values[0]) else 0
    elif data_type in {"uint16", "int16"}:
        register = normalize_byte_order([int(values[0])], mapping.byte_order)[0]
        raw = signed16(register) if data_type == "int16" else register
    elif data_type in {"uint32", "int32", "float32", "float64"}:
        expected = 4 if data_type == "float64" else 2
        if len(values) < expected:
            raise PollError(f"{data_type} requires {expected} registers")
        registers = normalize_word_order([int(value) for value in values[:expected]], mapping.word_order)
        registers = normalize_byte_order(registers, mapping.byte_order)
        if data_type == "float32":
            raw = struct.unpack(">f", struct.pack(">HH", registers[0], registers[1]))[0]
        elif data_type == "float64":
            raw = struct.unpack(">d", struct.pack(">HHHH", *registers))[0]
        else:
            raw = (registers[0] << 16) | registers[1]
            if data_type == "int32" and raw & 0x80000000:
                raw -= 0x100000000
    else:
        raise PollError(f"unsupported data_type {mapping.data_type}")

    if mapping.bit_offset is not None:
        widths = {"bool": 1, "bit": 1, "uint16": 16, "int16": 16, "uint32": 32, "int32": 32}
        if data_type not in widths:
            raise PollError(f"bit slicing is not supported for {mapping.data_type}")
        width = mapping.bit_length or 1
        if mapping.bit_offset + width > widths[data_type]:
            raise PollError(f"bit slice exceeds {data_type} width")
        mask = (1 << width) - 1
        raw = (int(raw) >> mapping.bit_offset) & mask

    value = float(raw) * mapping.scale + mapping.offset
    if mapping.precision is not None:
        value = round(value, mapping.precision)
    if data_type in {"bool", "bit"}:
        return int(value)
    if mapping.enum_map:
        enum_key = str(int(value)) if float(value).is_integer() else str(value)
        if enum_key in mapping.enum_map:
            return mapping.enum_map[enum_key]
    if data_type == "uint16" and mapping.bit_params:
        raw_int = int(raw) & 0xFFFF
        decoded: dict[str, int | float | str] = {mapping.param_code: raw_int}
        for param_code, bit in mapping.bit_params.items():
            decoded[param_code] = 1 if raw_int & (1 << bit) else 0
        return decoded
    return value


def call_modbus_read(method: Any, address: int, count: int, slave_id: int) -> Any:
    for slave_kw in ("slave", "unit", "device_id"):
        try:
            return method(address=address, count=count, **{slave_kw: slave_id})
        except TypeError:
            continue
    return method(address=address, count=count)


def normalize_word_order(registers: list[int], word_order: str) -> list[int]:
    if word_order.lower() == "little":
        return list(reversed(registers))
    return registers


def normalize_byte_order(registers: list[int], byte_order: str) -> list[int]:
    if byte_order.lower() != "little":
        return registers
    return [((value & 0xFF) << 8) | ((value >> 8) & 0xFF) for value in registers]


def encode_raw_for_dry_run(raw: int, mapping: RegisterMapping) -> list[int]:
    data_type = mapping.data_type.lower()
    if data_type in {"uint16", "int16"}:
        return [raw & 0xFFFF]
    if data_type in {"uint32", "int32"}:
        hi = (raw >> 16) & 0xFFFF
        lo = raw & 0xFFFF
        return normalize_byte_order(normalize_word_order([hi, lo], mapping.word_order), mapping.byte_order)
    if data_type == "float32":
        hi, lo = struct.unpack(">HH", struct.pack(">f", float(raw)))
        return normalize_byte_order(normalize_word_order([hi, lo], mapping.word_order), mapping.byte_order)
    if data_type == "float64":
        registers = list(struct.unpack(">HHHH", struct.pack(">d", float(raw))))
        return normalize_byte_order(normalize_word_order(registers, mapping.word_order), mapping.byte_order)
    return [raw & 0xFFFF]


def build_payload(
    config: AgentConfig,
    device: DeviceConfig,
    params: dict[str, Any],
    *,
    synthetic: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ts": utc_now(),
        "device_id": device.device_id,
        "params": params,
        "meta": {
            "source": "edge_agent",
            "synthetic": synthetic,
            "gateway_id": config.gateway_id,
            "profile_version": device.profile_version,
            "profile_digest": device.profile_digest,
        },
    }
    return payload


def topic_for(config: AgentConfig, device: DeviceConfig) -> str:
    return TOPIC_TEMPLATE.format(
        tenant_slug=config.tenant_slug,
        site_slug=config.site_slug,
        device_id=device.device_id,
    )


def payload_is_synthetic(config: AgentConfig, *, dry_run: bool) -> bool:
    gateway = config.raw.get("gateway")
    configured = isinstance(gateway, dict) and gateway.get("synthetic_source") is True
    return dry_run or configured


def run_agent(config: AgentConfig, *, once: bool, dry_run: bool, no_mqtt: bool) -> None:
    poller = ModbusPoller(config, dry_run=dry_run)
    publisher = Publisher(config, no_mqtt=no_mqtt)
    buffer = JsonlBuffer(config.buffer_dir / "mqtt-buffer.jsonl")
    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    poller.connect()
    publisher.connect()
    next_poll = {device.device_id: 0.0 for device in config.devices}

    try:
        while running:
            replayed = buffer.replay(publisher)
            if replayed:
                LOGGER.info("replayed %s buffered MQTT payloads", replayed)

            now = time.monotonic()
            did_work = False
            for device in config.devices:
                if now < next_poll[device.device_id]:
                    continue
                did_work = True
                next_poll[device.device_id] = now + device.poll_interval_seconds
                try:
                    params = poller.poll_device(device)
                    payload = build_payload(
                        config,
                        device,
                        params,
                        synthetic=payload_is_synthetic(config, dry_run=dry_run),
                    )
                    topic = topic_for(config, device)
                    if publisher.publish(topic, payload):
                        LOGGER.info("published %s params=%s", device.device_id, len(params))
                    else:
                        LOGGER.warning("buffering payload for %s", device.device_id)
                        buffer.append(topic, payload)
                except PollError as exc:
                    LOGGER.error("poll failed for %s: %s", device.device_id, exc)

            if once:
                break
            time.sleep(0.2 if did_work else 1.0)
    finally:
        poller.close()
        publisher.close()


def normalized_function(value: str) -> str:
    normalized = value.lower().strip()
    aliases = {
        "03": "holding_registers",
        "3": "holding_registers",
        "read_holding_registers": "holding_registers",
        "04": "input_registers",
        "4": "input_registers",
        "read_input_registers": "input_registers",
        "01": "coils",
        "1": "coils",
        "read_coils": "coils",
        "02": "discrete_inputs",
        "2": "discrete_inputs",
        "read_discrete_inputs": "discrete_inputs",
    }
    return aliases.get(normalized, normalized)


def default_count_for_type(data_type: str) -> int:
    if data_type.lower() == "float64":
        return 4
    if data_type.lower() in {"uint32", "int32", "float32"}:
        return 2
    return 1


def signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def none_or_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def parse_enum_map(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("enum_map must be an object")
    return {str(key): str(label) for key, label in value.items()}


def parse_bit_params(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("bit_params must be an object of param_code -> bit index")
    parsed: dict[str, int] = {}
    for param_code, bit in value.items():
        bit_index = int(bit)
        if bit_index < 0 or bit_index > 15:
            raise ConfigError(f"bit_params.{param_code} bit index must be 0..15")
        parsed[str(param_code)] = bit_index
    return parsed


def empty_to_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Modbus edge gateway agent")
    parser.add_argument("--config", required=True, help="Path to agent config JSON")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Use deterministic fake Modbus values")
    parser.add_argument("--no-mqtt", action="store_true", help="Print payloads instead of publishing MQTT")
    parser.add_argument("--validate-config", action="store_true", help="Validate config and exit")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(Path(args.config))
        if args.validate_config:
            print(json.dumps({"ok": True, "version": CONFIG_VERSION, "devices": len(config.devices)}, indent=2))
            return 0
        run_agent(config, once=args.once, dry_run=args.dry_run, no_mqtt=args.no_mqtt)
        return 0
    except (ConfigError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
