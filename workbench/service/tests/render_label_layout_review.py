"""Offline review artifact from CPCL commands. Never a firmware/printed proof."""
import importlib.util
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import qrcode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import mobile_cpcl as current

OUTPUT = ROOT / 'runtime' / 'label-layout-review'
ORDER = dict(name='HEX BOLT 六角螺栓', orderNo='PO20260910001234', projectNo='JOB-2026-090',
             spec='DIN933  M12*40', grade='8.8', quantity='1200', totalPackages='12',
             packageQuantity='100', lineNo='12', drawingNo='DRW-120034', surface='镀蓝白锌',
             sku='LS0012034', qrValue='SHIP-120034')
INVENTORY = dict(productName='内六角圆柱头螺钉 SOCKET CAP SCREW', spec='M12*100', model='12.9级发黑',
                 material='合金钢', location='A-12-08', standardNo='DIN912', sku='LS0012034', qrValue='LS0012034')


def font(size, bold=False):
    return ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc', size)


def draw_label(raw):
    paper = Image.new('RGB', (800, 400), 'white')
    draw = ImageDraw.Draw(paper)
    latin = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 22)
    chinese = font(22)
    bold, mx, my = 0, 1, 1
    lines = raw.decode('gb18030').split('\r\n')
    for index, line in enumerate(lines):
        if line.startswith('! U1 SETBOLD '): bold = int(line.split()[-1])
        elif line.startswith('SETMAG '): mx, my = map(int, line.split()[1:])
        elif line.startswith('BOX '):
            x1, y1, x2, y2, width = map(int, line.split()[1:])
            draw.rectangle((800-y2, x1, 800-y1, x2), outline='black', width=width)
        elif line.startswith('VTEXT 55 0 '):
            _, _, _, px, py, value = line.split(' ', 5)
            x, y = 800-int(py), int(px)
            for char in value:
                cell_width = 12 if ord(char) < 128 else 24
                glyph = Image.new('L', (cell_width+bold, 24+bold), 0)
                ImageDraw.Draw(glyph).text((0, 20), char, font=latin if ord(char)<128 else chinese,
                                          anchor='ls', fill=255, stroke_width=bold//2)
                glyph = glyph.resize((glyph.width*mx, glyph.height*my), Image.Resampling.NEAREST)
                paper.paste((0, 0, 0), (x, y), glyph)
                x += cell_width*mx
        elif line.startswith('VB QR '):
            parts = line.split()
            px, py, unit = int(parts[2]), int(parts[3]), int(parts[-1])
            content = lines[index+1][3:]
            code = qrcode.QRCode(box_size=unit, border=4)
            code.add_data(content)
            code.make(fit=True)
            qr_image = code.make_image(fill_color='black', back_color='white').convert('RGB')
            # The native QR command and payload are identical before and after.
            paper.paste(qr_image, (800-py, px))
    return paper


def main():
    OUTPUT.mkdir(exist_ok=True)
    source = OUTPUT / 'mobile_cpcl.before.py'
    spec = importlib.util.spec_from_file_location('core._cpcl_review_before', source)
    previous = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(previous)
    canvas = Image.new('RGB', (1744, 1190), '#f3f3f1')
    draw = ImageDraw.Draw(canvas)
    draw.text((32, 24), '标签排版对照  /  50 × 100 mm', font=font(32, True), fill='#151515')
    draw.text((32, 76), '按 CPCL 坐标绘制的示意图 · 桌面替代字形 · 非 A300E 实际出纸', font=font(19), fill='#606060')
    metadata = {'note': 'Offline layout illustration; substitute desktop glyphs, not a printed proof.', 'examples': {}}
    for row, (kind, fields) in enumerate((('order', ORDER), ('inventory', INVENTORY))):
        top = 160 + row * 490
        for col, (label, module) in enumerate((('调整前', previous), ('调整后', current))):
            x = 32 + col * 880
            draw.text((x, top-44), f'{"订单标签" if kind=="order" else "库存标签"} · {label}', font=font(25, True), fill='#161616')
            raw = getattr(module, kind+'_cpcl')(fields)
            image = draw_label(raw)
            canvas.paste(image, (x, top))
            draw.rectangle((x-1, top-1, x+800, top+400), outline='#ccccca', width=1)
            filename = f'{kind}-{"before" if col == 0 else "after"}'
            image.save(OUTPUT / (filename+'.png'))
            (OUTPUT / (filename+'.cpcl')).write_bytes(raw)
            metadata['examples'][filename] = {'bytes': len(raw), 'qrValue': fields['qrValue']}
    draw.text((32, 1090), '保留：二维码内容与位置、纸张、字段分区、数量及标准/规格空格。', font=font(21), fill='#353535')
    draw.text((32, 1128), '调整：标题对齐、正文减粗、分级字号、长文字完整适配；实物字重和字宽仍需手机试打一张确认。', font=font(20), fill='#606060')
    canvas.save(OUTPUT / 'label-layout-comparison.png')
    (OUTPUT / 'review.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(OUTPUT / 'label-layout-comparison.png')


if __name__ == '__main__':
    main()
