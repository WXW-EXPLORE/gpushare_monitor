import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from gpushare_monitor import (ProbeError, cycle, evaluate, load_state, save_state,
                             validate, env_headers, inspect_json, NoRedirect,
                             describe_json_shape, validate_market_diagnostic,
                             evaluate_market, fetch_market_pages, market_page_url,
                             NotListed)


BASE = {
    'target': {'machine_id': 'host-2', 'gpu_model': 'RTX 4090', 'gpu_count': 2, 'console_url': 'https://gpushare.com/'},
    'poll_seconds': 60,
    'source': {'confirmed_read_only': True, 'url': 'https://api.gpushare.com/app/api/market/matching/machine',
               'records_path': 'data.machines', 'id_path': 'id', 'model_path': 'model', 'free_path': 'free'}
}


def result(free):
    return {'data': {'machines': [
        {'id': 'host-1', 'model': 'RTX 4090', 'free': 200},
        {'id': 'host-2', 'model': 'RTX 4090', 'free': free}]}}


class MonitorTests(unittest.TestCase):
    def test_validates_source_and_machine(self):
        cfg = copy.deepcopy(BASE)
        validate(cfg)
        cfg['source']['url'] = 'https://api.gpushare.com/app/api/instance/start'
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg['source']['url'] = 'http://evil.example/data'
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg['source']['confirmed_read_only'] = False
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_exact_id_and_count(self):
        self.assertEqual(evaluate(result(1), BASE), (False, 1))
        self.assertEqual(evaluate(result(2), BASE), (True, 2))

    def test_missing_machine_is_unknown(self):
        data = {'data': {'machines': [{'id': 'another', 'free': 1}]}}
        with self.assertRaises(ProbeError):
            evaluate(data, BASE)

    def test_invalid_free_not_available(self):
        for bad in (True, '2', -1, None):
            with self.subTest(bad=bad), self.assertRaises(ProbeError):
                evaluate(result(bad), BASE)

    def test_missing_path_unknown(self):
        with self.assertRaises(ProbeError):
            evaluate({'data': {}}, BASE)

    def test_model_mismatch_unknown(self):
        data = result(3)
        data['data']['machines'][1]['model'] = 'RTX 3090'
        with self.assertRaises(ProbeError):
            evaluate(data, BASE)

    def test_dedup_rearm_and_unknown(self):
        messages = []
        probe = lambda src: result(2)
        notify = lambda title, body: messages.append(title)
        s, msg = cycle(BASE, {}, probe=probe, notify=notify)
        self.assertEqual(len(messages), 1)
        s, msg = cycle(BASE, s, probe=probe, notify=notify)
        self.assertEqual(len(messages), 1)
        s, msg = cycle(BASE, s, probe=lambda src: result(0), notify=notify)
        self.assertTrue(s['alerted'])
        s, msg = cycle(BASE, s, probe=lambda src: {'oops': 1}, notify=notify)
        self.assertTrue(s['alerted'])
        s, msg = cycle(BASE, s, probe=lambda src: result(0), notify=notify)
        s, msg = cycle(BASE, s, probe=lambda src: result(0), notify=notify)
        self.assertFalse(s['alerted'])
        s, msg = cycle(BASE, s, probe=probe, notify=notify)
        self.assertEqual(len(messages), 2)

    def test_mail_failure_does_not_suppress(self):
        with self.assertRaises(OSError):
            cycle(BASE, {}, probe=lambda src: result(2), notify=lambda *a: (_ for _ in ()).throw(OSError('mail')))
        messages = []
        s, _ = cycle(BASE, {}, probe=lambda src: result(2), notify=lambda *a: messages.append('ok'))
        self.assertEqual(messages, ['ok'])

    def test_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'nested'/'state.json'
            save_state(p, {'a': True})
            self.assertEqual(load_state(p), {'a': True})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_screenshot_instance_target(self):
        cfg_path = Path(__file__).resolve().parent.parent/'config.my-instance.example.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        self.assertEqual(cfg['target']['gpu_model'], '4080s-32G')
        self.assertEqual(cfg['target']['gpu_count'], 1)
        self.assertEqual(cfg['target']['match_mode'], 'gpu_model')
        self.assertNotIn('machine_id', cfg['target'])
        self.assertNotIn('region', cfg['target'])
        self.assertTrue(cfg['source']['confirmed_read_only'])
        self.assertEqual(cfg['source']['kind'], 'gpushare_market')
        validate(cfg)

    def test_offline_inspect_json_no_secrets(self):
        data = {'data': {'machines': [
            {'machineId': 'MACHL7cYdJ32V4WRGwRGsbSN', 'idleGpuCount': 1,
             'token': 'secret-never-print'}, {'machineId': 'another', 'idleGpuCount': 5}]}}
        msg = inspect_json(data, 'MACHL7cYdJ32V4WRGwRGsbSN')
        self.assertIn('idleGpuCount', msg)
        self.assertIn('machineId', msg)
        self.assertNotIn('secret-never-print', msg)
        self.assertIn('没有找到', inspect_json(data, 'not-found'))

    def test_no_cross_site_redirects(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://evil.example'))

    def test_inspect_response_shape_never_prints_secret_values(self):
        payload = {'code': 200, 'data': {'list': [{'machineId': 'host-2',
                                                   'accessToken': 'super-secret-value',
                                                   'idleGpuNum': 1}], 'total': 1}}
        shape = describe_json_shape(payload)
        self.assertIn('data.list', shape)
        self.assertIn('accessToken', shape)
        self.assertNotIn('super-secret-value', shape)
        self.assertNotIn('host-2', shape)

    def test_diagnostic_only_allows_market_get(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent /
                          'config.my-instance.example.json').read_text(encoding='utf-8'))
        validate_market_diagnostic(cfg['source'])
        for mutation in [
                {'method': 'POST'},
                {'url': 'https://api.gpushare.com/app/api/instance/start'},
                {'url': 'https://api.evil.com/app/api/market/machine/list'},
                {'url': 'http://api.gpushare.com/app/api/market/machine/list'}]:
            bad = {**cfg['source'], **mutation}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_market_diagnostic(bad)

    def test_screenshot_market_is_configured_to_read_only_get(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent /
                          'config.my-instance.example.json').read_text(encoding='utf-8'))
        self.assertTrue(cfg['source']['confirmed_read_only'])
        self.assertIn('gpuName=4080s-32G', cfg['source']['url'])
        self.assertIn('ps=10', cfg['source']['url'])
        self.assertEqual(cfg['source']['method'], 'GET')
        validate(cfg)

    def test_header_refs_only(self):
        with self.assertRaises(ProbeError):
            env_headers({'headers': {'Cookie': 'session=secret'}})
        with patch.dict(os.environ, {'GPUSHARE_READ_COOKIE': 'session=secret'}):
            headers = env_headers({'headers': {'Cookie': 'env:GPUSHARE_READ_COOKIE'}})
            self.assertEqual(headers['Cookie'], 'session=secret')


