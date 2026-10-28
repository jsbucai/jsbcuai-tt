"""Exact, owner-bound business acknowledgements; never an arbitrary command."""
import re

def ingest(channel,message,fallback):
    text=''.join(item.get('text_item',{}).get('text','') for item in message.get('item_list',[]) if item.get('type')==1).strip()
    if text.startswith(('确认事项','暂不处理')):
        match=re.fullmatch(r'(确认事项|暂不处理)\s+([0-9A-Fa-f]{10})',text)
        if channel:
            spool,binding=channel
            if match:spool.confirm_business(binding,match[2].upper(),match[1]=='确认事项',str(message.get('message_id','')))
            else:spool.reply('business-format-'+str(message.get('message_id','')),binding,'确认格式：确认事项 确认编号；暂不处理请回复：暂不处理 确认编号。')
        return
    if fallback:fallback(message)
