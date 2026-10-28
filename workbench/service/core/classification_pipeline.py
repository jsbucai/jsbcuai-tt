"""Isolate uncertain allocation components without hiding queryable ERP records."""
import copy
import json
from collections import defaultdict
from .logistics import key, calculate, RULE_VERSION
from .business_rules import calculate_snapshot, identity
from .order_lifecycle import release_receipt_orders, lifecycle_plan


def issue(reason, code='calculation_incomplete', **scope):
    return {'code':code, 'reason':reason, 'message':reason, **scope}


def scope_key(value):
    if isinstance(value, dict):
        return key(value)
    if isinstance(value, (list, tuple)) and len(value) == 5:
        result = tuple(str(x).strip() if x is not None else '' for x in value)
        if all(result):
            return result
    raise ValueError('库存范围身份不完整')


def components(snapshot):
    """Orders joined by any shared five-dimensional inventory key form a unit."""
    order_keys, key_orders = {}, defaultdict(set)
    for order in snapshot['orders']:
        number = str(order['order_no'])
        keys = {key(line) for line in order['lines']}
        if not keys:
            raise ValueError('订单没有可确认的库存范围')
        order_keys[number] = keys
        for item in keys:
            key_orders[item].add(number)
    groups, visited = [], set()
    for seed in sorted(order_keys):
        if seed in visited:
            continue
        numbers, keys, pending = set(), set(), {seed}
        while pending:
            number = pending.pop()
            if number in numbers:
                continue
            numbers.add(number)
            for item in order_keys[number]:
                if item not in keys:
                    keys.add(item)
                    pending.update(key_orders[item] - numbers)
        visited.update(numbers)
        groups.append((numbers, keys))
    return groups, order_keys


def compact_issues(items, limit=8):
    """Bound per-order explanations; retain original evidence once in the job."""
    grouped = {}
    for item in items:
        code=str(item.get('code','calculation_incomplete'))[:96]
        reason=str(item.get('reason',item.get('message','分类资料待核对')))[:400]
        signature=(code,reason)
        target=grouped.setdefault(signature,{'scope':'allocation','code':code,'reason':reason,'message':reason,
                                             'issue_count':0,'origin_order_examples':[]})
        target['issue_count']+=int(item.get('issue_count',1))
        origin=item.get('order_no')
        examples=target['origin_order_examples']
        if origin and origin not in examples and len(examples)<3:
            examples.append(origin)
    output=[grouped[k] for k in sorted(grouped)]
    if len(output)>limit:
        omitted=output[limit-1:]
        count=sum(i['issue_count'] for i in omitted)
        output=output[:limit-1]+[issue(f'另有 {count} 项分类资料待核对；原始问题已保存于同步记录',
                                    'additional_calculation_issues',scope='allocation',issue_count=count)]
    return output


def allocation_scope(result, order_no):
    group=result.get('order_scope_ids',{}).get(order_no)
    if group is not None:
        return result['scope_groups'][group]
    return result.get('order_scopes',{}).get(order_no,[])


def _index_state(previous):
    events, orders = defaultdict(list), defaultdict(list)
    for item_id, event in previous.get('events',{}).items():
        events[(event['warehouse'],event['sku'])].append(item_id)
    for item_id in previous.get('orders',{}):
        parts=json.loads(item_id)
        orders[tuple(parts[1:3])].append(item_id)
    return events,orders


def _state_slice(previous, keys, index=None):
    if previous is None:
        return None
    state = {k:copy.deepcopy(v) for k,v in previous.items() if k not in {'events','balances','orders','scope_periods'}}
    business_keys = {identity(dict(zip(('profile_id','warehouse_id','product_id','sku_id','base_unit'), k))) for k in keys}
    event_index,order_index=index or _index_state(previous)
    state['events']={item_id:copy.deepcopy(previous['events'][item_id]) for k in business_keys for item_id in event_index.get(k,[])}
    state['balances']={item_id:previous['balances'][item_id] for item_id in state['events'] if item_id in previous.get('balances',{})}
    state['orders']={item_id:copy.deepcopy(previous['orders'][item_id]) for k in business_keys for item_id in order_index.get(k,[])}
    previous_periods=previous.get('scope_periods',{})
    state['scope_periods']={encoded:previous_periods[encoded] for k in keys if (encoded:=json.dumps(k,ensure_ascii=False)) in previous_periods}
    periods = list(state['scope_periods'].values())
    periods = [p for p in periods if p]
    if periods:
        state['period_start']=min(p[0] for p in periods)
        state['period_end']=max(p[1] for p in periods)
    return state