MARKET_CONFIG = {
    'target': {'machine_id': 'MACH-EXAMPLE-1', 'gpu_model': 'TITAN X-12G',
               'gpu_count': 1, 'region': '华东', 'console_url': 'https://gpushare.com/'},
    'poll_seconds': 60,
    'source': {'kind': 'gpushare_market', 'confirmed_read_only': True,
               'url': 'https://api.gpushare.com/app/api/market/machine/list?gpuName=TITAN+X-12G&vmType=docker&pn=1&ps=10&minGpuNum=1',
               'method': 'GET', 'headers': {}, 'max_pages': 30}
}


def market_record(*, used=6, num=7, machine_id='MACH-EXAMPLE-1',
                  sku_status=1, sku_enable=1, online=True):
    # Schema based on user's real TITAN X example; machine ID is synthetic.
    return {'machineId': machine_id, 'gpuName': 'TITAN X-12G',
            'gpuNum': num, 'gpuUsed': used, 'status': 1, 'isOnline': online,
            'regionName': '华东', 'skuList': [
                {'skuName': 'payg', 'status': sku_status, 'enable': sku_enable}]}


def market_response(items, total=None):
    return {'status': 0, 'msg': '请求成功',
            'data': {'list': items, 'total': len(items) if total is None else total}}


