"""Render a frozen selection into screen PNGs and an audited CMYK merchant ZIP.

The old balanced-trim algorithm is deliberately parameterized here: measure the
visible raw artwork, center it, scale art and labels together, then check the
whole artwork rectangle and conservative label footprints against nine cuts.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageCms, ImageFilter
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

DEFAULT_PRINT_PROFILE = {
    'trim_allowance_mm': 2.0, 'corner_radius_mm': 4.0,
    'cut_offset_assumption_mm': 1.0, 'label_safe_mm': 5.0,
    'visible_margin_mm': 3.0, 'geometry_guard_mm': .12,
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pixels(size, dpi):
    return tuple(math.ceil(value * dpi / 25.4 - 1e-7) for value in size)


def print_profile(config):
    result = dict(DEFAULT_PRINT_PROFILE)
    supplied = config.get('print_profile', {})
    if not isinstance(supplied, dict) or set(supplied) - set(result):
        raise ValueError('商家印刷参数包含未知字段。')
    result.update(supplied)
    for key, value in result.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('商家印刷参数无效：' + key)
    if result['label_safe_mm'] < 5 or result['geometry_guard_mm'] < .12:
        raise ValueError('角标安全距离不得小于5毫米，几何余量不得小于0.12毫米。')
    return result


def verify_frozen_sources(folder, snapshot):
    """Only bytes recorded inside this job's frozen snapshot may be rendered."""
    root = (folder / 'snapshot' / 'sources').resolve()
    records = {}
    for row in snapshot.get('sources', {}).values():
        path = (folder / row['frozen_path']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or sha(path) != row['sha256']:
            raise ValueError('冻结原图缺失或校验失败：' + str(path))
        if 'bytes' in row and path.stat().st_size != row['bytes']:
            raise ValueError('冻结原图文件大小已改变：' + str(path))
        records[path] = row['sha256']
    for asset in snapshot['assets'].values():
        for field in ('art_source', 'name_asset'):
            if asset.get(field):
                path = Path(asset[field])
                path = (path if path.is_absolute() else folder / path).resolve()
                if path not in records:
                    raise ValueError('资产未记录在本次冻结快照：' + str(path))
                asset[field] = str(path)
    if not records:
        raise ValueError('冻结快照没有可验证的原图。')


def source_geometry(asset, settings, deck):
    x, y, w, h = deck.placement(asset, settings)
    # deck uses a bottom-left origin, while all raster geometry uses top-left.
    art = [x, settings['card_height_mm'] - y - h, w, h]
    with Image.open(asset['art_source']) as im:
        rgb = im.convert('RGBA')
        background = Image.new('RGBA', im.size, 'white')
        background.alpha_composite(rgb)
        values = np.asarray(background.convert('RGB'))
        yy, xx = np.nonzero(values.min(axis=2) < 235)
        if len(xx) < 10:
            raise ValueError('原画无法检测可见花框，请检查原图：' + asset['id'])
        box = [int(xx.min()), int(yy.min()), int(xx.max()) + 1, int(yy.max()) + 1]
        sx, sy = w / im.width, h / im.height
    visible = [x + box[0] * sx, art[1] + box[1] * sy,
               (box[2] - box[0]) * sx, (box[3] - box[1]) * sy]
    return {'art_box_mm': art, 'visible_box_mm': visible, 'bbox_px_xyxy': box,
            'center_mm': [visible[0] + visible[2] / 2, visible[1] + visible[3] / 2],
            'method': 'all source pixels with min(R,G,B)<235 after alpha on white; no content deletion'}


def label_boxes(card, asset, settings, deck):
    """Conservative footprints matching deck.draw_face's sizing expressions."""
    result = []
    def box(value):
        x, y, w, h = deck.art_box(asset, settings, value)
        return x, settings['card_height_mm'] - y - h, w, h
    for key in ('top_index_box', 'bottom_index_box'):
        x, y, w, h = box(asset['layout'][key])
        rank = card['rank']
        if rank == 'JOKER':
            size = min(9, w * .85 * mm / pdfmetrics.stringWidth('J', 'DeckBold', 1), h * .85 * mm / 5)
            iw, ih = size / mm, size * 5 / mm
        else:
            size = min(16, w * .86 * mm / pdfmetrics.stringWidth(rank, 'DeckBold', 1), h * .42 * mm / .74)
            iw = max(pdfmetrics.stringWidth(rank, 'DeckBold', size) / mm, min(4, w * .75))
            ih = size * .72 / mm + min(3.8, h * .32) + min(.6, h * .05)
        result.append({'kind': key, 'box_mm': [x + (w - iw) / 2, y + (h - ih) / 2, iw, ih]})
    for n, entry in enumerate(asset['layout']['name_boxes']):
        if entry.get('rotation_deg', 0) not in (0, 180):
            raise ValueError('名称只支持0或180度旋转。')
        x, y, w, h = box(entry['box'])
        if asset.get('name_asset'):
            with Image.open(asset['name_asset']) as im:
                iw, ih = im.size
            factor = min(w * .9 / iw, h * .9 / ih)
            iw, ih = iw * factor, ih * factor
        else:
            name = asset['display_name']
            font = 'DeckChinese' if any(ord(c) > 127 for c in name) else 'DeckSans'
            size = min(7, w * .9 * mm / pdfmetrics.stringWidth(name, font, 1), h * .82 * mm)
            iw, ih = pdfmetrics.stringWidth(name, font, size) / mm, size / mm
        result.append({'kind': 'name_' + str(n), 'box_mm': [x + (w - iw) / 2, y + (h - ih) / 2, iw, ih]})
    return result


def transform_box(box, center, scale, trim):
    x, y, w, h = box
    return [trim[0] / 2 + (x - center[0]) * scale,
            trim[1] / 2 + (y - center[1]) * scale, w * scale, h * scale]


def edge(box, trim, dx=0, dy=0):
    x, y, w, h = box
    return min(x - dx, y - dy, trim[0] + dx - x - w, trim[1] + dy - y - h)


def clearance(box, trim, radius, dx, dy):
    x, y, w, h = box
    points = np.array([(x, y), (x + w, y), (x, y + h), (x + w, y + h)])
    q = np.abs(points - np.array([trim[0] / 2 + dx, trim[1] / 2 + dy])) - (np.array(trim) / 2 - radius)
    sdf = np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(np.maximum(q[:, 0], q[:, 1]), 0) - radius
    return float(-sdf.max())


def layout_plan(geometry, labels, trim, profile):
    center = geometry['center_mm']
    visible = geometry['visible_box_mm']
    requested = min((trim[i] - profile['visible_margin_mm'] * 2) / visible[i + 2] for i in (0, 1))
    offset = profile['cut_offset_assumption_mm']
    shifts = list(itertools.product((-offset, 0., offset), repeat=2))
    def boxes(scale):
        art = transform_box(geometry['art_box_mm'], center, scale, trim)
        lab = [transform_box(v['box_mm'], center, scale, trim) for v in labels]
        return art, lab
    def valid(scale):
        art, lab = boxes(scale)
        return (all(edge(b, trim) >= profile['label_safe_mm'] for b in lab)
                and all(clearance(b, trim, profile['corner_radius_mm'], dx, dy) >= profile['geometry_guard_mm']
                        for b in [art] + lab for dx, dy in shifts))
    if requested <= 0 or not valid(0):
        raise ValueError('当前尺寸与裁切安全参数无法容纳原画。')
    lo, hi = 0., requested
    if valid(requested):
        chosen = requested
    else:
        for _ in range(45):
            mid = (lo + hi) / 2
            if valid(mid):
                lo = mid
            else:
                hi = mid
        chosen = lo
    if chosen < .25:
        raise ValueError('原图安全缩放小于25%，请调整该图排版框后重新导出。')
    art, lab = boxes(chosen)
    checks = [{'dx_mm': dx, 'dy_mm': dy,
               'minimum_rounded_clearance_mm': min(clearance(b, trim, profile['corner_radius_mm'], dx, dy) for b in [art] + lab)}
              for dx, dy in shifts]
    return {'scale': chosen, 'requested_scale': requested, 'safety_limited': chosen < requested - 1e-8,
            'source_geometry': geometry, 'source_label_boxes_mm': labels,
            'art_box_mm': art, 'label_boxes_mm': lab,
            'visible_box_mm': transform_box(visible, center, chosen, trim),
            'nominal_label_min_edge_mm': min(edge(b, trim) for b in lab), 'cut_checks': checks}


def render_front(trim_image, plan, trim, upload, dpi):
    """Same equal-axis affine transform as balanced-trim, with edge padding."""
    high_dpi = dpi * 2
    high = tuple(n * 2 for n in pixels(upload, dpi))
    center = plan['source_geometry']['center_mm']
    cx = trim_image.width / 2 + (center[0] - trim[0] / 2) * high_dpi / 25.4
    cy = trim_image.height / 2 + (center[1] - trim[1] / 2) * high_dpi / 25.4
    k = plan['scale']
    # Pad only the part reached by this inverse transform, including its filter.
    pad = math.ceil(max(0, high[0] / (2 * k) - cx, cx + high[0] / (2 * k) - trim_image.width,
                        high[1] / (2 * k) - cy, cy + high[1] / (2 * k) - trim_image.height)) + 4
    source = np.pad(np.asarray(trim_image.convert('RGB')), ((pad, pad), (pad, pad), (0, 0)), mode='edge')
    result = Image.fromarray(source).transform(high, Image.Transform.AFFINE,
        (1 / k, 0, pad + cx - high[0] / (2 * k), 0, 1 / k, pad + cy - high[1] / (2 * k)),
        resample=Image.Resampling.BICUBIC)
    return result.resize(pixels(upload, dpi), Image.Resampling.LANCZOS)


def check_raster_geometry(plan, trim, upload, dpi, profile):
    """Outward-rounded protected boxes must survive every assumed R4 cut."""
    width, height = pixels(upload, dpi)
    ppm = dpi / 25.4
    critical = np.zeros((height, width), dtype=bool)
    for x, y, w, h in [plan['art_box_mm']] + plan['label_boxes_mm']:
        l, t = math.floor((x - trim[0] / 2) * ppm + width / 2), math.floor((y - trim[1] / 2) * ppm + height / 2)
        r, b = math.ceil((x + w - trim[0] / 2) * ppm + width / 2), math.ceil((y + h - trim[1] / 2) * ppm + height / 2)
        if min(l, t) < 0 or r > width or b > height:
            raise ValueError('保护内容超出交稿范围。')
        critical[t:b, l:r] = True
    yy, xx = np.mgrid[:height, :width]
    radius = profile['corner_radius_mm']
    for row in plan['cut_checks']:
        x = (xx + .5 - width / 2) / ppm - row['dx_mm']
        y = (yy + .5 - height / 2) / ppm - row['dy_mm']
        qx = np.maximum(np.abs(x) - (trim[0] / 2 - radius), 0)
        qy = np.maximum(np.abs(y) - (trim[1] / 2 - radius), 0)
        inside = qx * qx + qy * qy <= radius * radius
        removed = int(np.count_nonzero(critical & ~inside))
        row['protected_box_pixels_removed'] = removed
        if removed:
            raise ValueError('圆角或假设偏刀会裁掉保护内容，请调整排版。')


def _profile_path():
    resources = Path(__file__).resolve().parents[1] / 'resources'
    for path in (resources / 'PSOcoated_v3.icc', resources / 'color' / 'PSOcoated_v3.icc'):
        if path.is_file():
            return path
    raise FileNotFoundError('缺少工具资源 resources/PSOcoated_v3.icc，无法生成真实CMYK商家包。')


def trim_source(asset, target_size):
    """Remove the source's original bleed before applying this job's allowance."""
    with Image.open(asset['art_source']) as source:
        source = source.convert('RGBA')
        picture = Image.new('RGBA', source.size, 'white')
        picture.alpha_composite(source)
        picture = picture.convert('RGB')
    bleed = asset.get('source_bleed_mm', 3 if asset.get('has_bleed') else 0)
    source_trim = asset.get('source_trim_mm', [63, 88] if asset.get('kind') == 'back' else target_size)
    if (type(bleed) not in (int, float) or not math.isfinite(bleed) or bleed < 0
            or not isinstance(source_trim, (list, tuple)) or len(source_trim) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in source_trim)):
        raise ValueError('原图出血或裁切尺寸无效：' + asset['id'])
    crop = [0, 0, picture.width, picture.height]
    if bleed:
        bx = picture.width * bleed / (source_trim[0] + 2 * bleed)
        by = picture.height * bleed / (source_trim[1] + 2 * bleed)
        crop = [round(bx), round(by), round(picture.width - bx), round(picture.height - by)]
        picture = picture.crop(crop)
    return picture, {'source_bleed_mm': bleed, 'source_trim_mm': list(source_trim),
                     'source_crop_px_xyxy': crop, 'source_bleed_defaulted': 'source_bleed_mm' not in asset}


