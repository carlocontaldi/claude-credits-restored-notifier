import json
import os
import sys
import tempfile
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


if __name__ == '__main__':
    unittest.main()
