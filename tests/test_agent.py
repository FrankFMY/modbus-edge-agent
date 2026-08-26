import json
import tempfile
import unittest
from pathlib import Path

from modbus_edge_agent import agent


def mapping(**overrides):
    data = {
        "param_code": "error_word",
        "function": "holding_registers",
        "address": 50,
        "count": 1,
        "data_type": "uint16",
        "scale": 1,
        "offset": 0,
        "precision": 0,
        "required": True,
        "word_order": "big",
        "byte_order": "big",
        "bit_params": {
            "alarm_temperature_sensor_fault": 0,
            "alarm_level_low": 4,
            "alarm_pressure_high": 7,
        },
    }
    data.update(overrides)
    return agent.RegisterMapping(**data)


class DecodeMappingBitParamsTest(unittest.TestCase):
    def test_decode_mapping_expands_uint16_bit_params_with_raw_value(self):
        result = agent.decode_mapping([0b10010001], mapping())

        self.assertEqual(result, {
            "error_word": 145,
            "alarm_temperature_sensor_fault": 1,
            "alarm_level_low": 1,
            "alarm_pressure_high": 1,
        })
        self.assertIs(type(result["error_word"]), int)

    def test_decode_mapping_expands_inactive_uint16_bit_params_to_zero(self):
        result = agent.decode_mapping([0], mapping())

        self.assertEqual(result, {
            "error_word": 0,
            "alarm_temperature_sensor_fault": 0,
            "alarm_level_low": 0,
            "alarm_pressure_high": 0,
        })


class DecodeWireContractTest(unittest.TestCase):
    def test_float64_round_trip_for_all_endian_combinations(self):
        for word_order in ("big", "little"):
            for byte_order in ("big", "little"):
                item = mapping(
                    param_code="energy_total",
                    count=4,
                    data_type="float64",
                    precision=6,
                    word_order=word_order,
                    byte_order=byte_order,
                    bit_params={},
                )
                registers = agent.encode_raw_for_dry_run(1234, item)
                self.assertAlmostEqual(agent.decode_mapping(registers, item), 1234.0, places=6)

    def test_uint16_honors_little_byte_order(self):
        item = mapping(byte_order="little", bit_params={})
        self.assertEqual(agent.decode_mapping([0x3412], item), 0x1234)

    def test_out_of_width_bit_slice_is_rejected(self):
        item = mapping(bit_offset=15, bit_length=2, bit_params={})
        with self.assertRaisesRegex(agent.PollError, "exceeds uint16 width"):
            agent.decode_mapping([0xFFFF], item)

    def test_float_bit_slice_is_rejected(self):
        item = mapping(data_type="float32", count=2, bit_offset=0, bit_length=1, bit_params={})
        with self.assertRaisesRegex(agent.PollError, "not supported for float32"):
            agent.decode_mapping([0x3F80, 0], item)

    def test_bit_slice_and_enum_map(self):
        sliced = mapping(param_code="mode_bits", bit_offset=4, bit_length=3, bit_params={})
        self.assertEqual(agent.decode_mapping([0b01110000], sliced), 7.0)
        enumerated = mapping(param_code="state", enum_map={"2": "running"}, bit_params={})
        self.assertEqual(agent.decode_mapping([2], enumerated), "running")


class ConfigValidationTest(unittest.TestCase):
    def test_synthetic_source_must_be_boolean(self):
        raw = {
            "version": agent.CONFIG_VERSION,
            "gateway": {
                "tenant_slug": "tenant",
                "site_slug": "site",
                "synthetic_source": "true",
            },
            "mqtt": {},
            "modbus": {},
            "devices": [
                {
                    "device_id": "pump-1",
                    "slave_id": 1,
                    "registers": [{"param_code": "pressure", "address": 44}],
                }
            ],
        }

        with self.assertRaisesRegex(agent.ConfigError, "gateway.synthetic_source must be a boolean"):
            agent.validate_raw_config(raw)


class DryRunEvidenceTest(unittest.TestCase):
    def test_dry_run_payload_is_marked_synthetic(self):
        device = agent.DeviceConfig(
            device_id="pump-1",
            slave_id=1,
            poll_interval_seconds=10,
            auth_token="",
            profile_version=1,
            profile_digest="abc123",
            registers=[],
        )
        config = agent.AgentConfig(
            raw={"version": "modbus-edge-agent.config.v1"},
            tenant_slug="tenant",
            site_slug="site",
            gateway_id="gw-1",
            default_poll_interval_seconds=10,
            buffer_dir=Path("/tmp"),
            mqtt={},
            modbus={},
            devices=[device],
        )

        payload = agent.build_payload(config, device, {"pressure": 18.4}, synthetic=True)

        self.assertIs(payload["meta"]["synthetic"], True)

    def test_lab_fixture_config_marks_real_transport_payload_synthetic(self):
        config = agent.AgentConfig(
            raw={
                "version": "modbus-edge-agent.config.v1",
                "gateway": {"synthetic_source": True},
            },
            tenant_slug="tenant",
            site_slug="site",
            gateway_id="gw-1",
            default_poll_interval_seconds=10,
            buffer_dir=Path("/tmp"),
            mqtt={},
            modbus={},
            devices=[],
        )

        self.assertIs(agent.payload_is_synthetic(config, dry_run=False), True)


class SecretCustodyTest(unittest.TestCase):
    def test_payload_and_durable_buffer_never_contain_device_token(self):
        device = agent.DeviceConfig(
            device_id="pump-1",
            slave_id=1,
            poll_interval_seconds=10,
            auth_token="device_super_secret",
            profile_version=3,
            profile_digest="abc123",
            registers=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            config = agent.AgentConfig(
                raw={"version": "modbus-edge-agent.config.v1"},
                tenant_slug="tenant",
                site_slug="site",
                gateway_id="gw-1",
                default_poll_interval_seconds=10,
                buffer_dir=Path(directory),
                mqtt={},
                modbus={},
                devices=[device],
            )
            payload = agent.build_payload(config, device, {"pressure": 18.4})
            self.assertNotIn("auth_token", payload)
            self.assertEqual(payload["meta"]["profile_version"], 3)
            buffer = agent.JsonlBuffer(Path(directory) / "mqtt-buffer.jsonl")
            buffer.append(agent.topic_for(config, device), payload)
            persisted = (Path(directory) / "mqtt-buffer.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("device_super_secret", persisted)
            self.assertEqual(json.loads(persisted)["payload"]["device_id"], "pump-1")


if __name__ == "__main__":
    unittest.main()