def back_master(trim_image, asset, trim, upload, dpi, profile):
    safety = asset.get('back_safety', {})
    method = safety.get('method', 'v7-gold-mask' if asset['id'] == 'back-v7' else 'edge-background')
    scale = safety.get('scale', .96)
    if type(scale) not in (int, float) or not 0 < scale <= 1:
        raise ValueError('牌背安全缩放无效。')
    picture = render_front(trim_image, {'scale': scale, 'source_geometry': {'center_mm': [v / 2 for v in trim]}}, trim, upload, dpi)
    values = np.asarray(picture).astype(np.int16)
    if method == 'v7-gold-mask':
        protected = (values[:, :, 0] > 25) | (values[:, :, 1] > 90) | (values[:, :, 2] > 100)
        rule = 'R>25 OR G>90 OR B>100; validated V7 dark-blue/gold artwork only'
    elif method == 'edge-background':
        # New backs must have a uniform expendable edge. Otherwise the mask
        # reaches a cut and export fails, requiring an explicit content box.
        corners = np.array([values[0, 0], values[0, -1], values[-1, 0], values[-1, -1]])
        background = np.median(corners, axis=0)
        if np.abs(corners - background).max() > 16:
            raise ValueError('牌背边缘背景不一致，请提供back_safety保护内容框。')
        protected = np.max(np.abs(values - background), axis=2) > 24
        rule = 'RGB max-channel distance >24 from median of four matching corners; edge spread <=16'
    elif method == 'protected-box':
        box = safety.get('content_box')
        if not (isinstance(box, list) and len(box) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in box)
                and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
            raise ValueError('牌背需提供有效back_safety.content_box。')
        protected = np.zeros(values.shape[:2], dtype=bool)
        placed = transform_box([box[0] * trim[0], box[1] * trim[1], (box[2] - box[0]) * trim[0], (box[3] - box[1]) * trim[1]], [v / 2 for v in trim], scale, trim)
        x, y, w, h = placed
        ppm = dpi / 25.4
        l, t = math.floor((x - trim[0] / 2) * ppm + picture.width / 2), math.floor((y - trim[1] / 2) * ppm + picture.height / 2)
        r, bottom = math.ceil((x + w - trim[0] / 2) * ppm + picture.width / 2), math.ceil((y + h - trim[1] / 2) * ppm + picture.height / 2)
        protected[t:bottom, l:r] = True
        rule = 'explicit normalized content_box in the source trim; whole box protected'
    else:
        raise ValueError('未知牌背保护规则：' + str(method))
    guard_mm = safety.get('mask_guard_mm', .25)
    if type(guard_mm) not in (int, float) or not math.isfinite(guard_mm) or guard_mm < .25:
        raise ValueError('牌背保护膨胀距离不得小于0.25毫米。')
    guard = math.ceil(guard_mm * dpi / 25.4)
    protected = np.asarray(Image.fromarray(protected.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(guard * 2 + 1))) > 0
    yy, xx = np.nonzero(protected)
    if len(xx) < 10:
        raise ValueError('牌背未检测到可保护内容，请检查原图及保护规则。')
    rows = []
    gy, gx = np.mgrid[:picture.height, :picture.width]
    radius = profile['corner_radius_mm']
    offset = profile['cut_offset_assumption_mm']
    for dx, dy in itertools.product((-offset, 0., offset), repeat=2):
        x = (gx + .5 - picture.width / 2) / (dpi / 25.4) - dx
        y = (gy + .5 - picture.height / 2) / (dpi / 25.4) - dy
        qx = np.maximum(np.abs(x) - (trim[0] / 2 - radius), 0)
        qy = np.maximum(np.abs(y) - (trim[1] / 2 - radius), 0)
        removed = int(np.count_nonzero(protected & ~(qx * qx + qy * qy <= radius * radius)))
        if removed:
            raise ValueError(f'牌背保护内容被R4/假设偏刀裁掉：{dx:g},{dy:g}mm；请调整牌背保护区域。')
        rows.append({'dx_mm': dx, 'dy_mm': dy, 'protected_pixels_removed': removed})
    return picture, {'scale': scale, 'method': method, 'mask_rule': rule, 'mask_guard_mm': guard_mm,
        'mask_guard_px': guard, 'guarded_content_bbox_px': [int(xx.min()), int(yy.min()), int(xx.max()) + 1, int(yy.max()) + 1], 'cut_checks': rows}


