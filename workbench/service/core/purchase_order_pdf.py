"""Purchase order layout based on the user's supplied two-page company template."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

ASSETS = Path(__file__).resolve().parents[1] / 'assets'


def render_order(path, snapshot, attachments):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image, KeepTogether
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader

    if 'ULChinese' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('ULChinese', str(ASSETS / 'fonts/NotoSansSC.ttf')))
    normal = ParagraphStyle('order', fontName='ULChinese', fontSize=9, leading=14, wordWrap='CJK')
    small = ParagraphStyle('cell', parent=normal, fontSize=7.5, leading=10.5)
    heading = ParagraphStyle('heading', parent=normal, fontSize=20, leading=25)
    section = ParagraphStyle('section', parent=normal, fontSize=14, leading=21, spaceAfter=10)
    p = lambda value, style=normal: Paragraph(escape(str(value or '')).replace('\n', '<br/>'), style)
    width = A4[0] - 56
    buyer = snapshot['buyer']
    title = Table([[p('PURCHASE ORDER\n采购订单\n' + snapshot['order_number'], heading),
                    Image(str(ASSETS / 'templates/purchase-logo.jpg'), width=88, height=46)]], colWidths=[width-102,102])
    title.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    header = Table([
        [p('Order date / 订单日期：' + snapshot['order_date']), p('Supplier / 供货单位：' + snapshot['supplier_name'])],
        [p('Business contact / 业务联系人：\n' + buyer['business_contact'] + '  ' + buyer['business_phone']), p('Delivery date / 交货日期：\n' + snapshot['delivery_date'])],
        [p('Receiver / 仓库收货人：\n' + buyer['receiver'] + '  ' + buyer['receiver_phone']), p('Additional instructions / 附加说明：\n' + snapshot.get('supplier_note',''))],
    ], colWidths=[width*.51,width*.49])
    header.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
    story = [title, Spacer(1,10), header, Spacer(1,5),p('Shipping address / 发票及送货地址：\n' + buyer['shipping_address']),Spacer(1,10)]
    columns = ['行号','产品编号','产品名称','规格','材质','型号','数量','税后单价','税后金额','备注']
    rows = [[p(v,small) for v in columns]]
    for n,x in enumerate(snapshot['lines'],1):
        d=x['details']
        note='交期 '+d['delivery_date'] if d.get('delivery_date') else ''
        if x.get('kind')=='非标件': note += ('\n' if note else '')+'图纸见附件'
        rows.append([p(v,small) for v in [n,x['sku'],x['product_name'],x['spec'],x.get('material',''),x['model'],x['quantity'],x['tax_unit_price'],x['tax_amount'],note]])
    widths=[24,48,110,60,32,58,34,49,53,71]
    widths[-1]+=width-sum(widths)
    table=Table(rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
    table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.6,HexColor('#222222')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4),('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
    story += [table,Spacer(1,12),p('TOTAL (WITHIN VAT) / 含税合计：人民币 '+snapshot['tax_total']+' 元'),p('此单据请随货。'),PageBreak(),p('TERMS AND CONDITIONS\n采购条款',section)]
    # Terms and company data are frozen with the order. Never reuse example supplier rows.
    for term in buyer['terms'].split('\n'):
        if term.strip():story.extend([p(term),Spacer(1,6)])
    seller = [p('SELLER / 卖方',section),p(snapshot['supplier_name']),p('地址：'),p('电话：'),p('税号：'),p('开户行：'),p('账号：'),p('委托代理人：')]
    buyer_box = [p('BUYER / 买方',section),p(buyer['company']),p('地址：'+buyer['registered_address']),p('电话：'+buyer['company_phone']),p('税号：'+buyer['tax_number']),p('开户行：'+buyer['bank']),p('账号：'+buyer['bank_account']),p('委托代理人：'+buyer['agent'])]
    signatures=Table([[seller,buyer_box]],colWidths=[width/2,width/2])
    signatures.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),12)]))
    story += [Spacer(1,20),signatures]
    for n,(item,data) in enumerate(attachments,1):
        story += [PageBreak(),p('非标件图纸附件 '+str(n)+' · '+item['sku'],section),p(item['product_name']),p('规格：'+item['spec']+'   型号：'+item['model']),p('对应采购单：'+snapshot['order_number']),Spacer(1,12)]
        w,h=ImageReader(BytesIO(data)).getSize();factor=min(width/w,625/h)
        story.append(Image(BytesIO(data),width=w*factor,height=h*factor))
    def footer(c,doc):
        c.setFont('ULChinese',7);c.setFillColor(HexColor('#777777'))
        c.drawString(28,23,snapshot['order_number']);c.drawRightString(A4[0]-28,23,'第 '+str(doc.page)+' 页')
    SimpleDocTemplate(str(path),pagesize=A4,leftMargin=28,rightMargin=28,topMargin=30,bottomMargin=40).build(story,onFirstPage=footer,onLaterPages=footer)
