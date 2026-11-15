"""Injected POST lifecycle cases; no user server, image file or model access."""
import json
import socket
import threading
import time
import traceback
from types import SimpleNamespace

import pytest

from core import lmstudio_description_transport as transport
from core.lmstudio_description import DescriptionError
from test_lmstudio_transport import FakeConnection, factory

PAYLOAD = json.dumps(dict(model_instance_id="fixture-instance",
    output=[dict(type="message", content="A generated fixture.")])).encode()
INPUTS = dict(model_key="fixture-model", expected_instance_id="fixture-instance",
              prompt="Describe the generated fixture.", image_bytes=b"synthetic bytes",
              image_mime="image/png", temperature=0, max_output_tokens=256)


class Connection(FakeConnection):
    def __init__(self, **kwargs):
        super().__init__(body=kwargs.pop("body", PAYLOAD), **kwargs)
        self.connects = self.posts = 0
        self.debug = None

    def set_debuglevel(self, value):
        self.debug = value

    def connect(self):
        self.connects += 1
        super().connect()

    def request(self, *args, **kwargs):
        self.posts += 1
        super().request(*args, **kwargs)


def invoke(connection=None, **changes):
    values = dict(INPUTS)
    values.update(changes)
    if connection is not None and 'connection_factory' not in values:
        values['connection_factory'] = factory(connection)
    return transport.describe(values.pop('endpoint', 'http://localhost:4321'), **values)


@pytest.fixture(autouse=True)
def client_workers_retire(monkeypatch):
    workers = []
    def worker(*args, **kwargs):
        result = threading.Thread(*args, **kwargs)
        workers.append(result)
        return result
    def timer(*args, **kwargs):
        result = threading.Timer(*args, **kwargs)
        workers.append(result)
        return result
    monkeypatch.setattr(transport, 'threading', SimpleNamespace(
        Thread=worker, Timer=timer, Event=threading.Event, Lock=threading.Lock))
    yield
    assert all(not worker.is_alive() for worker in workers)


def drained(connection):
    assert connection.closed and connection.sock.closed
    if connection.sock.duplicate is not None:
        assert connection.sock.duplicate.closed


def test_one_internal_codec_body_fixed_headers_and_result():
    connection = Connection()
    seen = []
    value = invoke(connection, token='ephemeral-fixture-token',
                   connection_factory=factory(connection, seen))
    assert value.description == 'A generated fixture.'
    assert connection.connects == connection.posts == len(connection.requests) == 1
    method, route, body, headers = connection.requests[0]
    assert method == 'POST' and route == '/api/v1/chat'
    encoded = json.loads(body)
    assert encoded['store'] is encoded['stream'] is False
    assert set(encoded) == {'model','input','store','stream','temperature','max_output_tokens'}
    assert headers == {'Accept':'application/json','Content-Type':'application/json',
        'Content-Length':str(len(body)),'Connection':'close',
        'Authorization':'Bearer ephemeral-fixture-token'}
    assert connection.debug == 0 and connection.response_closed
    assert seen[0][:2] == ('127.0.0.1',4321)
    drained(connection)


@pytest.mark.parametrize('changes,code', [
    ({'endpoint':'https://127.0.0.1:1234'}, 'invalid_endpoint'),
    ({'endpoint':'http://example.invalid:1234'}, 'invalid_endpoint'),
    ({'endpoint':'http://127.0.0.1'}, 'explicit_port_required'),
    ({'endpoint':'http://user:secret@127.0.0.1:1234'}, 'invalid_endpoint'),
    ({'token':'bad\r\nheader'}, 'invalid_token'),
    ({'prompt':''}, 'invalid_prompt'),
    ({'cancelled':None}, 'invalid_request'),
    ({'limits':object()}, 'invalid_request'),
    ({'cancelled':lambda:True}, 'description_cancelled'),
])
def test_preflight_never_constructs_a_connection(changes, code):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Unexpected connection')
    with pytest.raises(transport.DescriptionTransportError) as error:
        invoke(connection_factory=forbidden, **changes)
    assert str(error.value) == code and error.value.submission_state == 'not_sent'
    assert not error.value.cleanup_failed and not calls


