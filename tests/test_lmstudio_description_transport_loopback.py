"""Actual stock HTTP POST to owned generated loopback fixtures only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from core import lmstudio_description_transport as transport
from test_lmstudio_description_transport import INPUTS, PAYLOAD


@pytest.fixture(autouse=True)
def actual_client_workers_and_sockets_settle(monkeypatch):
    workers, connections = [], []
    def worker(*args, **kwargs):
        value = threading.Thread(*args, **kwargs)
        workers.append(value)
        return value
    def timer(*args, **kwargs):
        value = threading.Timer(*args, **kwargs)
        workers.append(value)
        return value
    monkeypatch.setattr(transport,'threading',SimpleNamespace(
        Thread=worker,Timer=timer,Event=threading.Event,Lock=threading.Lock))
    original_close = transport.http.client.HTTPConnection.close
    def close(connection):
        connections.append(connection)
        return original_close(connection)
    monkeypatch.setattr(transport.http.client.HTTPConnection,'close',close)
    yield
    assert all(not worker.is_alive() for worker in workers)
    assert connections and all(connection.sock is None for connection in connections)


@contextmanager
def fixture_server(mode='ok', *, ipv6=False, cancel=None):
    released, entered = threading.Event(), threading.Event()
    record = dict(connections=0, requests=[], server_retired=False)
    class Server(HTTPServer):
        address_family = socket.AF_INET6 if ipv6 else socket.AF_INET
        def get_request(self):
            value = super().get_request()
            record['connections'] += 1
            return value
        def handle_error(self, *_args):
            pass
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_args):
            pass
        def do_POST(self):
            length = int(self.headers.get('Content-Length','0'))
            assert 0 < length <= 12*1024*1024
            raw = self.rfile.read(length)
            record['requests'].append(dict(method=self.command,path=self.path,
                raw=raw,authorization=self.headers.get('Authorization')))
            entered.set()
            if mode == 'disconnect':
                self.close_connection = True
                return
            code = 302 if mode=='redirect' else 200
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(PAYLOAD)))
            self.send_header('Connection','close')
            if mode == 'redirect':
                self.send_header('Location','/never-follow')
            self.end_headers()
            try:
                if mode in {'cancel','trickle','io'}:
                    self.wfile.write(PAYLOAD[:1]);self.wfile.flush()
                    if mode=='cancel':
                        released.wait(.05)
                        cancel.set()
                    elif mode=='trickle':
                        for byte in PAYLOAD[1:4]:
                            if released.wait(.12):
                                break
                            self.wfile.write(bytes([byte]));self.wfile.flush()
                    released.wait(2)
                else:
                    self.wfile.write(PAYLOAD)
                    self.wfile.flush()
            except (OSError,ValueError):
                pass
            self.close_connection = True
    host = '::1' if ipv6 else '127.0.0.1'
    try:
        server = Server((host,0),Handler)
    except OSError:
        if ipv6:
            pytest.skip('IPv6 loopback unavailable;this branch is unqualified')
        raise
    thread = threading.Thread(target=server.serve_forever,
        kwargs={'poll_interval':.02},name='generated-description-post-fixture')
    thread.start()
    endpoint = 'http://[%s]:%d'%(host,server.server_port) if ipv6 else 'http://%s:%d'%(host,server.server_port)
    try:
        yield endpoint,record
    finally:
        released.set()
        server.shutdown();server.server_close();thread.join(2)
        assert not thread.is_alive()
        record['server_retired'] = True


@pytest.mark.parametrize('ipv6',[False,True],ids=['ipv4','ipv6'])
def test_exact_stock_post_and_no_second_connection(ipv6):
    with fixture_server(ipv6=ipv6) as (endpoint,record):
        result = transport.describe(endpoint,token='generated-ephemeral-token',**INPUTS)
        assert result.description == 'A generated fixture.'
        assert record['connections'] == len(record['requests']) == 1
        row = record['requests'][0]
        assert row['method']=='POST' and row['path']=='/api/v1/chat'
        body = json.loads(row['raw'])
        assert body['model']=='fixture-model' and body['store'] is body['stream'] is False
        assert row['authorization']=='Bearer generated-ephemeral-token'
        assert not hasattr(result,'server_stopped')
    assert record['server_retired']


@pytest.mark.parametrize('mode,code',[('disconnect','transport_unavailable'),('redirect','redirect_refused')])
def test_received_body_disconnect_or_redirect_never_replays(mode,code):
    with fixture_server(mode) as (endpoint,record):
        with pytest.raises(transport.DescriptionTransportError) as error:
            transport.describe(endpoint,**INPUTS)
        assert str(error.value)==code and error.value.submission_state=='outcome_unknown'
        assert record['connections']==len(record['requests'])==1
    assert record['server_retired']


@pytest.mark.parametrize('mode',['cancel','trickle','io'])
def test_stock_pending_io_cancellation_and_budgets(mode):
    cancel = threading.Event()
    limits = transport.DescriptionLimits(connect_seconds=.1,
        io_seconds=.1 if mode=='io' else .3,total_seconds=.35 if mode=='trickle' else .8)
    with fixture_server(mode,cancel=cancel) as (endpoint,record):
        started = time.monotonic()
        with pytest.raises(transport.DescriptionTransportError) as error:
            transport.describe(endpoint,cancelled=cancel.is_set,limits=limits,**INPUTS)
        allowed = {'cancel':{'description_cancelled'},'io':{'description_io_timeout'},
            'trickle':{'description_deadline','description_io_timeout'}}[mode]
        assert str(error.value) in allowed
        assert error.value.submission_state=='outcome_unknown'
        assert time.monotonic()-started < (.58 if mode=='trickle' else .65)
        assert record['connections']==len(record['requests'])==1
    assert record['server_retired']
