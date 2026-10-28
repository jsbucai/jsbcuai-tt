"""Credential-isolated rule comparison. Expected answers are never in the prompt."""
import argparse,json,os
from pathlib import Path
from sales_app_server import invoke
from sales_codex_runner import _environment,DISABLED_FEATURES,_atomic_json
SCHEMA={'type':'object','additionalProperties':False,'required':['product_codes','needs_clarification'],
 'properties':{'product_codes':{'type':'array','items':{'type':'string'}},'needs_clarification':{'type':'boolean'}}}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--job-dir',type=Path,required=True);p.add_argument('--codex-binary',type=Path,required=True);a=p.parse_args()
 data=json.loads((a.job_dir/'input.json').read_text());results=[]
 for supplement in data['supplements']:
  prompt=data['base_rules']+'\nOWNER RULE SUPPLEMENT\n'+supplement+'\nAnalyze this sample. '+\
    'Return only product_codes in original row order and needs_clarification. Never invent a code. '+\
    'No order is submitted. Original evidence:\n'+data['problem']+'\nAvailable catalog evidence:\n'+json.dumps(data['catalog'],ensure_ascii=False)
  result,stats=invoke(a.codex_binary,a.job_dir,prompt,SCHEMA,[],_environment(),DISABLED_FEATURES,timeout=150)
  results.append({'result':result,'stats':stats})
 _atomic_json(a.job_dir/'result.json',{'comparisons':results})