@pytest.mark.parametrize('changes', [dict(total_seconds=True),dict(total_seconds=301),
    dict(total_seconds=float('nan')),dict(connect_seconds=0),dict(io_seconds=121),
    dict(response_bytes=True),dict(response_bytes=4*1024*1024+1)])
def test_invalid_limits(changes):
    with pytest.raises(DescriptionError, match='^invalid_limits$'):
        transport.DescriptionLimits(**changes)


def test_no_arbitrary_body_or_caller_headers_api():
    for changes in [dict(body=b'evil'),dict(headers={}),dict(route='/api/v1/models')]:
        with pytest.raises(TypeError):
            invoke(**changes)


@pytest.mark.parametrize('status,code', [(302,'redirect_refused'),(401,'authentication_required_or_rejected'),
    (403,'authentication_required_or_rejected'),(404,'unsupported_route'),
    (405,'unsupported_route'),(503,'http_unavailable')])
def test_http_refusals_never_repeat(status, code):
    connection = Connection(status=status)
    with pytest.raises(transport.DescriptionTransportError) as error:
        invoke(connection)
    assert str(error.value) == code and error.value.submission_state == 'outcome_unknown'
    assert connection.connects == connection.posts == 1
    drained(connection)


@pytest.mark.parametrize('headers,code', [
    ([('Content-Type','text/html')],'non_json_response'),
    ([('Content-Type','application/json'),('Content-Encoding','gzip')],'encoded_response_refused'),
    ([('Content-Type','application/json'),('Content-Length','1'),('Content-Length','1')],'duplicate_response_header'),
    ([('Content-Type','application/json'),('Content-Length','1'),('Transfer-Encoding','chunked')],'ambiguous_response_framing'),
])
def test_framing_refusals(headers, code):
    connection = Connection(headers=headers)
    with pytest.raises(transport.DescriptionTransportError) as error:
        invoke(connection)
    assert str(error.value) == code and error.value.submission_state == 'outcome_unknown'
    drained(connection)


def test_unknown_length_byte_cap_and_truncation():
    small = transport.DescriptionLimits(response_bytes=4)
    connection = Connection(headers=[('Content-Type','application/json')])
    with pytest.raises(transport.DescriptionTransportError, match='^response_byte_limit$'):
        invoke(connection, limits=small)
    drained(connection)
    connection = Connection(headers=[('Content-Type','application/json'),
                                    ('Content-Length',str(len(PAYLOAD)+1))])
    with pytest.raises(transport.DescriptionTransportError, match='^truncated_response$'):
        invoke(connection)
    drained(connection)


def test_disconnect_or_partial_write_is_never_safe_retry_and_never_echoes():
    secret = 'private-fixture-token'
    connection = Connection(phase='request')
    with pytest.raises(transport.DescriptionTransportError) as error:
        invoke(connection, token=secret)
    assert error.value.submission_state == 'outcome_unknown'
    assert connection.posts == 1 and connection.requests == []
    assert error.value.__context__ is None
    assert secret not in ''.join(traceback.format_exception(error.value))
    drained(connection)


def test_partial_body_write_retains_unknown_disposition():
    connection = Connection()
    def partial(method, route, *, body, headers):
        connection.posts += 1
        connection.requests.append((method,route,body[:7],{}))
        raise ConnectionResetError('private-generated-error')
    connection.request = partial
    with pytest.raises(transport.DescriptionTransportError) as error:
        invoke(connection)
    assert error.value.submission_state == 'outcome_unknown'
    assert connection.posts == 1 and len(connection.requests[0][2]) == 7
    drained(connection)


