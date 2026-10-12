"""Verify recorded files with the standard MCAP reader and real MAVLink data."""
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcap.reader import make_reader
from pymavlink import mavutil

from heron.utils.logger import HeronLogger, default_log_directory


class LogDirectoryTests(unittest.TestCase):
    def test_uses_os_documents_folder_and_creates_log_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            documents = Path(temp) / 'Redirected Documents'
            with patch.dict(os.environ, {}, clear=True), patch(
                'heron.utils.logger.user_documents_path', return_value=documents
            ) as resolve_documents:
                directory = default_log_directory()
            resolve_documents.assert_called_once_with()
            self.assertEqual(directory, documents / 'Heron' / 'logs')
            with patch('heron.utils.logger.DEFAULT_LOG_DIR', directory):
                with HeronLogger() as logger:
                    self.assertTrue(logger.output_path.is_file())
                with logger.output_path.open('rb') as stream:
                    self.assertEqual(list(make_reader(stream).iter_messages()), [])

    def test_explicit_directory_override_is_preserved(self):
        with patch.dict(os.environ, {'HERON_LOG_DIR': '~/custom-logs'}), patch(
            'heron.utils.logger.user_documents_path'
        ) as resolve_documents:
            self.assertEqual(default_log_directory(), Path.home() / 'custom-logs')
        resolve_documents.assert_not_called()

    def test_empty_override_uses_documents(self):
        with patch.dict(os.environ, {'HERON_LOG_DIR': ''}), patch(
            'heron.utils.logger.user_documents_path', return_value=Path('/documents')
        ):
            self.assertEqual(default_log_directory(), Path('/documents/Heron/logs'))


class McapLoggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'session.mcap'
        self.encoder = mavutil.mavlink.MAVLink(None, srcSystem=3, srcComponent=1)

    def heartbeat(self):
        message = self.encoder.heartbeat_encode(
            mavutil.mavlink.MAV_TYPE_SURFACE_BOAT,
            mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            128, 10, mavutil.mavlink.MAV_STATE_ACTIVE)
        message.pack(self.encoder)
        return message

    def read(self):
        with self.path.open('rb') as stream:
            reader = make_reader(stream, validate_crcs=True)
            return list(reader.iter_messages()), reader.get_summary()

    def test_round_trip_preserves_fields_channels_timestamps_and_indexes(self):
        timestamp = 1_791_756_123_123_456_789
        with HeronLogger(output_path=self.path) as logger:
            with patch('heron.utils.logger.time.time_ns', return_value=timestamp):
                logger.log_message(self.heartbeat())
            attitude = self.encoder.attitude_encode(42, math.nan, 0.2, 0.3, 0, 0, 0)
            attitude.pack(self.encoder)
            logger.log_message(attitude)
            logger.log_message(self.heartbeat())
        self.assertEqual(self.path.read_bytes()[:8], b'\x89MCAP0\r\n')
        messages, summary = self.read()
        self.assertEqual(len(messages), 3)
        self.assertEqual(summary.statistics.message_count, 3)
        self.assertTrue(summary.chunk_indexes)
        self.assertEqual(summary.chunk_indexes[0].compression, 'zstd')
        self.assertEqual({channel.topic for _, channel, _ in messages},
                         {'/mavlink/HEARTBEAT', '/mavlink/ATTITUDE'})
        schema, channel, message = messages[0]
        self.assertEqual(message.log_time, timestamp)
        self.assertEqual(message.publish_time, timestamp)
        self.assertEqual(channel.message_encoding, 'json')
        self.assertEqual(schema.encoding, 'jsonschema')
        self.assertEqual(json.loads(schema.data)['properties']['data']['type'], 'object')
        payload = json.loads(message.data)
        self.assertEqual(payload['sys_id'], 3)
        self.assertEqual(payload['comp_id'], 1)
        self.assertEqual(payload['data']['custom_mode'], 10)
        self.assertEqual(payload['msg_type'], 'HEARTBEAT')
        attitude = next(json.loads(message.data) for _, channel, message in messages
                        if channel.topic == '/mavlink/ATTITUDE')
        self.assertIsNone(attitude['data']['roll'])
        self.assertAlmostEqual(attitude['data']['pitch'], 0.2)

    def test_filter_and_idempotent_open_close(self):
        logger = HeronLogger(output_path=self.path, msg_types=['heartbeat'])
        logger.open()
        logger.log_message(self.heartbeat())
        logger.open()  # Must not truncate an already open session.
        logger.log_message(self.encoder.attitude_encode(0, 0, 0, 0, 0, 0, 0))
        logger.log_message(None)
        logger.close()
        logger.close()
        messages, _ = self.read()
        self.assertEqual(len(messages), 1)
        self.assertEqual(logger.msg_count, 1)

    def test_empty_session_is_a_readable_mcap(self):
        with HeronLogger(output_path=self.path):
            pass
        messages, summary = self.read()
        self.assertEqual(messages, [])
        self.assertEqual(summary.statistics.message_count, 0)

    def test_generated_and_explicit_paths_use_mcap_suffix(self):
        with patch('heron.utils.logger.DEFAULT_LOG_DIR', Path(self.temp.name)):
            first = HeronLogger()
            second = HeronLogger()
        self.assertEqual(first.output_path.suffix, '.mcap')
        self.assertNotEqual(first.output_path, second.output_path)
        explicit = HeronLogger(output_path=Path(self.temp.name) / 'old.jsonl')
        self.assertEqual(explicit.output_path.suffix, '.mcap')
        with self.assertRaises(ValueError):
            HeronLogger(fmt='json')

    def test_periodic_flush_keeps_completed_chunks_readable_before_close(self):
        from mcap.stream_reader import StreamReader
        from mcap.records import Message
        from mcap.exceptions import EndOfFile
        logger = HeronLogger(output_path=self.path)
        logger.open()
        with patch('heron.utils.logger.time.monotonic', return_value=logger._last_flush + 2):
            logger.log_message(self.heartbeat())
        # A live file has no footer yet. Sequential reading can recover its
        # flushed records even when a process exits without finish().
        messages = []
        with self.path.open('rb') as stream:
            try:
                for record in StreamReader(stream).records:
                    if isinstance(record, Message):
                        messages.append(record)
            except EndOfFile:
                pass
        logger.close()
        self.assertEqual(len(messages), 1)


if __name__ == '__main__':
    unittest.main()
