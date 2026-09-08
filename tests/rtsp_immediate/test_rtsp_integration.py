#!/usr/bin/env python3
"""Real libavformat RTSP/TCP muxing against a loopback protocol receiver.

Packets are synthetic packetizer fixtures, not camera media and not decodable
audio/video. Assertions concern queue ownership, timestamp shifts, RTP packet
identity/order and repeated fresh RTSP sessions, not decoder interoperability.
"""
import json
import os
from fractions import Fraction
import pathlib
import socket
import struct
import subprocess
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
DRIVER = pathlib.Path(os.environ.get('RTSP_TEST_DRIVER', str(ROOT/'rtsp_driver')))


class Receiver:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen()
        self.sock.settimeout(8)
        self.port = self.sock.getsockname()[1]
        self.packets = []
        self.errors = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            with self.sock:
                conn, _ = self.sock.accept()
                with conn:
                    conn.settimeout(8)
                    buf = b''
                    while True:
                        part = conn.recv(65536)
                        if not part:
                            break
                        buf += part
                        while buf:
                            if buf[0] == 36:
                                if len(buf) < 4:
                                    break
                                n = int.from_bytes(buf[2:4], 'big')
                                if len(buf) < n + 4:
                                    break
                                channel, packet = buf[1], buf[4:n+4]
                                self.packets.append((channel, packet))
                                buf = buf[n+4:]
                                continue
                            end = buf.find(b'\r\n\r\n')
                            if end < 0:
                                break
                            lines = buf[:end].decode().split('\r\n')
                            headers = dict(line.split(':', 1) for line in lines[1:])
                            headers = {k.lower(): v.strip() for k, v in headers.items()}
                            size = end + 4 + int(headers.get('content-length', '0'))
                            if len(buf) < size:
                                break
                            method = lines[0].split()[0]
                            buf = buf[size:]
                            response = ['RTSP/1.0 200 OK', 'CSeq: ' + headers['cseq'],
                                        'Session: test-immediate']
                            if method == 'OPTIONS':
                                response += ['Public: OPTIONS, ANNOUNCE, SETUP, RECORD, TEARDOWN']
                            if method == 'SETUP':
                                response += ['Transport: ' + headers['transport']]
                            conn.sendall(('\r\n'.join(response) + '\r\n\r\n').encode())
                            if method == 'TEARDOWN':
                                return
        except Exception as error:
            self.errors.append(repr(error))

    def finish(self):
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            self.sock.close()
            raise AssertionError('receiver did not exit')
        if self.errors:
            raise AssertionError(self.errors)


def rescale(value, src, dst):
    exact = Fraction(value) * Fraction(*src) / Fraction(*dst)
    # av_rescale_q uses AV_ROUND_NEAR_INF (ties away from zero).
    absolute = abs(exact)
    rounded = (2 * absolute.numerator + absolute.denominator) // (2 * absolute.denominator)
    return -rounded if exact < 0 else rounded