def test_connect_failure_is_not_sent_and_socket_still_closes():
    connection = Connection(phase='connect')
    with pytest.raises(transport.DescriptionTransportError) as error:
        invoke(connection)
    assert error.value.submission_state == 'not_sent' and connection.posts == 0
    drained(connection)


@pytest.mark.parametrize('phase',['request','headers','body'])
def test_io_timeout_after_attempt_remains_unknown(phase):
    connection = Connection()
    def action(value):
        if value == phase:
            raise socket.timeout('private-generated-error')
    connection.action = action
    with pytest.raises(transport.DescriptionTransportError,match='^description_io_timeout$') as error:
        invoke(connection)
    assert error.value.submission_state == 'outcome_unknown' and connection.posts == 1
    drained(connection)


@pytest.mark.parametrize('phase', ['request','headers','body'])
@pytest.mark.parametrize('kind', ['cancel','deadline'])
def test_pending_io_interrupts_with_unknown_outcome(phase, kind):
    entered, cancel = threading.Event(), threading.Event()
    connection = Connection()
    def action(value):
        if value == phase:
            entered.set()
            connection.sock.signal.wait(1)
    connection.action = action
    canceller = None
    if kind == 'cancel':
        def stop():
            if entered.wait(.5):
                cancel.set()
        canceller = threading.Thread(target=stop)
        canceller.start()
    limits = transport.DescriptionLimits(connect_seconds=.05,io_seconds=.15,total_seconds=.25)
    started = time.monotonic()
    try:
        with pytest.raises(transport.DescriptionTransportError) as error:
            invoke(connection, cancelled=cancel.is_set, limits=limits)
        assert str(error.value) == ('description_cancelled' if kind=='cancel' else 'description_deadline')
        assert error.value.submission_state == 'outcome_unknown'
        assert time.monotonic()-started < .8 and connection.posts == 1
    finally:
        if canceller is not None:
            canceller.join(1)
            assert not canceller.is_alive()
    drained(connection)


@pytest.mark.parametrize('bad_close', ['response','connection'])
def test_cleanup_failure_cannot_return_success_and_preserves_primary(bad_close):
    for status in [200,503]:
        connection = Connection(status=status,bad_close=bad_close)
        with pytest.raises(transport.DescriptionTransportError) as error:
            invoke(connection)
        assert str(error.value) == ('transport_cleanup_failed' if status==200 else 'http_unavailable')
        assert error.value.cleanup_failed and error.value.submission_state == 'outcome_unknown'
        drained(connection)


def test_decoder_mismatch_is_unknown_not_replayed():
    connection = Connection(body=PAYLOAD.replace(b'fixture-instance',b'wrong-instance'))
    with pytest.raises(transport.DescriptionTransportError, match='^instance_mismatch$') as error:
        invoke(connection)
    assert error.value.submission_state == 'outcome_unknown' and connection.posts == 1
    drained(connection)


def test_cancelled_result_cannot_publish_after_decode(monkeypatch):
    connection = Connection()
    cancel = threading.Event()
    original = transport.decode_description
    def decode(*args):
        result = original(*args)
        cancel.set()
        return result
    monkeypatch.setattr(transport,'decode_description',decode)
    with pytest.raises(transport.DescriptionTransportError, match='^description_cancelled$') as error:
        invoke(connection, cancelled=cancel.is_set)
    assert error.value.submission_state == 'outcome_unknown'
    drained(connection)


def test_stock_send_cannot_implicitly_reconnect(monkeypatch):
    calls = []
    def connect(connection):
        calls.append(True)
        connection.sock = None
    monkeypatch.setattr(transport.http.client.HTTPConnection,'connect',connect)
    connection = transport._SingleConnection('127.0.0.1',4321)
    connection.connect()
    with pytest.raises(DescriptionError, match='^reconnect_refused$'):
        connection.send(b'never-sent')
    assert calls == [True]
    connection.close()
