"""Owner-only Weixin ClawBot notifications using Tencent's documented iLink API.

Owner messages may enter the separately configured sales journal. This transport
never executes business actions itself. Credentials stay local.
"""
import argparse
import base64
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
API = 'https://ilinkai.weixin.qq.com'
VERSION = '2.4.10'
CONFIG = Path('/etc/ul-weixin-notify')
STATE = Path('/var/lib/ul-weixin-notify')
REPORTS = Path('/var/lib/ul-guardian')
STOP = datetime(2026, 10, 17, tzinfo=TZ)
SALES_CONFIG = Path('/etc/ul-weixin-sales/config.json')
LABELS = {
    'sample_missing_or_stale': '运行采样缺失或过期', 'database_check_failed': '数据库只读检查失败',
    'health_failed': 'App 后台健康检查失败', 'sustained_low_memory': '可用内存持续不足',
    'container_oom': '业务进程因内存不足退出', 'container_cgroup_oom_recorded': '检测到内存不足杀进程记录',
    'container_restarted': '业务容器发生重启', 'disk_below_reserve': '磁盘低于安全余量',
    'daytime_source_delayed': '白天订单数据更新延迟', 'update_queue_growing': '更新任务积压',
    'disk_growth_requires_review': '磁盘日增长需要检查', 'wal_growth_requires_review': '数据库临时日志持续增长',
    'update_running_too_long': '更新任务运行过久', 'guardian_stale': '巡检报告超过十分钟未更新',
    'codex_unavailable': 'Codex 分析暂不可用，基础巡检仍保留',
}
CRITICAL = {'health_failed', 'database_check_failed', 'sustained_low_memory', 'container_oom',
            'container_cgroup_oom_recorded', 'disk_below_reserve', 'guardian_stale'}


def now():
    return datetime.now(TZ)


def dt(value):
    v = datetime.fromisoformat(value)
    return v.replace(tzinfo=TZ) if v.tzinfo is None else v.astimezone(TZ)