def preview_merchant_png(asset, rank, suit, settings):
    """Return an upload-sized screen preview using the formal export layout."""
    import deck
    settings = copy.deepcopy(settings)
    formal_dpi = settings['dpi']
    profile = print_profile({'print_profile': settings.pop('print_profile', {})})
    profile['geometry_guard_mm'] = max(profile['geometry_guard_mm'], math.sqrt(2) * 25.4 / formal_dpi + .01)
    trim = tuple(settings[k] for k in ('card_width_mm', 'card_height_mm'))
    upload = tuple(v + 2 * profile['trim_allowance_mm'] for v in trim)
    with deck.RENDER_LOCK, tempfile.TemporaryDirectory(prefix='merchant-preview-') as temporary:
        deck.register_fonts()
        settings.update(bleed_mm=0, safe_mm=0)
        plan = layout_plan(source_geometry(asset, settings, deck), label_boxes({'rank': rank}, asset, settings, deck), trim, profile)
        check_raster_geometry(plan, trim, upload, formal_dpi, profile)
        pdf = Path(temporary) / 'preview.pdf'
        page = canvas.Canvas(str(pdf), pagesize=tuple(v * mm for v in trim))
        deck.draw_face(page, asset, rank, suit, settings, bleed=False)
        page.showPage(); page.save()
        document = pdfium.PdfDocument(pdf)
        pdf_page = document[0]
        bitmap = pdf_page.render(scale=200 / 72)
        try:
            picture = render_front(bitmap.to_pil().convert('RGB'), plan, trim, upload, 100)
        finally:
            bitmap.close(); pdf_page.close(); document.close()
        output = BytesIO()
        picture.save(output, format='PNG', dpi=(100, 100))
        return output.getvalue()


