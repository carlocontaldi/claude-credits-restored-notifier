import hashlib
import http.server
import importlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS))
import limit_restore_notify as notify  # noqa: E402
from limitwatch import Cap  # noqa: E402

UTC = timezone.utc


def at(**offset):
    return datetime.now(UTC).replace(microsecond=0) + timedelta(**offset)


def error_row(text="You've hit your 5-hour limit · resets 2pm (UTC)", **info):
    """A transcript row like the real ones: quotaLimits carries the structured limit when `info` is given."""
    row = {'isApiErrorMessage': True, 'message': {'content': [{'type': 'text', 'text': text}]}}
    row['timestamp'] = datetime.now(UTC).isoformat()
    if info:
        row['quotaLimits'] = {'status': 'rejected', **info}
    return json.dumps(row, separators=(',', ':'))


class WithLogDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(notify, 'DIR', self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def log_text(self):
        return Path(self.dir, 'log.txt').read_text()


class SettingsTest(WithLogDir):
    def test_values_are_stripped_and_absent_ones_are_empty(self):
        env = {'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': ' topic ', 'CLAUDE_PLUGIN_OPTION_NTFY_TOKEN': 'tk'}
        with mock.patch.dict(os.environ, env):
            os.environ.pop('CLAUDE_PLUGIN_OPTION_NOTIFY_EMAIL', None)
            self.assertEqual(notify.read_settings(), {'ntfy_topic': 'topic', 'notify_email': '', 'ntfy_token': 'tk'})

    def test_an_unset_or_empty_topic_option_gets_a_private_topic_that_is_kept(self):
        for value in (None, '  '):
            with mock.patch.dict(os.environ):
                os.environ.pop('CLAUDE_PLUGIN_OPTION_NTFY_TOPIC', None)
                if value is not None:
                    os.environ['CLAUDE_PLUGIN_OPTION_NTFY_TOPIC'] = value
                topic = notify.read_settings()['ntfy_topic']
            self.assertRegex(topic, r'^claude-portavoce-[A-Za-z0-9]{22}$')
            self.assertEqual((self.dir / 'topic').read_text().strip(), topic)

    def test_a_topic_set_in_the_options_wins_and_makes_no_private_one(self):
        with mock.patch.dict(os.environ, {'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': 'mine'}):
            self.assertEqual(notify.read_settings()['ntfy_topic'], 'mine')
        self.assertFalse((self.dir / 'topic').exists())

    def test_watch_reports_missing_options_before_doing_anything_else(self):
        env = {
            'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': 't',
            'CLAUDE_PLUGIN_OPTION_NOTIFY_EMAIL': '',
            'CLAUDE_PLUGIN_OPTION_NTFY_TOKEN': 'k',
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(notify, 'sleep_until') as sleep:
            notify.watch({'transcript_path': None})
        self.assertIn('missing plugin options: notify_email', self.log_text())
        sleep.assert_not_called()


class ResetGroupsTest(unittest.TestCase):
    def test_caps_resetting_within_two_minutes_are_one_group(self):
        base = at(hours=2)
        five, monthly = Cap('five_hour', base), Cap('monthly', base + timedelta(seconds=90))
        self.assertEqual(notify.reset_groups([monthly, five]), [[five, monthly]])

    def test_caps_further_apart_are_separate_groups_in_time_order(self):
        base = at(hours=2)
        five, monthly = Cap('five_hour', base), Cap('monthly', base + timedelta(days=20))
        self.assertEqual(notify.reset_groups([monthly, five]), [[five], [monthly]])


class AnnouncementTest(unittest.TestCase):
    five, weekly, monthly = (
        Cap(kind, datetime(2026, 12, 1, tzinfo=UTC)) for kind in ('five_hour', 'weekly', 'monthly')
    )

    def test_one_cap_restored_and_nothing_else_in_force(self):
        self.assertEqual(notify.announcement([self.five], []), 'Your 5-hour limit has reset. You can continue.')

    def test_two_caps_restored_together(self):
        self.assertEqual(
            notify.announcement([self.five, self.monthly], []),
            'Your 5-hour limit and monthly spend limit have both reset. You can continue.',
        )

    def test_three_caps_restored_together(self):
        self.assertEqual(
            notify.announcement([self.five, self.weekly, self.monthly], []),
            'Your 5-hour limit, weekly limit and monthly spend limit have all reset. You can continue.',
        )

    def test_the_plan_is_back_while_the_spend_cap_still_limits_the_credits(self):
        self.assertEqual(
            notify.announcement([self.five], [self.monthly]),
            'Your 5-hour limit has reset. You can continue. '
            'Usage credits stay capped by your monthly spend limit until Dec 1, 00:00 UTC.',
        )

    def test_a_window_still_in_force_is_named_with_its_reset_and_nothing_says_continue(self):
        self.assertEqual(
            notify.announcement([self.five], [self.weekly]),
            'Your 5-hour limit has reset. Still in force: weekly limit (until Dec 1, 00:00 UTC).',
        )

    def test_a_window_and_a_spend_cap_both_still_in_force(self):
        self.assertEqual(
            notify.announcement([self.five], [self.weekly, self.monthly]),
            'Your 5-hour limit has reset. Still in force: weekly limit (until Dec 1, 00:00 UTC). '
            'Usage credits stay capped by your monthly spend limit until Dec 1, 00:00 UTC.',
        )

    def test_the_spend_cap_resetting_alone_says_you_can_continue(self):
        self.assertEqual(
            notify.announcement([self.monthly], []), 'Your monthly spend limit has reset. You can continue.'
        )


class Recorder(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.requests.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


SETTINGS = {'ntfy_topic': 'secret-topic', 'notify_email': 'me@example.com', 'ntfy_token': 'tk_secret'}
ENV = {f'CLAUDE_PLUGIN_OPTION_{k.upper()}': v for k, v in SETTINGS.items()}


class WatchTest(WithLogDir):
    def setUp(self):
        super().setUp()
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Recorder)
        self.server.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for target, value in (('NTFY_URL', f'http://127.0.0.1:{self.server.server_port}/'),):
            patcher = mock.patch.object(notify, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(notify, 'sleep_until')
        self.sleep_until = sleeper.start()
        self.addCleanup(sleeper.stop)

    def watch(self, *rows):
        transcript = self.dir / 'transcript.jsonl'
        transcript.write_text('\n'.join(rows) + '\n')
        with mock.patch.dict(os.environ, ENV):
            notify.watch({'transcript_path': str(transcript)})

    def bodies(self):
        return [body.decode() for _, _, body in self.server.requests]

    def test_a_five_hour_limit_is_pushed_once_with_its_name_and_its_lock_removed(self):
        reset = at(hours=2)
        self.watch(error_row(rateLimitType='five_hour', resetsAt=int(reset.timestamp())))
        [(path, headers, _)] = self.server.requests
        self.assertEqual(path, '/secret-topic')
        self.assertEqual((headers['Title'], headers['Tags']), ('Claude limit restored', 'white_check_mark'))
        self.assertEqual(self.bodies(), ['Your 5-hour limit has reset. You can continue.'])
        self.sleep_until.assert_called_once_with(reset + notify.MARGIN)
        self.assertEqual(list(self.dir.glob('*.lock')), [])

    def test_a_weekly_limit_without_structured_info_is_read_from_its_text(self):
        self.watch(error_row("You've hit your weekly limit · resets 2pm (UTC)"))
        self.assertEqual(self.bodies(), ['Your weekly limit has reset. You can continue.'])

    def test_the_spend_cap_that_resets_later_gets_its_own_push(self):
        five, monthly = at(hours=2), at(days=20)
        info = {
            'rateLimitType': 'five_hour',
            'resetsAt': int(five.timestamp()),
            'overageDisabledReason': 'org_spend_cap_reached',
            'overageResetsAt': int(monthly.timestamp()),
        }
        self.watch(error_row(**info))
        first, second = self.bodies()
        self.assertTrue(
            first.startswith(
                'Your 5-hour limit has reset. You can continue. '
                'Usage credits stay capped by your monthly spend limit until '
            )
        )
        self.assertEqual(second, 'Your monthly spend limit has reset. You can continue.')
        self.assertEqual(
            [call.args[0] for call in self.sleep_until.call_args_list], [five + notify.MARGIN, monthly + notify.MARGIN]
        )

    def test_a_spend_cap_a_few_days_off_is_named_weekly(self):
        info = {
            'rateLimitType': 'five_hour',
            'resetsAt': int(at(hours=2).timestamp()),
            'overageDisabledReason': 'org_spend_cap_reached',
            'overageResetsAt': int(at(days=4).timestamp()),
        }
        self.watch(error_row(**info))
        first, second = self.bodies()
        self.assertIn('Usage credits stay capped by your weekly spend limit until', first)
        self.assertEqual(second, 'Your weekly spend limit has reset. You can continue.')

    def test_caps_that_reset_together_are_one_push_saying_both(self):
        reset = int(at(hours=2).timestamp())
        info = {
            'rateLimitType': 'five_hour',
            'resetsAt': reset,
            'overageDisabledReason': 'org_spend_cap_reached',
            'overageResetsAt': reset + 30,
        }
        self.watch(error_row(**info))
        self.assertEqual(
            self.bodies(), ['Your 5-hour limit and individual spend limit have both reset. You can continue.']
        )

    def test_a_spend_cap_too_far_off_to_wait_for_is_named_but_not_watched(self):
        info = {
            'rateLimitType': 'five_hour',
            'resetsAt': int(at(hours=2).timestamp()),
            'overageDisabledReason': 'org_spend_cap_reached',
            'overageResetsAt': int(at(days=60).timestamp()),
        }
        self.watch(error_row(**info))
        [body] = self.bodies()
        self.assertIn('Usage credits stay capped by your monthly spend limit until', body)

    def test_a_reset_too_far_off_is_logged_and_nothing_is_sent(self):
        self.watch(error_row(rateLimitType='five_hour', resetsAt=int(at(days=9).timestamp())))
        self.assertEqual(self.server.requests, [])
        self.assertIn('unusable reset time', self.log_text())

    def test_a_group_another_watcher_holds_is_left_to_it(self):
        reset = at(hours=2)
        marker = self.dir / f'{int(reset.timestamp())}.lock'
        fd = notify.claim(marker)
        self.addCleanup(notify.release, fd, marker)
        self.watch(error_row(rateLimitType='five_hour', resetsAt=int(reset.timestamp())))
        self.assertEqual(self.server.requests, [])
        self.assertIn('already watching', self.log_text())

    def test_a_transcript_without_a_limit_is_logged(self):
        with mock.patch.object(notify, 'LOOKUP_TRIES', 1):
            self.watch('{"type":"user"}')
        self.assertIn('no limit message in transcript', self.log_text())


class TopicCommandTest(WithLogDir):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('CLAUDE_PLUGIN_OPTION_NTFY_TOPIC', None)

    def test_the_topic_is_shown_with_how_to_subscribe_and_stays_the_same(self):
        text = notify.topic_text()
        topic = (self.dir / 'topic').read_text().strip()
        self.assertIn(f'Portavoce pushes to your private ntfy topic {topic}.', text)
        self.assertIn('tap +', text)
        self.assertIn(f'ntfy://ntfy.sh/{topic}', text)
        self.assertTrue(text.endswith('Then run /portavoce test.'))
        self.assertEqual(notify.topic_text(), text)

    def test_topic_new_makes_another_and_says_to_drop_the_old_one(self):
        old = notify.topic_text()
        new = notify.topic_text(rotate=True)
        self.assertNotEqual(old, new)
        self.assertTrue(new.startswith('Made a new private topic, so drop the old one in the app.'))

    def test_a_topic_from_the_options_is_shown_as_such_and_not_changed(self):
        os.environ['CLAUDE_PLUGIN_OPTION_NTFY_TOPIC'] = 'mine'
        text = notify.topic_text(rotate=True)
        self.assertIn('Portavoce pushes to mine. The topic option is set', text)
        self.assertFalse((self.dir / 'topic').exists())

    def test_main_prints_the_topic_text(self):
        out = io.StringIO()
        with mock.patch.object(sys, 'argv', ['x', 'topic']), mock.patch.object(sys, 'stdout', out):
            notify.main()
        self.assertIn('private ntfy topic', out.getvalue())


class PushCommandTest(WithLogDir):
    def setUp(self):
        super().setUp()
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Recorder)
        self.server.requests = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = mock.patch.object(notify, 'NTFY_URL', f'http://127.0.0.1:{self.server.server_port}/')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_push_goes_to_the_topic_without_the_email(self):
        text = notify.send_test({**SETTINGS})
        self.assertEqual(text, 'Sent: it should show up in the ntfy app now.')
        [(path, headers, body)] = self.server.requests
        self.assertEqual(path, '/secret-topic')
        self.assertNotIn('Email', headers)
        self.assertEqual(headers['Title'], 'Claude Portavoce test')
        self.assertIn(b'the topic works', body)

    def test_a_failure_points_at_the_log(self):
        self.server.shutdown()
        self.server.server_close()
        with mock.patch('time.sleep'):
            text = notify.send_test({**SETTINGS})
        self.assertTrue(text.startswith('Not sent;'))
        self.assertIn('push failed', self.log_text())


class LogTest(WithLogDir):
    def test_the_log_is_readable_by_its_owner_only(self):
        self.addCleanup(os.umask, os.umask(0o022))
        notify.log('x')
        self.assertEqual(os.stat(self.dir / 'log.txt').st_mode & 0o777, 0o600)


class NtfyUrlTest(unittest.TestCase):
    def test_a_base_url_without_a_trailing_slash_is_accepted(self):
        self.addCleanup(importlib.reload, notify)
        with mock.patch.dict(os.environ, {'PORTAVOCE_NTFY_URL': 'http://localhost:8080'}):
            importlib.reload(notify)
            self.assertEqual(notify.NTFY_URL, 'http://localhost:8080/')


class VendoredTest(unittest.TestCase):
    def test_the_copy_of_limitwatch_matches_the_hash_in_its_header(self):
        _, header, body = (HOOKS / 'limitwatch.py').read_text().split('\n', 2)
        stated = re.search(r'sha256: ([0-9a-f]{64})', header).group(1)
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), stated)


if __name__ == '__main__':
    unittest.main()
