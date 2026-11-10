import fcntl
import http.server
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'hooks'))
import limit_restore_notify as notify  # noqa: E402

UTC = timezone.utc
AT = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


class WithLogDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        patcher = mock.patch.object(notify, 'DIR', self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def log_text(self):
        return Path(self.dir, 'log.txt').read_text()


class ParseResetTest(unittest.TestCase):
    def test_a_time_later_today_is_today(self):
        self.assertEqual(notify.parse_reset('resets 2:20pm (UTC)', AT), datetime(2026, 10, 20, 14, 20, tzinfo=UTC))

    def test_a_time_already_past_is_tomorrow(self):
        self.assertEqual(notify.parse_reset('resets 9am (UTC)', AT), datetime(2026, 10, 21, 9, 0, tzinfo=UTC))

    def test_noon_and_midnight(self):
        self.assertEqual(notify.parse_reset('resets 12pm (UTC)', AT), datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
        self.assertEqual(notify.parse_reset('resets 12am (UTC)', AT), datetime(2026, 10, 21, 0, 0, tzinfo=UTC))

    def test_a_dated_reset_is_converted_from_its_zone(self):
        reset = notify.parse_reset('resets Oct 26, 3am (Europe/Lisbon)', AT)
        self.assertEqual(reset, datetime(2026, 10, 26, 3, 0, tzinfo=UTC))
        reset = notify.parse_reset('resets Oct 6, 3am (Europe/Lisbon)', datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(reset, datetime(2026, 10, 6, 2, 0, tzinfo=UTC))

    def test_a_dated_reset_without_year_that_has_passed_is_next_year(self):
        at = datetime(2026, 12, 30, 10, 0, tzinfo=UTC)
        self.assertEqual(notify.parse_reset('resets Jan 2, 3am (UTC)', at), datetime(2027, 1, 2, 3, 0, tzinfo=UTC))

    def test_an_explicit_year_is_used(self):
        at = datetime(2026, 12, 30, 10, 0, tzinfo=UTC)
        self.assertEqual(notify.parse_reset('resets Jan 2, 2027, 3am (UTC)', at), datetime(2027, 1, 2, 3, 0, tzinfo=UTC))

    def test_an_unknown_zone_is_none(self):
        self.assertIsNone(notify.parse_reset('resets 2pm (Mars/Olympus)', AT))

    def test_text_without_a_reset_is_none(self):
        self.assertIsNone(notify.parse_reset('API Error: overloaded', AT))


def error_line(text, age=timedelta(0)):
    stamp = (datetime.now(timezone.utc) - age).isoformat()
    return json.dumps(
        {'isApiErrorMessage': True, 'message': {'content': [{'type': 'text', 'text': text}]}, 'timestamp': stamp},
        separators=(',', ':'),
    )


class LastLimitErrorTest(WithLogDir):
    def write(self, *lines):
        path = Path(self.dir, 'transcript.jsonl')
        path.write_text('\n'.join(lines) + '\n')
        return str(path)

    def test_returns_a_fresh_limit_error(self):
        path = self.write(error_line("You've hit your session limit · resets 2:20pm (UTC)"))
        text, _ = notify.last_limit_error(path)
        self.assertIn('resets 2:20pm (UTC)', text)

    def test_the_newest_error_wins(self):
        path = self.write(error_line('limit · resets 1pm (UTC)'), error_line('limit · resets 5pm (UTC)'))
        text, _ = notify.last_limit_error(path)
        self.assertIn('5pm', text)

    def test_ignores_an_error_older_than_five_minutes(self):
        path = self.write(error_line('limit · resets 2pm (UTC)', age=timedelta(minutes=6)))
        self.assertIsNone(notify.last_limit_error(path))

    def test_ignores_errors_without_a_reset_time(self):
        path = self.write('{"type":"user"}', error_line('API Error: overloaded'))
        self.assertIsNone(notify.last_limit_error(path))

    def test_a_missing_transcript_is_none(self):
        self.assertIsNone(notify.last_limit_error(os.path.join(self.dir, 'nope.jsonl')))
        self.assertIsNone(notify.last_limit_error(None))


class SettingsTest(WithLogDir):
    def test_values_are_stripped_and_absent_ones_are_empty(self):
        env = {'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': ' topic ', 'CLAUDE_PLUGIN_OPTION_NTFY_TOKEN': 'tk'}
        with mock.patch.dict(os.environ, env):
            os.environ.pop('CLAUDE_PLUGIN_OPTION_NOTIFY_EMAIL', None)
            self.assertEqual(notify.read_settings(), {'ntfy_topic': 'topic', 'notify_email': '', 'ntfy_token': 'tk'})

    def test_watch_reports_missing_options_before_doing_anything_else(self):
        env = {'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': 't', 'CLAUDE_PLUGIN_OPTION_NOTIFY_EMAIL': '', 'CLAUDE_PLUGIN_OPTION_NTFY_TOKEN': 'k'}
        with mock.patch.dict(os.environ, env), mock.patch.object(notify.time, 'sleep') as sleep:
            notify.watch({'transcript_path': None})
        self.assertIn('missing plugin options: notify_email', self.log_text())
        sleep.assert_not_called()


class Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.requests.append((self.path, dict(self.headers), body))
        self.send_response(self.server.status)
        if self.server.truncate:
            self.send_header('Content-Length', '10')
            self.end_headers()
            self.wfile.write(b'abc')
            return
        self.end_headers()

    def log_message(self, *args):
        pass


SETTINGS = {'ntfy_topic': 'secret-topic', 'notify_email': 'me@example.com', 'ntfy_token': 'tk_secret'}


class PushTest(WithLogDir):
    def setUp(self):
        super().setUp()
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Recorder)
        self.server.requests, self.server.status, self.server.truncate = [], 200, False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = mock.patch.object(notify, 'NTFY_URL', f'http://127.0.0.1:{self.server.server_port}/')
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(notify.time, 'sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_posts_the_text_with_the_email_and_token(self):
        notify.push('weekly', SETTINGS)
        [(path, headers, body)] = self.server.requests
        self.assertEqual(path, '/secret-topic')
        self.assertEqual(headers['Authorization'], 'Bearer tk_secret')
        self.assertEqual(headers['Email'], 'me@example.com')
        self.assertEqual(body, b'Your weekly limit has reset. You can continue.')
        self.assertIn('pushed (attempt 1)', self.log_text())

    def test_a_client_error_is_logged_without_the_topic_and_not_retried(self):
        self.server.status = 400
        notify.push('session', SETTINGS)
        self.assertEqual(len(self.server.requests), 1)
        self.sleep.assert_not_called()
        self.assertIn('HTTP 400', self.log_text())
        self.assertNotIn('secret-topic', self.log_text())

    def test_a_server_error_is_retried_three_times_then_given_up(self):
        self.server.status = 503
        notify.push('session', SETTINGS)
        self.assertEqual(len(self.server.requests), 3)
        self.assertIn('gave up pushing', self.log_text())

    def test_too_many_requests_is_retried(self):
        self.server.status = 429
        notify.push('session', SETTINGS)
        self.assertEqual(len(self.server.requests), 3)

    def test_a_response_cut_off_mid_body_is_retried(self):
        self.server.truncate = True
        notify.push('session', SETTINGS)
        self.assertEqual(len(self.server.requests), 3)
        self.assertIn('IncompleteRead', self.log_text())

    def test_a_refused_connection_is_retried(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        with mock.patch.object(notify, 'NTFY_URL', f'http://127.0.0.1:{port}/'):
            notify.push('session', SETTINGS)
        self.assertIn('push failed (attempt 3): URLError', self.log_text())

    def test_the_topic_is_url_quoted(self):
        notify.push('session', {**SETTINGS, 'ntfy_topic': 'my topic/x'})
        self.assertEqual(self.server.requests[0][0], '/my%20topic%2Fx')

    def test_a_malformed_token_is_neither_logged_nor_retried(self):
        notify.push('session', {**SETTINGS, 'ntfy_token': 'abc\ndef'})
        self.assertEqual(self.server.requests, [])
        self.sleep.assert_not_called()
        self.assertIn('ValueError', self.log_text())
        self.assertNotIn('abc', self.log_text())

    def test_watch_pushes_once_and_removes_its_lock(self):
        transcript = Path(self.dir, 'transcript.jsonl')
        transcript.write_text(error_line("You've hit your weekly limit · resets 2pm (UTC)") + '\n')
        env = {f'CLAUDE_PLUGIN_OPTION_{k.upper()}': v for k, v in SETTINGS.items()}
        with mock.patch.dict(os.environ, env), mock.patch.object(notify, 'sleep_until'):
            notify.watch({'transcript_path': str(transcript)})
        [(path, _, body)] = self.server.requests
        self.assertEqual(path, '/secret-topic')
        self.assertIn(b'weekly', body)
        self.assertEqual([name for name in os.listdir(self.dir) if name.endswith('.lock')], [])


class LogTest(WithLogDir):
    def test_the_log_is_readable_by_its_owner_only(self):
        self.addCleanup(os.umask, os.umask(0o022))
        notify.log('x')
        self.assertEqual(os.stat(os.path.join(self.dir, 'log.txt')).st_mode & 0o777, 0o600)


class NtfyUrlTest(unittest.TestCase):
    def test_a_base_url_without_a_trailing_slash_is_accepted(self):
        self.addCleanup(importlib.reload, notify)
        with mock.patch.dict(os.environ, {'LIMIT_WATCH_NTFY_URL': 'http://localhost:8080'}):
            importlib.reload(notify)
            self.assertEqual(notify.NTFY_URL, 'http://localhost:8080/')


class ClaimTest(WithLogDir):
    def setUp(self):
        super().setUp()
        self.marker = os.path.join(self.dir, '1900000000.lock')

    def test_a_held_marker_cannot_be_claimed_again_until_released(self):
        fd = notify.claim(self.marker)
        self.assertIsNotNone(fd)
        self.assertIsNone(notify.claim(self.marker))
        notify.release(fd, self.marker)
        self.assertFalse(os.path.exists(self.marker))
        notify.release(notify.claim(self.marker), self.marker)

    def test_a_marker_left_by_a_dead_watcher_is_taken_over(self):
        Path(self.marker).write_text('999999')
        fd = notify.claim(self.marker)
        self.assertIsNotNone(fd)
        notify.release(fd, self.marker)

    def test_a_marker_replaced_between_open_and_lock_is_not_claimed_by_its_old_inode(self):
        real_flock, calls = fcntl.flock, []

        def flock_after_a_release(fd, operation):
            if not calls:
                os.unlink(self.marker)
                Path(self.marker).touch()
            calls.append(operation)
            return real_flock(fd, operation)

        with mock.patch.object(fcntl, 'flock', flock_after_a_release):
            fd = notify.claim(self.marker)
        self.assertEqual(os.fstat(fd).st_ino, os.stat(self.marker).st_ino)
        notify.release(fd, self.marker)

    def test_only_one_of_many_simultaneous_claims_over_a_stale_marker_wins(self):
        Path(self.marker).write_text('999999')
        barrier = threading.Barrier(8)
        won = []

        def contend():
            barrier.wait()
            won.append(notify.claim(self.marker))

        threads = [threading.Thread(target=contend) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [fd for fd in won if fd is not None]
        self.assertEqual(len(winners), 1)
        notify.release(winners[0], self.marker)


if __name__ == '__main__':
    unittest.main()