def save(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.notify-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(doc, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load(path, default=None, private=False):
    if not path.exists():
        return {} if default is None else default
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > 2*1024**2:
        raise ValueError('unsafe_file')
    if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError('unsafe_private_file')
    return json.loads(path.read_text())


def base_url(value):
    u = urllib.parse.urlsplit(value)
    if (u.scheme != 'https' or not u.hostname or not u.hostname.endswith('.weixin.qq.com') or
        u.username or u.password or u.port not in (None,443) or u.path not in ('','/') or u.query or u.fragment):
        raise ValueError('untrusted_api_host')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('unexpected_http_redirect')


def request(base, endpoint, body=None, token='', timeout=12):
    headers = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': str((2<<16)|(4<<8)|10)}
    if body is not None:
        headers.update({'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token',
                        'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
    if token:
        headers['Authorization'] = 'Bearer '+token
        body = dict(body, base_info={'channel_version': VERSION, 'bot_agent': 'ULGuardian/1.0'})
    req = urllib.request.Request(base_url(base)+endpoint,
        data=None if body is None else json.dumps(body).encode(), headers=headers)
    # Never inherit arbitrary proxy credentials or forward Bot credentials on redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        raw = response.read(1024**2+1)
        if len(raw) > 1024**2:
            raise ValueError('response_too_large')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('invalid_response')
        return result


def login_start():
    if (CONFIG/'account.json').exists():
        raise ValueError('already_bound_use_existing_account')
    result = request(API, '/ilink/bot/get_bot_qrcode?bot_type=3', {'local_token_list': []})
    if not all(isinstance(result.get(k), str) and result[k] for k in ('qrcode','qrcode_img_content')):
        raise ValueError('qrcode_not_returned')
    save(CONFIG/'login.json', {'qrcode': result['qrcode'], 'base': API, 'created_at': now().isoformat()})
    # This is the QR content, not a bot credential. Display only on the user's computer.
    save(CONFIG/'qr-display.json', {'content': result['qrcode_img_content']})
    return {'status': 'waiting_for_scan'}


def login_poll(verify_code=''):
    pending = load(CONFIG/'login.json', private=True)
    if not pending:
        raise ValueError('no_pending_login')
    if (now()-dt(pending['created_at'])).total_seconds() > 480:
        return {'status': 'expired'}
    query = {'qrcode': pending['qrcode']}
    if verify_code:
        if not verify_code.isdigit() or not 4 <= len(verify_code) <= 10:
            raise ValueError('invalid_verify_code')
        query['verify_code'] = verify_code
    result = request(pending['base'], '/ilink/bot/get_qrcode_status?'+urllib.parse.urlencode(query), timeout=40)
    status = result.get('status', 'unknown')
    if status == 'scaned_but_redirect':
        pending['base'] = base_url('https://'+result['redirect_host']); save(CONFIG/'login.json', pending)
    elif status == 'confirmed':
        if not all(isinstance(result.get(k),str) and result[k] for k in ('bot_token','ilink_bot_id','ilink_user_id','baseurl')):
            raise ValueError('incomplete_login_response')
        account = {k: result[k] for k in ('bot_token','ilink_bot_id','ilink_user_id')}
        account.update(base=base_url(result['baseurl']), bound_at=now().isoformat())
        save(CONFIG/'account.json', account)
        for name in ('login.json', 'qr-display.json'):
            (CONFIG/name).unlink(missing_ok=True)
    return {'status': status}


def issue_label(code):
    if code in LABELS:
        return LABELS[code]
    if code.startswith('stopped_timer:'):
        return '自动任务定时器停止'
    if code.startswith('failed_service:'):
        return '备份或维护任务失败'
    return '存在需要复核的运行异常'


def snapshot(reports, at):
    final = load(reports/'final.json')
    report = final or load(reports/'latest.json')
    findings = sorted(set(report.get('findings', [])))
    if not final and (not report or (at-dt(report['checked_at'])).total_seconds() > 600):
        findings = sorted(set(findings+['guardian_stale']))
    if report.get('last_codex', {}).get('status') == 'failed':
        findings = sorted(set(findings+['codex_unavailable']))
    state = load(reports/'state.json')
    repairs = [r for r in state.get('attempts', []) if r.get('result') in ('started','started_pending_health_check','requested_pending_service_result')]
    return {'findings': findings, 'repair_at': max([r['at'] for r in repairs], default=''),
            'complete': bool(final), 'checked_at': report.get('checked_at', at.isoformat()),
            'current': report.get('current', {}), 'days': report.get('days', {})}


def summary(snap):
    lines = ['UL随行 N150 巡检', '时间：'+snap['checked_at'][:19].replace('T',' ')]
    if snap['complete']:
        lines += ['三日观察已结束，汇总已保存 NAS。观察结束不代表全部验收通过。',
                  '任务页真机流畅度仍需单独核对。']
    elif snap['findings']:
        lines += ['需要关注：'] + ['• '+issue_label(k) for k in snap['findings'][:8]]
    else:
        lines += ['本次未发现阈值异常。']
    cur = snap['current']
    if type(cur.get('memory_available_bytes')) is int:
        lines.append(f"可用内存：{cur['memory_available_bytes']/1024**2:.0f} MiB")
    if type(cur.get('disk_free_bytes')) is int:
        lines.append(f"磁盘剩余：{cur['disk_free_bytes']/1024**3:.1f} GiB")
    lines += ['详细报告：NAS / UL随行 / 运行观察 / N150自主巡检',
              '巡检消息不触发业务操作；录单须使用独立的草稿确认流程。']
    return '\n'.join(lines)


def receive(account, state, payload, at, ingest=None):
    """Only the QR-authorizing owner may refresh context or request a status."""
    command_name = ''
    seen = state.setdefault('seen_messages', [])
    counts = {'received': 0, 'owner': 0, 'ignored': 0}
    for message in payload.get('msgs', []):
        counts['received'] += 1
        if (not isinstance(message,dict) or message.get('from_user_id') != account['ilink_user_id'] or
            message.get('group_id') or message.get('message_type') != 1 or message.get('message_state') != 2 or
            message.get('to_user_id') != account['ilink_bot_id']):
            counts['ignored'] += 1
            continue
        counts['owner'] += 1
        identifier = str(message.get('message_id') or message.get('client_id') or '')
        if not identifier or identifier in seen:
            continue
        texts = [item.get('text_item',{}).get('text','') for item in message.get('item_list',[])
                 if isinstance(item,dict) and item.get('type')==1]
        text = ''.join(t for t in texts if isinstance(t,str)).strip()
        media=any(isinstance(i,dict) and i.get('type')!=1 for i in message.get('item_list',[]))
        if ingest and (media or text not in ('已收到','收到','状态','巡检状态','测试提醒','绑定提醒')):
            ingest(message)  # Persist before acknowledging this ID or advancing the receive cursor.
        seen.append(identifier)
        token = message.get('context_token')
        if isinstance(token,str) and 0 < len(token) <= 16384:
            state['context_token'] = token
            state['context_at'] = at.isoformat()
        if text in ('已收到','收到'):
            state['human_received_at'] = at.isoformat()
            # Acknowledgement applies only to the most recent preceding notification.
            outbox=state.get('outbox',{})
            if outbox:
                latest=max(outbox.values(),key=lambda value:value.get('at',''))
                if latest.get('status') in ('accepted','unknown','dispatching'):
                    latest['human_confirmed_at']=at.isoformat()
        if text in ('状态','巡检状态','测试提醒','绑定提醒'):
            command_name = text
            state['request_id'] = identifier
    state['seen_messages'] = seen[-100:]
    state['last_receive_counts'] = counts
    cursor = payload.get('get_updates_buf')
    if isinstance(cursor,str) and cursor:
        state['cursor'] = cursor
    return command_name


def pending_event(snap, state, at, requested=''):
    target = {k: snap[k] for k in ('findings','repair_at','complete')}
    previous = state.get('accepted_snapshot', {'findings': [], 'repair_at': '', 'complete': False})
    if requested:
        kind = 'owner_status'
        event_id = 'request-'+state['request_id']
    elif not state.get('initial_test_accepted'):
        kind = 'initial_test'; event_id = 'initial-test'
    elif target == previous:
        return None
    else:
        if not snap['complete'] and not (8 <= at.hour < 19) and not (set(snap['findings']) & CRITICAL):
            return None
        kind = 'complete' if snap['complete'] else 'alert' if snap['findings'] else 'recovered'
        event_id = hashlib.sha256(json.dumps([target,state.get('change_sequence',0)],sort_keys=True).encode()).hexdigest()[:24]
    prefix = {'initial_test':'微信提醒连接测试', 'owner_status':'你查询的当前状态',
              'complete':'三日观察汇总', 'alert':'后台异常提醒', 'recovered':'异常恢复或恢复动作记录'}[kind]
    body = prefix+'\n'+summary(snap)
    if kind == 'initial_test':
        body += '\n若在微信看到本条，请回复“已收到”，确认实际送达。'
    return {'id':event_id,'kind':kind,'target':target,'text':body+'\n提醒编号：'+event_id}


def send(account, state, event, at, state_path):
    # Budget includes failures and uncertain responses; avoid flood on retries/restarts.
    today = at.date().isoformat()
    prefix='sales_' if event['kind']=='sales' else ''
    if state.get(prefix+'budget_day') != today:
        state[prefix+'budget_day']=today;state[prefix+'budget_used']=0
    if state.get(prefix+'budget_used',0) >= (300 if prefix else 10):
        return {'status':'daily_limit'}
    previous = state.setdefault('outbox', {}).get(event['id'], {})
    if previous.get('status') in ('accepted','dispatching','unknown'):
        return {'status': 'already_'+previous['status']}
    if previous.get('attempts',0) >= 2:
        return {'status':'retry_limit'}
    if previous.get('at') and (at-dt(previous['at'])).total_seconds() < 900:
        return {'status':'retry_wait'}
    if not state.get('context_token'):
        return {'status':'waiting_for_owner_message'}
    record = {'status':'dispatching','attempts':previous.get('attempts',0)+1,'at':at.isoformat()}
    state['outbox'][event['id']] = record
    state[prefix+'budget_used'] = state.get(prefix+'budget_used',0)+1
    save(state_path,state)
    try:
        response = request(account['base'],'/ilink/bot/sendmessage',{'msg':{
            'from_user_id':'', 'to_user_id':account['ilink_user_id'], 'client_id':'ul-guard-'+event['id'],
            'message_type':2, 'message_state':2, 'context_token':state['context_token'],
            'item_list':event.get('item_list') or [{'type':1,'text_item':{'text':event['text']}}]}},account['bot_token'])
        # Tencent's sendMessage contract allows omitted ret on HTTP success.
        # Do not confuse a complete valid JSON response with a transport timeout.
        code=response.get('ret',0)
        error_code=response.get('errcode',0)
        success=(type(code) is int and code==0 and type(error_code) is int and error_code==0
                 and not response.get('errmsg'))
        if success:
            record['status']='accepted'
            record['acceptance_basis']='ret_zero' if 'ret' in response else 'http_success_no_business_error'
            if event['kind']!='sales':
                first_acceptance = not state.get('initial_test_accepted')
                state['initial_test_accepted']=True
                if first_acceptance or event['kind']!='owner_status':
                    state['accepted_snapshot']=event['target'];state['change_sequence']=state.get('change_sequence',0)+1
        elif (type(code) is int and code!=0) or (type(error_code) is int and error_code!=0):
            code=code or error_code
            record.update(status='rejected',code=code if type(code)is int else 'invalid')
            if code == -14:
                state['pause_until']=(at+timedelta(hours=1)).isoformat()
        else:
            record['status']='unknown'
    except Exception as error:
        # Never log URL, response body, token, user ID or raw exception text.
        record.update(status='unknown',error_type=type(error).__name__)
    save(state_path,state)
    return {'status':record['status'],'kind':event['kind'],'event_id':event['id']}


def sales_channel(account):
    config=load(SALES_CONFIG,private=True)
    if not config or config.get('enabled') is not True:return None
    try:
        from tools.weixin_sales_spool import Spool,account_binding
    except ImportError:
        from weixin_sales_spool import Spool,account_binding
    if config.get('binding')!=account_binding(account):raise ValueError('sales_owner_binding_mismatch')
    if config.get('spool')!='/var/lib/ul-weixin-sales':raise ValueError('sales_spool_path_invalid')
    return Spool(config['spool']),config['binding']


def active_window(at):
    return at<STOP or load(SALES_CONFIG,private=True).get('enabled') is True


def deliver_sales(channel,account,state,at,state_path):
    if not channel:return
    spool,binding=channel
    for _ in range(30):
        reply=spool.next_reply(binding)
        if not reply:return
        spool.reply_state(reply['id'],'sending')
        event={'id':'sales-'+hashlib.sha256(reply['id'].encode()).hexdigest()[:40],
               'kind':'sales','target':{},'text':reply['text']}
        result=send(account,state,event,at,state_path)['status']
        if result in ('accepted','already_accepted'):status='accepted'
        elif result in ('unknown','already_unknown','already_dispatching'):status='unknown'
        elif result in ('rejected','retry_limit'):status='rejected'
        else:
            spool.reply_state(reply['id'],'pending');return
        spool.reply_state(reply['id'],status)
        if state.get('pause_until') and at<dt(state['pause_until']):return


def poll_once(account, reports, state_path):
    """Single receiver; cursor is committed with owner context before any send."""
    at=now()
    state=load(state_path,private=True)
    if state.get('pause_until') and at<dt(state['pause_until']):
        return {'status':'session_paused_rebind_may_be_needed'}
    channel=sales_channel(account)
    try:
        from tools import weixin_erp_verification as verification
        from tools import weixin_business_confirmation as business
    except ImportError:
        import weixin_erp_verification as verification
        import weixin_business_confirmation as business
    try:
        verification.notify(account, state, at, state_path, send, request, NoRedirect)
    except Exception as error:
        state['verification_notify_error_type'] = type(error).__name__
        save(state_path, state)
    deliver_sales(channel,account,state,at,state_path)
    requested=''
    try:
        data=request(account['base'],'/ilink/bot/getupdates',
            {'get_updates_buf':state.get('cursor','')},account['bot_token'],timeout=40)
        at=now()
        if data.get('ret',0)!=0 or data.get('errcode',0)!=0:
            code=data.get('ret') or data.get('errcode')
            if code==-14:
                state['pause_until']=(at+timedelta(hours=1)).isoformat();save(state_path,state)
            return {'status':'receive_rejected','code':code if type(code)is int else 'invalid'}
        requested=receive(account,state,data,at,
                          lambda msg: business.ingest(channel,msg,lambda message: verification.ingest(message,
                              (lambda item:channel[0].ingest(channel[1],item)) if channel else None)))
        state.pop('receive_error_type',None)
        state['last_receive_at']=at.isoformat()
        save(state_path,state)
    except Exception as error:
        state['receive_error_type']=type(error).__name__
        save(state_path,state)
        return {'status':'receive_failed','error_type':type(error).__name__}
    if not active_window(at):
        return {'status':'notification_window_complete'}
    snap=snapshot(reports,at)
    event=pending_event(snap,state,at,requested) if at<STOP or requested else None
    result=send(account,state,event,at,state_path) if event else {'status':'quiet'}
    deliver_sales(channel,account,state,at,state_path)
    state['outbox']={k:v for k,v in state.get('outbox',{}).items() if (at-dt(v['at'])).days<4}
    save(state_path,state)
    return dict(result,checked_at=at.isoformat(),human_received=bool(state.get('human_received_at')),
                owner_messages=len(state.get('seen_messages',[])),has_context=bool(state.get('context_token')))


def lifecycle(account, action):
    result=request(account['base'],'/ilink/bot/msg/notify'+action,{},account['bot_token'])
    code=result.get('ret')
    if type(code) is not int or code != 0 or result.get('errcode',0)!=0:
        code=code or result.get('errcode')
        return {'status':'online_registration_rejected','code':code if type(code)is int else 'invalid'}
    return {'status':'online' if action=='start' else 'offline'}


def run(reports=REPORTS, state_root=STATE, serve=False, stop_event=None):
    stop_event=stop_event or threading.Event()
    at=now()
    if not active_window(at):
        return {'status':'notification_window_complete'}
    state_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=os.open(state_root/'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        account=load(CONFIG/'account.json',private=True)
        if not account:
            return {'status':'not_bound'}
        state_path=state_root/'state.json'
        online=False
        try:
            while not stop_event.is_set() and active_window(now()):
                state=load(state_path,private=True)
                paused=state.get('pause_until') and now()<dt(state['pause_until'])
                try:
                    if paused:
                        result={'status':'session_paused_rebind_may_be_needed'}
                    elif not online:
                        result=lifecycle(account,'start')
                        online=result['status']=='online'
                        if result.get('code')==-14:
                            state['pause_until']=(now()+timedelta(hours=1)).isoformat();save(state_path,state)
                        save(state_root/'status.json',dict(result,checked_at=now().isoformat(),online=online))
                    if online and not paused:
                        result=poll_once(account,reports,state_path)
                except Exception as error:
                    result={'status':'connection_failed','error_type':type(error).__name__}
                result=dict(result,online=online,checked_at=now().isoformat())
                save(state_root/'status.json',result)
                if not serve:
                    return dict(result,online=False)
                # Long polling is normally 35 seconds; throttle immediate empty/error responses too.
                delay=60 if paused or result['status']=='session_paused_rebind_may_be_needed' else 30 if result['status'] in (
                    'receive_rejected','receive_failed','connection_failed','online_registration_rejected') else 1
                stop_event.wait(delay)
            return {'status':'notification_window_complete' if now()>=STOP else 'stopped'}
        finally:
            if online:
                try:
                    lifecycle(account,'stop')
                except Exception:
                    pass
            save(state_root/'status.json',{'status':'notification_window_complete' if now()>=STOP else 'stopped',
                                         'online':False,'checked_at':now().isoformat()})
    finally:
        os.close(lock)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login',action='store_true');parser.add_argument('--poll-login',action='store_true')
    parser.add_argument('--verify-stdin',action='store_true')
    parser.add_argument('--serve',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    stop_event=threading.Event()
    for signum in (signal.SIGINT,signal.SIGTERM):
        signal.signal(signum,lambda *_:stop_event.set())
    try:
        if args.login: result=login_start()
        elif args.poll_login: result=login_poll(sys.stdin.readline().strip() if args.verify_stdin else '')
        else:
            result=run(serve=args.serve,stop_event=stop_event);save(STATE/'status.json',result)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'status':'failed','error_type':type(error).__name__}))
        raise SystemExit(1)


if __name__=='__main__': main()