class GpuShareMarketTests(unittest.TestCase):
    def test_actual_fields_seven_minus_six_equals_one(self):
        self.assertEqual(evaluate_market(market_response([market_record()]),
                                         MARKET_CONFIG['target']), (True, 1))
        cfg = copy.deepcopy(MARKET_CONFIG)
        cfg['target']['gpu_count'] = 2
        self.assertEqual(evaluate_market(market_response([market_record()]),
                                         cfg['target']), (False, 0))

    def test_zero_free_and_bad_counts(self):
        self.assertEqual(evaluate_market(market_response([market_record(used=7)]),
                                         MARKET_CONFIG['target']), (False, 0))
        for bad in (None, '6', True, -1, 8):
            with self.subTest(bad=bad), self.assertRaises(ProbeError):
                evaluate_market(market_response([market_record(used=bad)]),
                                MARKET_CONFIG['target'])

    def test_machine_not_listed_is_not_assumed_zero(self):
        data = market_response([market_record(machine_id='different')])
        with self.assertRaises(NotListed):
            evaluate_market(data, MARKET_CONFIG['target'])

    def test_block_inactive_sku_or_offline_machine(self):
        for record in [market_record(sku_enable=0), market_record(sku_status=0),
                       market_record(online=False)]:
            self.assertEqual(evaluate_market(market_response([record]),
                                             MARKET_CONFIG['target']), (False, 0))

    def test_missing_eligibility_and_mismatch_unknown(self):
        for key, value in [('skuList', None), ('gpuName', 'RTX-OTHER'),
                           ('regionName', '西北'), ('status', None),
                           ('gpuNum', 0)]:
            row = market_record()
            row[key] = value
            with self.subTest(key=key), self.assertRaises(ProbeError):
                evaluate_market(market_response([row]), MARKET_CONFIG['target'])

    def test_business_failure_is_not_available(self):
        data = market_response([market_record()])
        data['status'] = 401
        with self.assertRaises(ProbeError):
            evaluate_market(data, MARKET_CONFIG['target'])

    def test_multiple_page_scan_locates_later_target(self):
        calls = []
        source = MARKET_CONFIG['source'].copy()
        source['url'] = source['url'].replace('ps=10', 'ps=2')
        rows = [market_record(machine_id='other-a'),
                market_record(machine_id='other-b'), market_record()]
        def get_page(src):
            calls.append(src['url'])
            page = int(src['url'].split('pn=')[1].split('&')[0])
            return market_response(rows[(page-1)*2:page*2], total=3)
        response = fetch_market_pages(source, get_page=get_page)
        self.assertEqual(len(calls), 2)
        self.assertIn('pn=2', calls[1])
        self.assertEqual(evaluate_market(response, MARKET_CONFIG['target']), (True, 1))

    def test_incomplete_or_changed_pages_are_unknown(self):
        source = MARKET_CONFIG['source'].copy()
        source['url'] = source['url'].replace('ps=10', 'ps=2')
        def truncated(src):
            page = int(src['url'].split('pn=')[1].split('&')[0])
            return market_response([market_record()], total=3 if page == 1 else 2)
        with self.assertRaises(ProbeError):
            fetch_market_pages(source, get_page=truncated)
        with self.assertRaises(ProbeError):
            fetch_market_pages({**source, 'max_pages': 1},
                               get_page=lambda src: market_response([], total=3))

    def test_absence_resets_alert_only_after_two_full_scans(self):
        mails = []
        mailer = lambda *args: mails.append(args)
        available = market_response([market_record()])
        missing = market_response([])
        state, _ = cycle(MARKET_CONFIG, {}, probe=lambda src: available, notify=mailer)
        self.assertEqual(len(mails), 1)
        state, _ = cycle(MARKET_CONFIG, state, probe=lambda src: missing, notify=mailer)
        self.assertTrue(state['alerted'])
        self.assertEqual(state['last_status'], 'not_listed')
        state, _ = cycle(MARKET_CONFIG, state, probe=lambda src: missing, notify=mailer)
        self.assertFalse(state['alerted'])
        state, _ = cycle(MARKET_CONFIG, state, probe=lambda src: available, notify=mailer)
        self.assertEqual(len(mails), 2)

    def test_wrong_endpoint_and_model_rejected(self):
        cfg = copy.deepcopy(MARKET_CONFIG)
        cfg['source']['url'] = 'https://api.gpushare.com/app/api/instance/start'
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg = copy.deepcopy(MARKET_CONFIG)
        cfg['source']['url'] = cfg['source']['url'].replace('TITAN+X-12G', 'RTX+4090')
        with self.assertRaises(ValueError):
            validate(cfg)


MODEL_CONFIG = copy.deepcopy(MARKET_CONFIG)
MODEL_CONFIG['target'] = {'match_mode': 'gpu_model', 'gpu_model': 'TITAN X-12G',
                          'gpu_count': 1, 'console_url': 'https://gpushare.com/'}


