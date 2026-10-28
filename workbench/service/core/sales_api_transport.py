"""Explicit sales API adapter; no generic model-callable ERP write method."""
from datetime import datetime
import json
import re
import urllib.parse
import urllib.request
from .sales_api_jobs import SalesAPIError, decimal_json
from .wsgjp_gateway import GATEWAY,NoRedirect,business_signature,compact,post


def signed(client,method,arguments):
    record=client._ready();credentials=record['credentials'];token=record['token']
    params={k:compact(v) if isinstance(v,(dict,list,bool)) else str(v) for k,v in arguments.items()}
    params.update(appkey=credentials['appkey'],token=token['auth_token'],method=method,
                  timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    params['sign']=business_signature(params,credentials['signkey'])
    return params


def read_pages(client,method,collection,params=None):
    if method not in {'erp.employee.listall','erp.btype.list','erp.stock.list','erp.ptype.list'}:raise ValueError('read not allowed')
    rows=[];seen=set()
    with client.lock:
        for page in range(1,101):
            result=post('/allnewapi',signed(client,method,{**(params or {}),'pageno':page,'pagesize':100}))
            batch=result.get(collection) if isinstance(result,dict) else None
            if not isinstance(batch,list):raise SalesAPIError('sales_api_dictionary_invalid','ERP 基础资料返回不完整')
            for row in batch:
                identity=str(row.get('id') or row.get('ptypeid') or '')
                if not identity or identity in seen:raise SalesAPIError('sales_api_dictionary_invalid','ERP 基础资料有重复或缺失身份')
                seen.add(identity)
            rows+=batch
            if len(batch)<100:return rows
    raise SalesAPIError('sales_api_dictionary_incomplete','ERP 基础资料超出本次核对范围')


def unique(rows,name):
    matches=[r for r in rows if str(r.get('fullname','')).strip()==name
             and not r.get('deleted') and not r.get('isstop')]
    if len(matches)!=1:raise SalesAPIError('sales_api_identity_ambiguous',name+'：ERP 身份不唯一')
    return matches[0]


def preflight(client,model):
    with client.lock:token=dict(client._ready()['token'])
    if str(token.get('profileid'))!=model['profile_id']:raise SalesAPIError('sales_api_account_changed','ERP 账套不一致')
    employees=read_pages(client,'erp.employee.listall','employees')
    maker=unique(employees,'罗健树');handler=unique(employees,model['handler_name'])
    if str(maker['id'])!=str(token.get('employee_id')):raise SalesAPIError('sales_api_maker_changed','当前 API 授权制单人不是罗健树')
    customer=unique(read_pages(client,'erp.btype.list','btypes'),model['customer_name'])
    if model.get('erp_customer_id') and str(customer['id'])!=model['erp_customer_id']:
        raise SalesAPIError('sales_api_customer_changed','ERP 客户身份与选择时不一致，请核对原客户')
    warehouse=unique(read_pages(client,'erp.stock.list','stocks'),'江苏优螺')
    if str(warehouse['id'])!=model['warehouse_id']:raise SalesAPIError('sales_api_warehouse_changed','仓库身份不一致')
    codes=list(dict.fromkeys(d['product_code'] for d in model['documents']))
    products={}
    # Small, bounded chunks; dictionary reads never run inside a SQLite writer.
    for start in range(0,len(codes),50):
        rows=read_pages(client,'erp.ptype.list','ptypes',{'begintime':'1900-01-01 00:00:00',
            'endtime':'2100-01-01 00:00:00','timetype':1,'isall':True,'usercodes':codes[start:start+50]})
        for code in codes[start:start+50]:
            match=[p for p in rows if str(p.get('usercode','')).upper()==code.upper() and not p.get('deleted') and not p.get('isstop')]
            if len(match)!=1:raise SalesAPIError('sales_api_product_ambiguous',code+'：当前商品身份不唯一')
            p=match[0];skus=p.get('skus',[])
            required={d['identity']['sku_id'] for d in model['documents'] if d['product_code']==code}
            skus=[s for s in skus if str(s.get('skuid',s.get('id',''))) in required]
            if len(required)!=1 or len(skus)!=1 or not all(k in skus[0] for k in ('prop1','prop2')):
                raise SalesAPIError('sales_api_sku_ambiguous',code+'：颜色尺码或 SKU 身份待核对')
            sku={**skus[0],'skuid':skus[0].get('skuid',skus[0].get('id'))}
            products[code]={'ptypeid':p['ptypeid'],'fullname':p['fullname'],'sku':sku}
    return {'customer_id':str(customer['id']),'handler_id':str(handler['id']),
            'maker_id':str(maker['id']),'products':products}


def submit_once(client,document):
    with client.lock:
        parameters=signed(client,'erp.saleorder.add',{'saleorder':decimal_json(document)})
        request=urllib.request.Request(GATEWAY+'/allnewapi',data=urllib.parse.urlencode(parameters).encode(),method='POST',
                                      headers={'Content-Type':'application/x-www-form-urlencoded'})
        # No redirects and no retry at any layer. A timeout remains unknown.
        try:
            with urllib.request.build_opener(NoRedirect()).open(request,timeout=25) as response:
                raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:return {'status':'unknown','message':'ERP 响应过大，原尝试已冻结'}
            value=json.loads(raw)
        except Exception:
            return {'status':'unknown','message':'未取得完整 API 回执，请核对原单，禁止重新提交'}
        if not isinstance(value,dict):return {'status':'unknown','message':'ERP 回执格式不明'}
        if value.get('iserror') is True:
            message=str(value.get('message') or value.get('errormessage') or value.get('msg') or 'ERP 拒绝了本次请求')[:600]
            for field in ('appkey','token','sign'):
                message=message.replace(parameters[field],'[已隐去]')
            message=re.sub(r'(?i)(token|signkey|appsecret|authorization)\s*[:=]\s*\S+',r'\1=[已隐去]',message)
            code=value.get('errorcode',value.get('code'))
            return {'status':'rejected','code':code if isinstance(code,int) else None,'message':message,
                    'number':document['number']}
        response=value.get('response')
        identity=response.get('vchcode') if isinstance(response,dict) else None
        if value.get('iserror') is False and not isinstance(identity,bool) and str(identity).isdigit() and int(identity)>0:
            return {'status':'accepted','vchcode':str(identity),'number':document['number'],'completion_basis':'api_receipt'}
        return {'status':'unknown','message':'ERP 未返回有效单据标识，原尝试已冻结','number':document['number']}