def run(types, rows, enabled='1', avoid=-1, offset=0, flush_before_trailer=True,
        expect_packet_error=False):
    server = Receiver()
    proc = subprocess.run([str(DRIVER),
        f'rtsp://127.0.0.1:{server.port}/test', enabled, types, str(avoid), str(offset),
        str(int(flush_before_trailer))],
        input=''.join(f'{s} {d} {p} {i}\n' for s, d, p, i in rows),
        text=True, capture_output=True, timeout=15)
    server.finish()
    if proc.returncode != (6 if expect_packet_error else 0):
        raise AssertionError(proc.stderr + proc.stdout)
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    states = [event for event in events if 'step' in event]
    clocks = [event for event in events if event.get('event') == 'clocks']
    trailers = [event for event in events if event.get('event') == 'trailer']
    assert len(clocks) == 1 and len(clocks[0]['streams']) == len(types)
    assert trailers == [{'event': 'trailer', 'ret': 0, 'queued': 0}]
    assert all(state['consumed'] for state in states)
    assert any(state['ret'] < 0 for state in states) == expect_packet_error
    assert all(state['ret'] <= 0 for state in states)
    clocks = clocks[0]['streams']
    assert len({clock['epoch_us'] for clock in clocks}) == 1, 'RTP children must share RTSP header epoch'
    rtp, sender_reports = [], []
    for channel, data in server.packets:
        if channel % 2:
            position = 0
            while position < len(data):
                assert len(data) - position >= 4
                assert data[position] >> 6 == 2
                packet_type = data[position + 1]
                size = (int.from_bytes(data[position+2:position+4], 'big') + 1) * 4
                assert size >= 4 and position + size <= len(data)
                if packet_type == 200:
                    assert size >= 28
                    ssrc, seconds, fraction, stamp = struct.unpack_from('!IIII', data, position + 4)
                    sender_reports.append({'stream': channel//2, 'ssrc': ssrc, 'stamp': stamp,
                                           'ntp_us': seconds * 1000000 + fraction * 1000000 // 2**32})
                position += size
            continue
        assert len(data) >= 13 and data[0] == 0x80, 'fixture RTP has no extensions/CSRCs'
        seq, stamp = struct.unpack_from('!HI', data, 2)
        rtp.append({'stream': channel//2, 'seq': seq, 'stamp': stamp,
                    'ssrc': int.from_bytes(data[8:12], 'big'),
                    'id': data[-1], 'length': len(data)})

    inputs = {identity: (stream, pts) for stream, _, pts, identity in rows if stream >= 0}
    assert len(inputs) == sum(row[0] >= 0 for row in rows), 'fixture identities must be unique'
    # Absolute PTS uses the actual random base reported by the library, not a
    # fitted first-packet base. This catches constant per-track phase shifts
    # that relative timestamp increments alone would miss.
    for packet in rtp:
        stream, pts = inputs[packet['id']]
        assert stream == packet['stream']
        clock = clocks[stream]
        parent_offset, child_offset = states[-1]['offsets'][stream]
        parent_pts = (rescale(pts, (1,1000), clock['parent_tb']) + parent_offset
                      + rescale(offset, (1,1000000), clock['parent_tb']))
        expected = (clock['base'] + rescale(parent_pts, clock['parent_tb'], clock['rtp_tb'])
                    + child_offset) & 0xffffffff
        assert packet['stamp'] == expected, (packet, expected)
    for stream in {packet['stream'] for packet in rtp}:
        packets = [packet for packet in rtp if packet['stream'] == stream]
        reports = [report for report in sender_reports if report['stream'] == stream]
        assert reports, 'every written fixture track must send its initial SR'
        assert reports[0]['stamp'] == packets[0]['stamp'], 'rtcp_from_packet SR must describe its triggering packet'
        assert all(report['ssrc'] == packets[0]['ssrc'] for report in reports)
        assert len({packet['ssrc'] for packet in packets}) == 1
        assert all(report['ntp_us'] >= clocks[stream]['epoch_us'] - 1 for report in reports)
        assert all(b['ntp_us'] >= a['ntp_us'] for a,b in zip(reports,reports[1:]))
    return states, rtp, proc.stderr


class Integration(unittest.TestCase):
    normal = [(1,1000,1000,1),(0,1040,1040,2),(0,1080,1080,3),
              (0,1120,1120,4),(1,1064,1064,5),(0,1160,1160,6)]

    def check_packets(self, rows, rtp):
        self.assertEqual(sorted(x['id'] for x in rtp), sorted(x[3] for x in rows if x[0] >= 0))
        for stream in {x['stream'] for x in rtp}:
            seen = [x for x in rtp if x['stream'] == stream]
            self.assertEqual([x['id'] for x in seen], [x[3] for x in rows if x[0] == stream])
            self.assertTrue(all((b['seq']-a['seq']) & 65535 == 1 for a,b in zip(seen,seen[1:])))

    def test_enabled_drains_once_then_sends_each_call(self):
        s, rtp, log = run('va', self.normal)
        self.assertEqual(s[0]['mode'], 'guard')
        self.assertEqual(s[3]['mode'], 'pass')
        self.assertTrue(all(x['queued'] == 0 for x in s[3:]))
        self.assertTrue(all(x['consumed'] for x in s))
        self.assertTrue(all(x['ret'] == 0 for x in s))
        self.assertEqual(log.count('immediate output enabled'), 1)
        self.check_packets(self.normal, rtp)

    def test_default_and_explicit_off_keep_original_queue(self):
        outputs = []
        for flag in ('default','0'):
            s, rtp, _ = run('va', self.normal, flag)
            self.assertTrue(all(x['mode'] == 'dts' for x in s))
            self.assertGreater(s[4]['queued'], 0)
            self.check_packets(self.normal, rtp)
            outputs.append(s)
        self.assertEqual(outputs[0], outputs[1])

    def test_late_negative_aac_stays_original(self):
        rows = [(0,0,0,1),(0,40,40,2),(1,-64,-64,3),
                (1,0,0,4),(0,80,80,5),(1,64,64,6)]
        s, rtp, log = run('va', rows)
        self.assertEqual(s[-1]['mode'],'dts')
        self.assertEqual(s[-1]['offsets'][1][1],1024)
        self.assertNotIn('immediate output enabled',log)
        self.check_packets(rows, rtp)
        off, other, _ = run('va',rows,'0')
        self.assertEqual(s[-1]['offsets'],off[-1]['offsets'])
        self.assertEqual([x['id'] for x in rtp],[x['id'] for x in other])

    def test_common_negative_shift_preserved(self):
        rows = [(1,-64,-64,1),(0,0,0,2),(0,40,40,3),
                (1,0,0,4),(0,80,80,5),(1,64,64,6)]
        s, rtp, _ = run('va',rows)
        self.assertEqual(s[-1]['mode'],'pass')
        self.assertEqual(s[-1]['offsets'],[[5760,0],[1024,0]])
        self.check_packets(rows,rtp)
        off, other, _ = run('va',rows,'0')
        self.assertEqual(s[-1]['offsets'],off[-1]['offsets'])
        for stream in (0,1):
            def relative(packets):
                packets = [x for x in packets if x['stream']==stream]
                return [(x['id'],(x['stamp']-packets[0]['stamp']) & 0xffffffff) for x in packets]
            self.assertEqual(relative(rtp),relative(other))

    def test_video_first_and_g711_audio(self):
        rows = [(0,1000,1000,1),(1,1024,1024,2),(0,1040,1040,3),
                (0,1080,1080,4),(1,1088,1088,5)]
        for types in ('va','vu'):
            s, rtp, _ = run(types, rows)
            self.assertEqual(s[-1]['mode'],'pass')
            self.check_packets(rows,rtp)

    def test_single_track_remains_original_passthrough(self):
        rows = [(0,0,0,1),(0,64,64,2),(0,128,128,3)]
        for types in ('v','a','u'):
            for flag in ('0','1'):
                s,rtp,_ = run(types,rows,flag)
                self.assertTrue(all(x['mode']=='pass' and x['queued']==0 for x in s))
                self.check_packets(rows,rtp)

    def test_missing_track_and_explicit_flush(self):
        rows = [(0,0,0,1),(0,40,40,2),(-1,0,0,0),(0,80,80,3)]
        s,rtp,log = run('va',rows)
        self.assertTrue(all(x['mode']=='guard' for x in s))
        self.assertEqual(s[3]['queued'],0)
        self.assertNotIn('immediate output enabled',log)
        self.check_packets(rows,rtp)

    def test_stopped_audio_does_not_hold_video_after_transition(self):
        rows = self.normal + [(0,t,t,i) for i,t in enumerate(range(1200,1600,40),7)]
        s,rtp,_ = run('va',rows)
        self.assertTrue(all(x['queued']==0 for x in s[3:]))
        self.check_packets(rows,rtp)

    def test_make_zero_and_output_offset(self):
        for avoid,offset in ((2,0),(-1,64000),(0,0)):
            s,rtp,_ = run('va',self.normal,avoid=avoid,offset=offset)
            off,_,_ = run('va',self.normal,'0',avoid=avoid,offset=offset)
            self.assertEqual(s[-1]['mode'],'pass')
            self.assertEqual(s[-1]['offsets'],off[-1]['offsets'])
            self.check_packets(self.normal,rtp)

    def test_trailer_flushes_missing_track_queue_without_manual_flush(self):
        rows = [(0,0,0,1),(0,40,40,2),(0,80,80,3)]
        for flag in ('0','1'):
            states,rtp,log = run('va', rows, flag, flush_before_trailer=False)
            self.assertGreater(states[-1]['queued'], 0)
            self.assertNotIn('immediate output enabled', log)
            self.check_packets(rows,rtp)

    def test_trailer_flushes_all_tracks_before_guard_is_ready(self):
        rows = [(1,1000,1000,1),(0,1040,1040,2)]
        states,rtp,_ = run('va',rows,flush_before_trailer=False)
        self.assertEqual(states[-1]['mode'],'guard')
        self.assertGreater(states[-1]['queued'],0)
        self.check_packets(rows,rtp)

    def test_pts_reordering_keeps_dts_packet_order(self):
        rows = [(1,1000,1000,1),(0,1040,1120,2),(0,1080,1080,3),
                (1,1064,1064,4),(0,1120,1200,5)]
        for flag in ('0','1'):
            states,rtp,_ = run('va',rows,flag)
            self.check_packets(rows,rtp)
            video = [packet for packet in rtp if packet['stream']==0]
            self.assertEqual((video[1]['stamp']-video[0]['stamp']) & 0xffffffff, (-3600) & 0xffffffff)
            self.assertEqual(states[-1]['mode'], 'pass' if flag=='1' else 'dts')

    def test_invalid_dts_after_transition_is_rejected_and_consumed(self):
        rows = self.normal + [(0,1120,1200,7)]
        states,rtp,log = run('va',rows,expect_packet_error=True)
        self.assertTrue(any(state['ret'] < 0 and state['mode']=='pass' for state in states))
        self.assertIn('non monotonically increasing',log)
        self.check_packets(self.normal,rtp)


if __name__ == '__main__':
    unittest.main(verbosity=2)