class GpuModelMatchingTests(unittest.TestCase):
    def test_any_host_with_right_model_counts_even_in_other_region(self):
        row_a = market_record(machine_id='old-machine', used=7)
        row_b = market_record(machine_id='another-machine', used=6)
        row_b['regionName'] = '西北'
        self.assertEqual(evaluate_market(market_response([row_a, row_b]),
                                         MODEL_CONFIG['target']), (True, 1))
        self.assertNotIn('machine_id', MODEL_CONFIG['target'])
        validate(MODEL_CONFIG)

    def test_available_on_multiple_hosts_sum_idle(self):
        rows = [market_record(machine_id='A', num=4, used=3),
                market_record(machine_id='B', num=8, used=6)]
        self.assertEqual(evaluate_market(market_response(rows), MODEL_CONFIG['target']), (True, 3))

    def test_cannot_combine_separate_hosts_for_two_gpu_request(self):
        cfg = copy.deepcopy(MODEL_CONFIG)
        cfg['target']['gpu_count'] = 2
        rows = [market_record(machine_id='A', num=4, used=3),
                market_record(machine_id='B', num=8, used=7)]
        self.assertEqual(evaluate_market(market_response(rows), cfg['target']), (False, 0))
        rows[1]['gpuUsed'] = 6
        self.assertEqual(evaluate_market(market_response(rows), cfg['target']), (True, 2))

    def test_other_gpu_types_do_not_trigger_alert(self):
        bad = market_record(machine_id='A', num=8, used=2)
        bad['gpuName'] = '4080s-32G'
        with self.assertRaises(NotListed):
            evaluate_market(market_response([bad]), MODEL_CONFIG['target'])

    def test_known_offline_hosts_are_not_available(self):
        rows = [market_record(machine_id='A', online=False),
                market_record(machine_id='B', sku_enable=0)]
        self.assertEqual(evaluate_market(market_response(rows), MODEL_CONFIG['target']), (False, 0))

    def test_bad_row_does_not_mask_another_confirmed_available_host(self):
        good = market_record(machine_id='B')
        bad = market_record(machine_id='A')
        bad.pop('gpuNum')
        self.assertEqual(evaluate_market(market_response([bad, good]),
                                         MODEL_CONFIG['target']), (True, 1))
        with self.assertRaises(ProbeError):
            evaluate_market(market_response([bad]), MODEL_CONFIG['target'])

    def test_duplicate_host_ids_fail_closed(self):
        with self.assertRaises(ProbeError):
            evaluate_market(market_response([market_record(), market_record()]),
                            MODEL_CONFIG['target'])

    def test_no_machine_based_alert_suppression_and_email_mentions_model(self):
        sent = []
        row_a = market_record(machine_id='not-the-old-machine')
        state, msg = cycle(MODEL_CONFIG, {},
                           probe=lambda source: market_response([row_a]),
                           notify=lambda subject, content: sent.append((subject, content)))
        self.assertIn('AVAILABLE', msg)
        self.assertEqual(len(sent), 1)
        self.assertIn('TITAN X-12G', sent[0][1])
        self.assertIn('not-the-old-machine', sent[0][1])
        self.assertNotIn('目标机器：', sent[0][1])
        state, msg = cycle(MODEL_CONFIG, state,
                           probe=lambda source: market_response([row_a]),
                           notify=lambda *args: sent.append(args))
        self.assertEqual(len(sent), 1)
        for _ in range(2):
            state, _ = cycle(MODEL_CONFIG, state,
                             probe=lambda source: market_response([market_record(used=7)]),
                             notify=lambda *args: sent.append(args))
        state, _ = cycle(MODEL_CONFIG, state,
                         probe=lambda source: market_response([row_a]),
                         notify=lambda *args: sent.append(args))
        self.assertEqual(len(sent), 2)

    def test_target_mode_cannot_mix_with_unknown_generic_source(self):
        cfg = copy.deepcopy(BASE)
        cfg['target']['match_mode'] = 'gpu_model'
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_market_pages_can_find_another_host(self):
        src = copy.deepcopy(MODEL_CONFIG['source'])
        src['url'] = src['url'].replace('ps=10', 'ps=2')
        rows = [market_record(machine_id='old-machine', used=7),
                market_record(machine_id='other-1', used=7),
                market_record(machine_id='other-2', used=6)]
        def fetch(src):
            page = int(src['url'].split('pn=')[1].split('&')[0])
            return market_response(rows[(page-1)*2:page*2], total=3)
        data = fetch_market_pages(src, get_page=fetch)
        self.assertEqual(evaluate_market(data, MODEL_CONFIG['target']), (True, 1))


if __name__ == '__main__':
    unittest.main()