def _merge_state(previous, updates, profile):
    state = copy.deepcopy(previous) if previous is not None else {'version':1,'events':{},'balances':{},'orders':{}}
    base=previous if previous is not None else state
    index=_index_state(base)
    for keys, update in updates:
        prior_slice = _state_slice(base, keys, index)
        for name in ('events','balances','orders'):
            for old in prior_slice.get(name, {}):
                state.setdefault(name, {}).pop(old, None)
            state.setdefault(name, {}).update(update.get(name, {}))
        state['dispatch_version'] = update['dispatch_version']
        for key_value in keys:
            state.setdefault('scope_periods',{})[json.dumps(key_value,ensure_ascii=False)] = [update['period_start'],update['period_end']]
        # Conservative global watermark retains the oldest unprocessed range.
        for field in ('period_start','period_end'):
            state[field] = min(state.get(field, update[field]), update[field])
    state['profile_id'] = str(profile)
    return state


def calculate_query_snapshot(snapshot, previous_state=None, today=None):
    """Only complete connected components are calculated and committed to ledger."""
    source_issues = copy.deepcopy(snapshot.get('calculation_issues', []))
    date_warnings = [item for item in source_issues if item.get('scope') == 'order'
                     and item.get('code') in {'delivery_date_missing', 'delivery_date_invalid'}]
    issues = [item for item in source_issues if not (item.get('scope') == 'order' and
              item.get('code') in {'delivery_date_missing', 'delivery_date_invalid'})]
    date_warning_orders = {item.get('order_no') for item in date_warnings}
    previous_state, released_receipt_orders = release_receipt_orders(
        previous_state, snapshot.get('receipt_release_order_nos', []))
    result = {'plans':[], 'conflicts':[], 'allocations':[], 'rule_version':RULE_VERSION,
              'issues':[], 'order_scopes':{}, 'scope_groups':{}, 'order_scope_ids':{},
              'blocked_orders':[], 'partial_allowed':True,
              'write_blocks':[], 'released_receipt_orders':released_receipt_orders}
    try:
        groups, order_keys = components(snapshot)
        result['order_scopes'] = {n:[list(k) for k in sorted(keys)] for n,keys in order_keys.items()}
        for index,(numbers,keys) in enumerate(groups):
            group_id=str(index)
            result['scope_groups'][group_id]=[list(k) for k in sorted(keys)]
            result['order_scope_ids'].update({n:group_id for n in numbers})
    except (ValueError, KeyError, TypeError):
        groups, order_keys = [], {}
        issues.append(issue('库存身份范围无法确定，本批物流计算暂停', scope='global'))
    if snapshot.get('rules_contract') == 'v2':
        if previous_state is None:
            if snapshot.get('receipt_state_bootstrap_verified') is True and isinstance(snapshot.get('receipt_initial_state'), dict):
                previous_state = snapshot['receipt_initial_state']
            else:
                issues.append(issue('首次分类的入库账本尚未完成身份映射和初始化核对', 'receipt_bootstrap_required', scope='global'))
        if previous_state is not None and (not isinstance(previous_state,dict) or previous_state.get('version')!=1
                or str(previous_state.get('profile_id'))!=str(snapshot['profile_id'])
                or any(not isinstance(previous_state.get(name,{}),dict) for name in ('events','balances','orders','scope_periods'))):
            issues.append(issue('入库账本结构或账套身份待核对', 'receipt_ledger_invalid', scope='global'))
        if snapshot.get('receipt_events_complete') is not True:
            issues.append(issue('实际到货流水尚未完整核实', 'receipt_events_incomplete', scope='global'))
    global_issues, order_issues, key_issues = [], defaultdict(list), defaultdict(list)
    for item in issues:
        item.setdefault('message', item.get('reason','分类资料待核对'))
        if item.get('scope') == 'order' and item.get('order_no') in order_keys:
            order_issues[item['order_no']].append(item)
        elif item.get('scope') == 'inventory':
            try:
                key_issues[scope_key(item['inventory_key'])].append(item)
            except (ValueError, KeyError, TypeError):
                global_issues.append(item)
        else:
            global_issues.append(item)
    stocks = defaultdict(list)
    for row in snapshot['inventory']:
        try:
            stocks[key(row)].append(row)
        except (ValueError, TypeError):
            global_issues.append(issue('库存记录身份不完整',scope='global'))
    # A source issue is retained once for audit, never once for every affected order.
    result['source_issues']=issues + date_warnings
    result['warnings']=date_warnings
    global_issues=compact_issues(global_issues)
    state_index=None
    if not global_issues and previous_state is not None:
        try:
            state_index=_index_state(previous_state)
        except (ValueError,KeyError,TypeError,AttributeError):
            global_issues=compact_issues([issue('入库账本明细或订单身份损坏，保留原账本等待核对',
                                               'receipt_ledger_invalid',scope='global')])
    source_indexes={name:defaultdict(list) for name in ('receipt_events',)}
    if snapshot.get('rules_contract')=='v2' and not global_issues:
        try:
            for name,source_index in source_indexes.items():
                for row in snapshot[name]:
                    source_index[key(row)].append(row)
        except (ValueError,KeyError,TypeError):
            global_issues=compact_issues([issue('采购或入库记录范围身份不完整',scope='global')])
    updates = []
    orders_by_number={o['order_no']:o for o in snapshot['orders']}
    for numbers, keys in groups:
        reasons = list(global_issues)
        reasons.extend(item for n in numbers for item in order_issues[n])
        reasons.extend(item for k in keys for item in key_issues[k])
        if not reasons:
            part = dict(snapshot)
            part['orders'] = [copy.deepcopy(orders_by_number[n]) for n in sorted(numbers)]
            for order in part['orders']:
                order['lines'].sort(key=lambda line:str(line['line_id']))
                if order['order_no'] in date_warning_orders:
                    order['delivery_date'] = None
                    for line in order['lines']:
                        line['delivery_date'] = None
            part['inventory'] = [row for k in sorted(keys) for row in stocks[k]]
            try:
                if snapshot.get('rules_contract') == 'v2':
                    for name in ('receipt_events',):
                        part[name] = sorted((row for k in keys for row in source_indexes[name][k]),
                            key=lambda row:json.dumps(row,sort_keys=True,ensure_ascii=False))
                    calculated = calculate_snapshot(part, _state_slice(previous_state, keys, state_index), today)
                else:
                    calculated = calculate(part['orders'],part['inventory'],today)
                if calculated['conflicts']:
                    reasons = [issue(c['reason'],c.get('code','calculation_incomplete'),
                        scope='order',order_no=c.get('order_no', min(numbers))) for c in calculated['conflicts']]
                else:
                    result['plans'].extend(calculated['plans'])
                    result['allocations'].extend(calculated['allocations'])
                    if 'receipt_state' in calculated:
                        updates.append((keys,calculated['receipt_state']))
            except (ValueError, TypeError, KeyError, OverflowError):
                reasons = [issue('该库存分配范围的需求、库存或批次证据存在冲突',scope='order',order_no=min(numbers))]
        reasons=compact_issues(reasons)
        reason_text='；'.join(r['reason'] for r in reasons)
        for number in sorted(numbers) if reasons else []:
            result['blocked_orders'].append({'order_no':number,'reason':reason_text})
            result['issues'].extend(dict(r, affected_order_no=number) for r in reasons)
    if not groups:
        for order in snapshot['orders']:
            number=order['order_no']
            result['blocked_orders'].append({'order_no':number,'reason':'库存分配范围无法确认'})
            result['issues'].extend(dict(r,affected_order_no=number) for r in global_issues)
    result['conflicts'] = list(result['blocked_orders'])
    result['plans'].sort(key=lambda p:p['tradeid'])
    if updates:
        result['receipt_state'] = _merge_state(previous_state,updates,snapshot['profile_id'])
    elif released_receipt_orders:
        # A 600 transition may leave no active component to recalculate.  The
        # released order provenance still has to commit with lifecycle state.
        result['receipt_state'] = previous_state

    # State-500 demand has already passed through the normal FIFO above.  Now
    # replace only its classification.  Candidate/missing-code plans remain
    # local display plans and are filtered from ERP writes by write_enabled.
    lifecycle_orders = {str(order['order_no']): order for order in snapshot['orders']
                        if order.get('shadow_demand')}
    if lifecycle_orders:
        existing = {plan['tradeid']:plan for plan in result['plans']}
        blocked = {item['order_no'] for item in result['blocked_orders']}
        freight = snapshot.get('lifecycle_freight') if isinstance(
            snapshot.get('lifecycle_freight'), dict) else {
                'available':False, 'code':'', 'reason':'物流公司字典未验证'}
        for number, order in lifecycle_orders.items():
            if number not in existing:
                result['write_blocks'].append({'order_no':number,
                    'code':'lifecycle_allocation_incomplete',
                    'reason':'需结存订单已保留数量，但共享库存范围尚未完成分配核对'})
                continue
            plan = lifecycle_plan(order, freight)
            existing[number] = plan
            if order.get('lifecycle_pending_review'):
                plan['write_enabled'] = False
                result['write_blocks'].append({'order_no':number,
                    'code':'lifecycle_pending_review',
                    'reason':'订单处理状态精确查询待核对，暂停物流写回'})
            elif order.get('lifecycle_order_date_fallback'):
                plan['write_enabled'] = False
                result['write_blocks'].append({'order_no':number,
                    'code':'lifecycle_order_date_unverified',
                    'reason':'需结存历史订单缺少已验证订货日期；已保守占用库存，暂停共享范围写回'})
            elif order.get('lifecycle_state') == 'candidate':
                result['write_blocks'].append({'order_no':number,
                    'code':'lifecycle_confirmation_pending',
                    'reason':'process_state=500 尚待下一次成功更新连续确认'})
            elif not plan['write_enabled']:
                code = 'lifecycle_delivery_date_missing' if not plan['freightnumber'] \
                    else 'lifecycle_freight_mapping_missing'
                reason = '需结存订单缺少交期，不写回 ERP' if not plan['freightnumber'] \
                    else freight.get('reason') or '需结存物流公司编码未验证，不写回 ERP'
                result['write_blocks'].append({'order_no':number,'code':code,'reason':reason})
        review_groups = {result.get('order_scope_ids', {}).get(number)
                         for number, order in lifecycle_orders.items()
                         if order.get('lifecycle_pending_review') or
                         order.get('lifecycle_order_date_fallback')}
        review_groups.discard(None)
        already_blocked = {item['order_no'] for item in result['write_blocks']}
        for number, plan in existing.items():
            if result.get('order_scope_ids', {}).get(number) in review_groups:
                plan['write_enabled'] = False
                if number not in already_blocked:
                    result['write_blocks'].append({'order_no':number,
                        'code':'lifecycle_scope_pending_review',
                        'reason':'共享库存范围内有消失订单状态待核对，暂停本范围物流写回'})
        result['plans'] = sorted(existing.values(), key=lambda plan:plan['tradeid'])
        result['write_blocks'].sort(key=lambda item:(item['order_no'],item['code']))
    existing_blocks = {item['order_no'] for item in result['write_blocks']}
    for plan in result['plans']:
        if plan.get('write_enabled') is False and plan['tradeid'] not in existing_blocks:
            result['write_blocks'].append({'order_no':plan['tradeid'],
                'code':'local_classification_only',
                'reason':plan.get('write_block_reason') or '该订单仅作本地分类，不写回ERP'})
    return result