def build_merchant_export(folder, snapshot, progress=None):
    import deck  # Runtime import avoids a deck -> merchant_export import cycle.
    folder = Path(folder).resolve()
    progress = progress or (lambda percent, message: None)
    snapshot = copy.deepcopy(snapshot)
    verify_frozen_sources(folder, snapshot)
    config, assets = snapshot['config'], snapshot['assets']
    settings = copy.deepcopy(config['settings'])
    trim = tuple(float(settings[k]) for k in ('card_width_mm', 'card_height_mm'))
    dpi = settings['dpi']
    if (type(dpi) not in (int, float) or not math.isfinite(dpi) or not 150 <= dpi <= 600
            or any(not math.isfinite(v) or v <= 0 for v in trim)):
        raise ValueError('卡牌尺寸或DPI无效。')
    profile = print_profile(config)
    # A lower raster DPI needs a wider analytic guard for outward pixel rounding.
    profile['geometry_guard_mm'] = max(profile['geometry_guard_mm'], math.sqrt(2) * 25.4 / dpi + .01)
    if profile['corner_radius_mm'] > min(trim) / 2:
        raise ValueError('圆角半径大于卡牌半边长度。')
    b = profile['trim_allowance_mm']
    upload = tuple(v + 2 * b for v in trim)
    # Render original labels before the shared affine transform; final safety is
    # established analytically and again at raster pixel level below.
    settings.update(bleed_mm=0, safe_mm=0)
    cards = deck.cards_for(config)
    if len(cards) != 54 or len({v['id'] for v in cards}) != 54:
        raise ValueError('冻结配置必须生成54张不同正面。')
    used = {c['asset_id'] for c in cards} | {config['back']} | set(config['box'].values())
    if None in used or not used.issubset(assets):
        raise ValueError('冻结配置缺少所选资产，已阻止导出。')
    icc_path = _profile_path()
    icc_bytes = icc_path.read_bytes()
    target_profile = ImageCms.getOpenProfile(str(icc_path))
    if (target_profile.profile.xcolor_space.strip() != 'CMYK'
            or ImageCms.getProfileDescription(target_profile).strip() != 'PSO Coated v3'):
        raise ValueError('目标ICC不是PSO Coated v3 CMYK配置。')
    rgb_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
    transform = ImageCms.buildTransformFromOpenProfiles(rgb_profile, target_profile, 'RGB', 'CMYK',
        renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
    package, screen = folder / 'package', folder / 'screen_png'
    package.mkdir(exist_ok=False)
    screen.mkdir(exist_ok=False)
    for name in ('01-卡牌正面', '02-卡牌反面', '03-牌盒'):
        (package / name).mkdir()
    rows, plans, geometry_cache, back_checks, source_crop_checks = [], [], {}, None, []
    progress(1, '正在从本次冻结选图生成61份屏幕稿及CMYK商家稿')
    with deck.RENDER_LOCK, tempfile.TemporaryDirectory(prefix='deck-merchant-') as temporary:
        deck.register_fonts()
        pdf = Path(temporary) / 'render.pdf'
        page = canvas.Canvas(str(pdf), pagesize=(trim[0] * mm, trim[1] * mm), pageCompression=1)
        specs = []
        for card in cards:
            asset = assets[card['asset_id']]
            if card['asset_id'] not in geometry_cache:
                geometry_cache[card['asset_id']] = source_geometry(asset, settings, deck)
            plan = layout_plan(geometry_cache[card['asset_id']], label_boxes(card, asset, settings, deck), trim, profile)
            check_raster_geometry(plan, trim, upload, dpi, profile)
            plan.update(file=card['id'] + '.png', asset_id=card['asset_id'])
            plans.append(plan)
            page.setPageSize((trim[0] * mm, trim[1] * mm))
            deck.draw_face(page, asset, card['rank'], card['suit'], settings, bleed=False)
            page.showPage()
            specs.append({'stem': card['id'], 'group': '01-卡牌正面', 'kind': 'front', 'trim_mm': list(trim), 'asset_id': card['asset_id'], 'plan': plan})
        faces = [('55-牌背', '02-卡牌反面', config['back'], trim)]
        bw, bh, bd = (config['settings'][k] for k in ('box_width_mm', 'box_height_mm', 'box_depth_mm'))
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (bw, bh, bd)):
            raise ValueError('牌盒尺寸无效。')
        sizes = {'front': (bw, bh), 'back': (bw, bh), 'left': (bd, bh), 'right': (bd, bh), 'top': (bw, bd), 'bottom': (bw, bd)}
        faces += [('牌盒-' + deck.BOX_NAMES[face], '03-牌盒', config['box'][face], sizes[face]) for face in deck.BOX_FACES]
        for stem, group, asset_id, size in faces:
            page.setPageSize(((size[0] + 2 * b) * mm, (size[1] + 2 * b) * mm))
            picture, crop_check = trim_source(assets[asset_id], size)
            normalized_source = Path(temporary) / ('normalized-source-' + str(len(specs)) + '.png')
            picture.save(normalized_source)
            normalized_asset = {**assets[asset_id], 'art_source': str(normalized_source), 'has_bleed': False}
            is_back = group == '02-卡牌反面'
            if is_back:
                page.setPageSize((size[0] * mm, size[1] * mm))
            deck.draw_image(page, normalized_asset, size[0], size[1], 0 if is_back else b)
            page.showPage()
            source_crop_checks.append({'asset_id': asset_id, **crop_check})
            specs.append({'stem': stem, 'group': group, 'kind': 'back' if is_back else 'box', 'trim_mm': list(size), 'asset_id': asset_id})
        page.save()
        doc = pdfium.PdfDocument(pdf)
        try:
            if len(doc) != 61:
                raise ValueError('内部渲染页数不是61。')
            for n, spec in enumerate(specs):
                pdf_page = doc[n]
                bitmap = pdf_page.render(scale=dpi * 2 / 72)
                try:
                    picture = bitmap.to_pil().convert('RGB')
                finally:
                    bitmap.close()
                    pdf_page.close()
                size = tuple(v + 2 * b for v in spec['trim_mm'])
                if spec['kind'] == 'front':
                    picture = render_front(picture, spec['plan'], trim, upload, dpi)
                elif spec['kind'] == 'back':
                    picture, back_checks = back_master(picture, assets[spec['asset_id']], trim, upload, dpi, profile)
                else:
                    picture = picture.resize(pixels(size, dpi), Image.Resampling.LANCZOS)
                png = screen / (spec['stem'] + '.png')
                jpg = package / spec['group'] / (spec['stem'] + '.jpg')
                picture.save(png, dpi=(dpi, dpi), icc_profile=rgb_profile.tobytes())
                converted = ImageCms.applyTransform(picture, transform)
                converted.save(jpg, format='JPEG', quality=100, subsampling=0, optimize=True,
                               dpi=(dpi, dpi), icc_profile=icc_bytes)
                with Image.open(png) as check:
                    if check.mode != 'RGB' or check.size != pixels(size, dpi) or check.info.get('icc_profile') != rgb_profile.tobytes():
                        raise ValueError('屏幕PNG校验失败：' + png.name)
                    if any(abs(v - dpi) > .02 for v in check.info['dpi']):
                        raise ValueError('屏幕PNG DPI校验失败。')
                with Image.open(jpg) as check:
                    check.load()
                    if (check.mode != 'CMYK' or check.size != pixels(size, dpi)
                            or check.info.get('icc_profile') != icc_bytes or any(abs(v - dpi) > 1 for v in check.info['dpi'])):
                        raise ValueError('CMYK JPG元数据校验失败：' + jpg.name)
                    error = float(np.abs(np.asarray(check).astype(np.int16) - np.asarray(converted).astype(np.int16)).mean())
                    if error >= 1:
                        raise ValueError('JPG压缩误差过大：' + jpg.name)
                rows.append({'file': jpg.relative_to(package).as_posix(), 'screen_file': png.relative_to(folder).as_posix(),
                    'asset_id': spec['asset_id'], 'kind': spec['kind'], 'trim_mm': spec['trim_mm'], 'upload_mm': list(size),
                    'pixels': list(pixels(size, dpi)), 'dpi': dpi, 'mode': 'CMYK', 'icc_embedded': True,
                    'sha256': sha(jpg), 'screen_sha256': sha(png), 'mean_codec_error_0_255': error})
                progress(5 + int((n + 1) / 61 * 88), f'已生成并校验 {n + 1}/61 份PNG和CMYK JPG')
        finally:
            doc.close()
    readme = (f'AI娘扑克商家印刷说明\n54张正面＋统一牌背1张＋牌盒独立六面，共61张CMYK JPG。\n'
        f'卡牌上传{upload[0]:g}×{upload[1]:g}毫米，每边裁{b:g}毫米，成品{trim[0]:g}×{trim[1]:g}毫米；{dpi:g} DPI。\n'
        '54张正面各印1张；每张正面的反面均配同一份55-牌背，包括大小王，共54张牌。\n'
        '正面与牌背按文件上边缘对齐做同向双面，先确认一张实物正反样再整副生产；牌背整体未经严格180度对称认证。\n'
        f'矩形交稿，圆角R{profile["corner_radius_mm"]:g}由印厂裁切，不要再次缩放或抠圆角。\n'
        f'角标及名称距裁切线至少{profile["label_safe_mm"]:g}毫米；偏刀±{profile["cut_offset_assumption_mm"]:g}毫米仅为设计检验假设，请印厂确认。\n'
        f'牌盒宽{bw:g}×高{bh:g}×厚{bd:g}毫米，六面尺寸见文件实际毫米尺寸；六面均含每边{b:g}毫米余量。刀模、插舌、粘口、纸厚请印厂确认。\n'
        f'厚度设计假设：单卡0.30毫米，54张叠厚16.2毫米，当前盒厚预留{bd-16.2:g}毫米；纸厚、涂层、覆膜与压缩由印厂复核。\n'
        'JPG已嵌入PSO Coated v3 ICC，颜色以实物打样为准。包内无PDF。\n')
    (package / '给商家的说明.txt').write_text(readme, encoding='utf-8-sig')
    contents = sorted(p for p in package.rglob('*') if p.is_file())
    if len(rows) != 61 or len(contents) != 62 or sum(p.suffix == '.jpg' for p in contents) != 61:
        raise ValueError('商家包数量校验失败。')
    archive_path = folder / 'ai-girls-deck.zip'
    temporary_archive = archive_path.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary_archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        for path in contents:
            archive.write(path, path.relative_to(package).as_posix())
    with zipfile.ZipFile(temporary_archive) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != 62:
            raise ValueError('商家ZIP完整性校验失败。')
        for path in contents:
            if hashlib.sha256(archive.read(path.relative_to(package).as_posix())).hexdigest() != sha(path):
                raise ValueError('商家ZIP内容校验失败。')
    screen_archive = folder / 'ai-girls-screen.zip'
    with zipfile.ZipFile(screen_archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as archive:
        for path in sorted(screen.glob('*.png')):
            archive.write(path, path.name)
    with zipfile.ZipFile(screen_archive) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != 61:
            raise ValueError('屏幕PNG包完整性校验失败。')
    # Recheck the frozen bytes before publishing a successful archive.
    verify_frozen_sources(folder, snapshot)
    report = {'schema_version': 1, 'status': 'passed', 'front_count': 54, 'back_count': 1, 'box_count': 6,
        'png_count': 61, 'jpg_count': 61, 'pdf_count_in_archive': 0, 'config': config,
        'selection': snapshot.get('selection'), 'frozen_at': snapshot.get('frozen_at'), 'sources': snapshot['sources'],
        'print_profile': profile, 'card_trim_mm': list(trim), 'card_upload_mm': list(upload), 'dpi': dpi,
        'icc': {'description': ImageCms.getProfileDescription(target_profile).strip(), 'sha256': sha(icc_path)},
        'rendering_intent': 'perceptual', 'black_point_compensation': True, 'jpeg_quality': 100,
        'frozen_sources_verified_before_and_after_render': True,
        'color_assumption': 'PSO Coated v3 is a coated-paper profile; final printer/paper match requires a physical proof.',
        'cut_check_scope': 'front art rectangle/label footprints and back protected-content mask; nine translated R4 cuts, no rotation/size-error model; box artwork not semantically checked',
        'back_rotation_applied_deg': 0, 'box_back_rotation_applied_deg': 0,
        'front_layout_checks': plans, 'back_checks': back_checks, 'source_crop_checks': source_crop_checks,
        'files': rows, 'archive_sha256': sha(temporary_archive), 'screen_archive_sha256': sha(screen_archive)}
    (folder / 'export-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (folder / '配置快照.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary_archive.replace(archive_path)
    summary = {key: report[key] for key in ('front_count', 'back_count', 'box_count', 'png_count', 'jpg_count', 'print_profile', 'card_trim_mm', 'card_upload_mm', 'dpi')}
    summary.update(archive_sha256=report['archive_sha256'], report='export-report.json', screen_directory='screen_png')
    summary['back_safety'] = {key: back_checks[key] for key in ('method', 'scale', 'mask_guard_mm')}
    progress(100, '61份屏幕PNG和真实CMYK JPG商家包已通过校验')
    return {'archive': archive_path, 'screen_archive': screen_archive, 'summary': summary}
