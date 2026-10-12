"""Exercise large tool frames with the released SDK and bundled Claude process.

Only inference is a deterministic loopback fixture; the native Read tool,
hooks, transcript mirror and SDK message reader are real.
"""
import asyncio
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import random
import sys
import threading
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

from agent.harnesses.claude_harness import ClaudeAgent, SDK_MAX_BUFFER_SIZE
from agent.context_store import ContextStore


def large_image_case(tmp_path, monkeypatch, *, sdk_default=False, progress=lambda message: None):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    image_path = tmp_path / 'fixture.png'
    fixture = Image.frombytes('RGB', (700, 700), random.Random(42).randbytes(700 * 700 * 3))
    ImageDraw.Draw(fixture).rectangle((300, 300, 400, 400), fill=(20, 200, 40))
    fixture.save(image_path)
    assert image_path.stat().st_size > 1024 * 1024
    requests, images, frames, events, native_actions = [], [], [], [], []
    original_reader = SubprocessCLITransport._read_messages_impl

    async def observe_frames(transport):
        async for message in original_reader(transport):
            frames.append({'type': message.get('type'), 'bytes': len(json.dumps(message).encode())})
            yield message

    monkeypatch.setattr(SubprocessCLITransport, '_read_messages_impl', observe_frames)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            assert self.path == '/tools'
            self.reply([])

        def do_POST(self):
            import base64
            assert self.path.startswith('/v1/messages')
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(True)
            tool_results = [block for message in data['messages'] for block in message.get('content', [])
                            if isinstance(block, dict) and block.get('tool_use_id') == 'read_large_image']
            if tool_results:
                for result in tool_results:
                    assert not result.get('is_error'), result
                def collect_images(value):
                    if isinstance(value, list):
                        for item in value:
                            collect_images(item)
                    elif isinstance(value, dict):
                        if value.get('type') == 'image':
                            # Native Read may transcode PNG to JPEG. Verify the
                            # decoded dimensions and visual marker, not encoding.
                            with Image.open(io.BytesIO(base64.b64decode(value['source']['data']))) as received:
                                images.append({'size': received.size,
                                               'marker': received.convert('RGB').getpixel((350, 350))})
                        else:
                            for item in value.values():
                                collect_images(item)
                collect_images(data['messages'])
                block = {'type': 'text', 'text': 'Large image received; tool completed once.'}
            else:
                block = {'type': 'tool_use', 'id': 'read_large_image', 'name': 'Read',
                         'input': {'file_path': str(image_path)}}
            done = block['type'] == 'text'
            message = {'id': 'msg_' + str(len(requests)), 'type': 'message', 'role': 'assistant',
                       'model': 'claude-sonnet-4-5', 'content': [block],
                       'stop_reason': 'end_turn' if done else 'tool_use', 'stop_sequence': None,
                       'usage': {'input_tokens': 50, 'output_tokens': 10}}
            if not data.get('stream'):
                return self.reply(message)
            stream = [dict(type='message_start', message={**message, 'content': [], 'stop_reason': None}),
                      dict(type='content_block_start', index=0,
                           content_block={'type': 'text', 'text': ''} if done else {**block, 'input': {}}),
                      dict(type='content_block_delta', index=0,
                           delta={'type': 'text_delta', 'text': block['text']} if done else
                                 {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}),
                      dict(type='content_block_stop', index=0),
                      dict(type='message_delta', delta={'stop_reason': message['stop_reason'], 'stop_sequence': None},
                           usage={'output_tokens': 10}), dict(type='message_stop')]
            wire = ''.join('event: ' + frame['type'] + '\ndata: ' + json.dumps(frame) + '\n\n'
                           for frame in stream).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'large-image-fixture')
    store = ContextStore(tmp_path / 'session' / 'context.sqlite3', 'large-image-fixture')
    store.initialize([])

    def native(body):
        native_actions.append(body['action'])
        return {'lease': body['lease'], 'state': None, 'reason': 'fresh'}

    relay = SimpleNamespace(url=f'http://127.0.0.1:{server.server_port}', native=native)
    agent = ClaudeAgent(spec={'model': 'anthropic/claude-sonnet-4-5', 'timeout': 30, 'max_iterations': 3},
        relay=relay, config={'mcp_servers': {'workspace': {'command': sys.executable,
            'args': [str(Path(__file__).resolve().parents[1] / 'agent/tools/mcp_bridge.py')],
            'env': {'WORKSPACE_BROKER_URL': relay.url, 'WORKSPACE_RUN_TOKEN': 'large-image-fixture'}}}},
        activity=SimpleNamespace(start=lambda *args: events.append(('start', args)),
            complete=lambda *args: events.append(('complete', args)), commentary=lambda text: None),
        step=lambda: None, cwd=str(tmp_path), definition=None, context_store=store)
    if sdk_default:
        options = agent.options
        def default_options(system_message):
            value = options(system_message)
            value.max_buffer_size = None
            return value
        monkeypatch.setattr(agent, 'options', default_options)
    progress(f'Native Read input: {image_path.stat().st_size:,} byte synthetic PNG.')
    try:
        result = agent.run_conversation('Read the fixture image once.', conversation_history=[],
                                        system_message='Local SDK transport fixture.')
        completed = [args for phase, args in events if phase == 'complete']
        proof = {'completed': result['completed'], 'model_requests': len(requests),
                 'image_received': len(images) == 1 and images[0]['size'] == (700, 700),
                 'image_marker_verified': len(images) == 1 and
                     all(abs(actual - expected) <= 5 for actual, expected in zip(images[0]['marker'], (20, 200, 40))),
                 'completed_tools': len(completed),
                 'pending_tools': len(agent.journal.pending), 'largest_frame': max(frames, key=lambda item: item['bytes']),
                 'sdk_failure': result.get('sdk_failure'), 'answer': result['final_response'],
                 'native_mirror_enabled': agent.transcript is not None}
        progress(json.dumps(proof))
        return proof
    finally:
        agent.close()
        store.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_sdk_large_image_reaches_model_with_one_receipt(tmp_path, monkeypatch):
    proof = large_image_case(tmp_path, monkeypatch)
    assert proof['completed'], proof
    assert proof['image_received'] and proof['image_marker_verified'] and proof['completed_tools'] == 1, proof
    assert proof['pending_tools'] == 0 and proof['model_requests'] == 2
    assert proof['largest_frame']['bytes'] > 1024 * 1024, proof
    assert proof['native_mirror_enabled']


