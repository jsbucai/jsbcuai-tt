"""Private stdio Codex adapter. No public socket, tool execution or ERP access."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
import tomllib


class AppServerError(ValueError):
    pass


def invoke(binary, cwd, prompt, schema, images, env, disabled_features, *, timeout=180):
    # User-installed MCPs would bypass the otherwise disabled native tools.
    home = Path(env.get('CODEX_HOME', str(Path(env['HOME']) / '.codex')))
    config = home / 'config.toml'
    if config.exists():
        settings = tomllib.loads(config.read_text())
        if settings.get('mcp_servers') or settings.get('profiles'):
            raise AppServerError('codex_unreviewed_configuration')
    command = [str(binary), '--no-daemon', '-a', 'never']
    for feature in disabled_features:
        command += ['--disable', feature]
    command += ['--enable', 'skip_host_skill_discovery', '-c', 'web_search="disabled"',
                'app-server', '--stdio']
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, start_new_session=True, cwd=cwd)
    previous_term=signal.getsignal(signal.SIGTERM)
    def interrupted(_signum,_frame):
        raise AppServerError('codex_cancelled')
    signal.signal(signal.SIGTERM,interrupted)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'out')
    selector.register(process.stderr, selectors.EVENT_READ, 'err')
    started = time.monotonic(); pending = b''; total = 0; messages = {}; thread = ''
    def send(value):
        process.stdin.write((json.dumps(value, ensure_ascii=False)+'\n').encode()); process.stdin.flush()
    send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'ul_sales','version':'0.5.23'},
          'capabilities':{'experimentalApi':True}}})
    try:
        while time.monotonic()-started < timeout:
            for key, _ in selector.select(.2):
                raw = os.read(key.fileobj.fileno(), 65536)
                if not raw:
                    selector.unregister(key.fileobj)
                    if key.data == 'out': raise AppServerError('codex_process_failed')
                    continue
                total += len(raw)
                if total > 8*1024*1024: raise AppServerError('codex_output_too_large')
                if key.data != 'out': continue
                pending += raw
                while b'\n' in pending:
                    raw, pending = pending.split(b'\n', 1)
                    if not raw.strip(): continue
                    event = json.loads(raw)
                    if 'method' in event and 'id' in event:
                        # Never approve shell, network, credentials or tool requests.
                        send({'id':event['id'],'error':{'code':-32601,'message':'Tools disabled'}})
                        raise AppServerError('codex_tool_use_forbidden')
                    if 'error' in event: raise AppServerError('codex_protocol_error')
                    if event.get('id') == 1:
                        send({'method':'initialized','params':{}})
                        send({'id':2,'method':'thread/start','params':{'cwd':str(cwd),'ephemeral':True,
                            'approvalPolicy':'never','sandbox':'read-only',
                            'baseInstructions':'Only analyze the supplied business evidence. Do not use any tools.',
                            'config':{'web_search':'disabled'}}})
                    elif event.get('id') == 2:
                        thread = event['result']['thread']['id']
                        send({'id':3,'method':'turn/start','params':{'threadId':thread,
                            'input':[{'type':'text','text':prompt}]+[{'type':'localImage','path':str(p)} for p in images],
                            'outputSchema':schema}})
                    method = event.get('method'); params = event.get('params',{})
                    if method in ('item/started','item/completed'):
                        item = params.get('item',{}); kind = item.get('type')
                        if kind not in ('userMessage','agentMessage','reasoning','plan','contextCompaction'):
                            raise AppServerError('codex_tool_use_forbidden')
                        if method == 'item/completed' and kind == 'agentMessage':
                            messages[item['id']] = item.get('text','')
                    elif method == 'turn/completed':
                        if params.get('turn',{}).get('status') != 'completed':
                            raise AppServerError('codex_turn_failed')
                        for value in reversed(list(messages.values())):
                            try: result = json.loads(value)
                            except ValueError: continue
                            return result, {'elapsed_seconds':round(time.monotonic()-started,3),
                                'stream_bytes':total,'tool_calls':0,'transport':'app-server-stdio'}
                        raise AppServerError('codex_structured_result_missing')
            if process.poll() is not None: raise AppServerError('codex_process_failed')
        raise AppServerError('codex_timed_out')
    finally:
        signal.signal(signal.SIGTERM,previous_term)
        selector.close()
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=3)
        for stream in (process.stdin,process.stdout,process.stderr): stream.close()