@pytest.mark.parametrize('size,accepted', [(1024 * 1024 + 1, True), (SDK_MAX_BUFFER_SIZE, True),
                                          (SDK_MAX_BUFFER_SIZE + 1, False)])
def test_sdk_reader_keeps_a_finite_message_limit(tmp_path, monkeypatch, size, accepted):
    from claude_agent_sdk import CLIJSONDecodeError
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from test_claude_sdk import make_agent
    agent, _ = make_agent(monkeypatch, tmp_path)
    transport = SubprocessCLITransport('fixture', agent.options('fixture'))
    line = '{"payload":"' + 'x' * (size - len('{"payload":""}')) + '"}\n'
    async def chunks():
        for offset in range(0, len(line), 65536):
            yield line[offset:offset + 65536]
    async def wait(): return 0
    transport._process = SimpleNamespace(wait=wait)
    transport._stdout_stream = chunks()
    async def read(): return [message async for message in transport._read_messages_impl()]
    if accepted:
        assert len(asyncio.run(read())) == 1
    else:
        with pytest.raises(CLIJSONDecodeError):
            asyncio.run(read())


@pytest.mark.parametrize('kind', ['limit', 'invalid_json', 'unrelated_exception'])
def test_buffer_failure_is_actionable_without_publishing_payloads(tmp_path, monkeypatch, kind):
    from claude_agent_sdk import CLIJSONDecodeError
    from test_claude_sdk import make_agent
    limit_message = f'JSON message exceeded maximum buffer size of {SDK_MAX_BUFFER_SIZE} bytes'
    error = (RuntimeError(limit_message) if kind == 'unrelated_exception' else
             CLIJSONDecodeError(limit_message if kind == 'limit' else 'private-image-data',
                                ValueError('private-provider-payload')))
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def query(self, prompt): pass
        async def receive_messages(self):
            raise error
            yield
    monkeypatch.setattr('claude_agent_sdk.ClaudeSDKClient', Client)
    agent, events = make_agent(monkeypatch, tmp_path)
    try:
        result = agent.run_conversation('Read the image', conversation_history=[], system_message='fixture')
        assert result['failed'] and not result['completed']
        assert 'private-' not in json.dumps([result, events])
        diagnostic = result['sdk_failure']
        if kind == 'limit':
            assert diagnostic['code'] == 'sdk_message_buffer_exceeded'
            assert diagnostic['buffer_limit_bytes'] == SDK_MAX_BUFFER_SIZE
            assert 'sdk_message_buffer_exceeded' in result['final_response']
        else:
            assert 'code' not in diagnostic and 'buffer_limit_bytes' not in diagnostic
    finally:
        agent.close()
