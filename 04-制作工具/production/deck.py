"""Independent production registry, versioned configuration, and frozen export queue."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import queue
import re
import struct
import threading
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
import pypdfium2 as pdfium
from runtime_paths import PROJECT, LIBRARY, EXPORTS, RESOURCES, project_path, pid_alive
from library import Library
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONTS = {'DeckSans': RESOURCES / 'NotoSansSC-Regular.ttf', 'DeckBold': RESOURCES / 'NotoSansSC-Bold.ttf', 'DeckChinese': RESOURCES / 'NotoSansSC-Regular.ttf'}
FAMILIES = [('A','llama','Llama'),('2','hunyuan','混元'),('3','minimax','MiniMax'),('4','mistral','Mistral'),('5','ernie','文心一言'),('6','qwen','Qwen'),('7','glm','GLM'),('8','kimi','Kimi'),('9','grok','Grok'),('10','deepseek','DeepSeek'),('J','gemini','Gemini'),('Q','gpt','GPT'),('K','claude','Claude'),('JOKER','doubao','豆包')]
SUITS = [('SPADES','black'),('HEARTS','red'),('CLUBS','black'),('DIAMONDS','red')]
BOX_FACES = ['front','back','left','right','top','bottom']
BOX_NAMES = dict(zip(BOX_FACES,['正面','背面','左侧','右侧','顶面','底面']))
MAPPING_VERSION = 'v3-grok9-geminiJ-20261019'
JOB_ID = re.compile(r'^[0-9]{8}T[0-9]{6}-[0-9a-f]{10}$')
RENDER_LOCK = threading.RLock()

def now():
    return datetime.now(timezone.utc).isoformat()

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def resolve_path(value):
    return project_path(value)

def manifest_assets(data):
    for item in data.get('assets',data.get('candidates',[])):
        item=copy.deepcopy(item)
        item.setdefault('coordinate_system',data.get('coordinate_system','normalized_xyxy'))
        yield item

class ConfigConflict(ValueError):
    def __init__(self, current):
        super().__init__('制作配置已被其他页面更新，请重新读取后保存。')
        self.current = current

class ValidationError(ValueError):
    def __init__(self, errors):
        super().__init__('；'.join(errors))
        self.errors = errors

class Registry:
    """Only trusted local manifests add paths; browser requests only reference IDs."""
    def __init__(self, ui_root=None, external=None):
        self.ui_root = Path(ui_root) if ui_root is not None else None
        self.library = Library(Path(external).parent) if external else Library()
        self._cached_signature = None
        self._cached_registry = None
        self._cache_lock = threading.RLock()

    def load(self):
        with self._cache_lock:
            signature = self.library.signature()
            if signature != self._cached_signature:
                self._cached_registry = self._load_normalized()
                self._cached_signature = signature
            return copy.deepcopy(self._cached_registry)

    def _load_normalized(self):
        data = self.library.read()
        names = {role: str(project_path(value, within=self.library.root)) for role, value in data.get('name_assets', {}).items()}
        assets = {}
        for identifier, value in data['assets'].items():
            item = copy.deepcopy(value)
            canonical = data['assets'].get(item.get('alias_of'))
            if canonical:
                for key in ('kind', 'role', 'face', 'layout', 'production_ready', 'measured_layout',
                            'art_source', 'has_bleed', 'source_bleed_mm', 'source_trim_mm', 'back_safety'):
                    if key in canonical:
                        item[key] = copy.deepcopy(canonical[key])
            item['id'] = identifier
            if not item.get('art_source'):
                continue
            item = self.normalize(item)
            item['available'] = Path(item['art_source']).is_file()
            if item.get('role') in names:
                item['name_asset'] = names[item['role']]
            assets[identifier] = item
        gallery = {item['id']: item for group in self.library.catalog().get('groups', []) for item in group['items']}
        return {'assets': assets, 'gallery': gallery, 'name_assets': names, 'families': FAMILIES, 'box_faces': BOX_NAMES,
                'library_revision': data.get('revision', 1), 'library_root': str(self.library.root)}

    @staticmethod
    def normalize(asset):
        asset = copy.deepcopy(asset)
        asset.setdefault('kind','face')
        if asset['kind']=='face':
            asset['role']=str(asset.get('role',asset.get('model',asset.get('character','')))).lower()
            asset.setdefault('display_name',asset.get('character',asset['role']))
        asset['art_source'] = str(resolve_path(asset['art_source']))
        if asset.get('name_asset'):
            asset['name_asset'] = str(resolve_path(asset['name_asset']))
        if asset.get('preview'):
            asset['preview'] = str(resolve_path(asset['preview']))
        layout = asset.setdefault('layout',{})
        coordinate_system=str(asset.get('coordinate_system',layout.get('coordinate_system','normalized_xyxy'))).lower().replace(' ','')
        xywh='xywh' in coordinate_system or 'x,y,w,h' in coordinate_system
        def normalized_box(value):
            if isinstance(value,dict):
                return [value['x'],value['y'],value['x']+value['w'],value['y']+value['h']]
            if xywh and isinstance(value,list) and len(value)==4:
                return [value[0],value[1],value[0]+value[2],value[1]+value[3]]
            return value
        for key in ('top_index_box','bottom_index_box','name_boxes','name_box','art_box','name_color'):
            if key in asset:
                layout[key] = asset.pop(key)
        for key in ('top_index_box','bottom_index_box','name_box','art_box'):
            if key in layout:
                layout[key]=normalized_box(layout[key])
        if 'name_boxes' in layout:
            layout['name_boxes']=[{'box':normalized_box(entry.get('box',entry)),'rotation_deg':entry.get('rotation_deg',entry.get('rotation',0))} for entry in layout['name_boxes']]
        if 'name_box' in layout and 'name_boxes' not in layout:
            layout['name_boxes'] = [{'box':layout['name_box'],'rotation_deg':0}]
        asset['source_coordinate_system']=asset.get('coordinate_system','normalized_xyxy')
        asset['coordinate_system']='normalized_xyxy'
        layout['coordinate_system']='normalized_xyxy'
        return asset

class Production:
    def __init__(self, ui_root, selection_store, external=None, exports_root=EXPORTS, legacy_exports=None, start_worker=True):
        # Load native rendering dependencies on the construction thread before any
        # export/request thread can race their first C-extension initialization.
        from merchant_export import build_merchant_export
        self.export_builder = build_merchant_export
        self.root = Path(ui_root)
        self.selection_store = selection_store
        self.registry = Registry(ui_root, external)
        self.path = self.root / 'deck-config.json'
        self.exports = project_path(exports_root)
        self.exports.mkdir(parents=True, exist_ok=True)
        self.legacy_exports = [project_path(path) for path in (legacy_exports or [])]
        self.lock = threading.RLock()
        self.jobs = queue.Queue()
        if not self.path.exists():
            write_json(self.path, self.defaults())
        for path in self.exports.glob('*/job.json'):
            job = read_json(path)
            if job.get('status') in {'queued', 'running', 'freezing'} and not pid_alive(job.get('worker_pid')):
                job.update(status='failed', error='上次制作进程已结束，冻结快照仍保留，请重新提交导出。', finished_at=now())
                write_json(path, job)
        if start_worker:
            threading.Thread(target=self.worker, daemon=True, name='deck-export').start()

    def defaults(self):
        selected = self.selection_store.current()
        registry = self.registry.load()
        slots = {role: {'black': None, 'red': None} for _, role, _ in FAMILIES}
        return {'schema_version': 1, 'version': 1, 'mapping_version': MAPPING_VERSION, 'updated_at': now(),
                'selection_revision': selected['revision'], 'slots': slots, 'back': 'back-v7' if 'back-v7' in registry['assets'] else None,
                'box': {face: ('box-' + face if 'box-' + face in registry['assets'] else None) for face in BOX_FACES},
                'settings': {'card_width_mm': 63, 'card_height_mm': 88, 'bleed_mm': 3, 'dpi': 300, 'safe_mm': 3,
                             'box_width_mm': 65, 'box_height_mm': 90, 'box_depth_mm': 18}}

    def current(self):
        with self.lock:
            return read_json(self.path)

    def state(self):
        config = self.current()
        registry = self.registry.load()
        selection = self.selection_store.current()
        return {'config':config,'selection_revision':selection['revision'],'validation':self.validate(config,registry,selection),'registry':registry}

    def validate(self, config, registry=None, selection=None):
        registry = registry or self.registry.load()
        selection = selection or self.selection_store.current()
        assets, errors = registry['assets'], []
        if not isinstance(config,dict) or any(not isinstance(config.get(key),dict) for key in ('slots','box','settings')):
            return ['制作配置的槽位、牌盒和尺寸必须为对象。']
        allowed_roles={role for _,role,_ in FAMILIES}
        if set(config['slots']) != allowed_roles or any(not isinstance(slot,dict) or set(slot) != {'black','red'} or any(value is not None and not isinstance(value,str) for value in slot.values()) for slot in config['slots'].values()):
            return ['人物槽位必须包含全部14个角色，每个角色仅允许黑色和红色素材编号。']
        if set(config['box']) != set(BOX_FACES) or any(value is not None and not isinstance(value,str) for value in config['box'].values()) or (config.get('back') is not None and not isinstance(config['back'],str)):
            return ['牌背和牌盒六面必须使用有效素材编号。']
        try:
            settings = config['settings']
            limits = {'card_width_mm':(40,100),'card_height_mm':(60,150),'bleed_mm':(0,10),'dpi':(150,600),'safe_mm':(3,10),'box_width_mm':(40,150),'box_height_mm':(60,200),'box_depth_mm':(5,100)}
            labels = {'card_width_mm':'卡牌宽度','card_height_mm':'卡牌高度','bleed_mm':'每边出血','dpi':'DPI','safe_mm':'文字安全距离','box_width_mm':'牌盒宽度','box_height_mm':'牌盒高度','box_depth_mm':'牌盒厚度'}
            for key,(low,high) in limits.items():
                value = settings.get(key)
                if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
                    errors.append(f'{labels[key]}必须在{low}到{high}之间。')
            if type(settings.get('dpi')) is not int:
                errors.append('DPI必须为整数。')
        except (TypeError,KeyError):
            errors.append('缺少制作尺寸设置。')
        for rank,role,name in FAMILIES:
            for color,label in [('black','黑色'),('red','红色')]:
                identifier = config.get('slots',{}).get(role,{}).get(color)
                prefix = f'{rank} {name} {label}槽位'
                if not identifier:
                    errors.append(prefix+'尚未指定。')
                elif identifier not in selection['selected_ids']:
                    errors.append(prefix+f'的 {identifier} 未在图库中勾选并保存。')
                elif identifier not in assets or assets[identifier].get('role') != role or assets[identifier]['kind'] != 'face':
                    errors.append(prefix+f'的 {identifier} 缺少对应角色的生产登记。')
                else:
                    asset = assets[identifier]
                    if not asset.get('production_ready', False):
                        errors.append(prefix+'尚未完成逐图排版测量，当前仅供图库查看。')
                    if not Path(asset['art_source']).is_file():
                        errors.append(prefix+'的无字原画文件不存在。')
                    layout = asset.get('layout',{})
                    for key in ('top_index_box','bottom_index_box'):
                        if not valid_box(layout.get(key)):
                            errors.append(prefix+f'缺少有效的逐图排版框 {key}。')
                    if not layout.get('name_boxes') or any(not valid_box(entry.get('box')) or entry.get('rotation_deg',0) not in (0,180) for entry in layout.get('name_boxes',[])):
                        errors.append(prefix+'缺少有效的品牌名排版框。')
                    if asset.get('name_asset') and not Path(asset['name_asset']).is_file():
                        errors.append(prefix+'品牌名素材不存在。')
        for label,identifier,kind,face in [('牌背',config.get('back'),'back',None)]+[('牌盒'+BOX_NAMES[face],config.get('box',{}).get(face),'box',face) for face in BOX_FACES]:
            if not identifier:
                errors.append(label+('尚未指定；等待Gemini/GPT黑色槽位服装选择后制作。' if label=='牌盒背面' else '尚未指定；请先选定素材。'))
            elif identifier not in assets or assets[identifier].get('kind') != kind or not Path(assets[identifier]['art_source']).is_file():
                errors.append(label+'的生产素材不可用。')
            elif kind == 'box' and assets[identifier].get('face') != face:
                errors.append(label+'必须使用该盒面的素材，不能混用其他盒面。')
        return errors

    def update(self,payload):
        with self.lock:
            current = self.current()
            if payload.get('base_version') != current['version']:
                raise ConfigConflict(current)
            incoming = payload.get('config')
            if not isinstance(incoming,dict):
                raise ValidationError(['缺少制作配置。'])
            config = {key:copy.deepcopy(incoming.get(key,current[key])) for key in ('slots','back','box','settings')}
            registry,selection = self.registry.load(),self.selection_store.current()
            # Missing slots are saveable; invalid references and dimensions are rejected.
            errors = self.validate(config,registry,selection)
            invalid = [error for error in errors if '尚未指定' not in error]
            if invalid:
                raise ValidationError(invalid)
            config.update(schema_version=1,version=current['version']+1,mapping_version=MAPPING_VERSION,updated_at=now(),selection_revision=selection['revision'])
            write_json(self.path,config)
            return self.state()

    def submit(self,payload):
        with self.lock, self.selection_store.lock:
            config,selection,registry = self.current(),self.selection_store.current(),self.registry.load()
            if payload.get('config_version') != config['version'] or payload.get('selection_revision') != selection['revision']:
                raise ConfigConflict(config)
            errors = self.validate(config,registry,selection)
            if errors:
                raise ValidationError(errors)
            identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:10]
            folder = self.exports / identifier
            folder.mkdir()
            job = {'worker_pid': os.getpid(), 'id':identifier,'status':'freezing','progress':0,'message':'冻结制作配置与原画','created_at':now(),'config_version':config['version'],'selection_revision':selection['revision']}
            write_json(folder/'job.json',job)
            try:
                used = {config['back'], *config['box'].values(), *[value for slots in config['slots'].values() for value in slots.values()]}
                selected_ids = set(selection['selected_ids'])
                missing = selected_ids - set(registry['assets'])
                if missing:
                    raise ValueError('选中原画尚未登记：' + ', '.join(sorted(missing)))
                all_frozen = {key: copy.deepcopy(registry['assets'][key]) for key in used | selected_ids}
                source_records = {}
                sources = folder / 'snapshot/sources'
                sources.mkdir(parents=True)
                for asset in all_frozen.values():
                    for field in ('art_source', 'name_asset'):
                        if not asset.get(field):
                            continue
                        original = project_path(asset[field], within=self.registry.library.root)
                        data = original.read_bytes()
                        sha = hashlib.sha256(data).hexdigest()
                        target = sources / (sha + original.suffix.lower())
                        if not target.exists():
                            target.write_bytes(data)
                        source_records[original.relative_to(PROJECT).as_posix()] = {'sha256': sha, 'bytes': len(data), 'frozen_path': target.relative_to(folder).as_posix()}
                        asset[field] = str(target)
                snapshot = {'config': config, 'selection': selection, 'assets': {key: all_frozen[key] for key in used},
                            'selected_assets': {key: all_frozen[key] for key in selected_ids}, 'sources': source_records, 'frozen_at': now()}
                write_json(folder/'snapshot/snapshot.json',snapshot)
                job.update(status='queued',message='已冻结，等待导出')
                write_json(folder/'job.json',job)
                self.jobs.put(identifier)
                return job
            except Exception as error:
                job.update(status='failed',error=str(error),finished_at=now())
                write_json(folder/'job.json',job)
                raise

    def preflight(self, config, registry):
        from merchant_export import print_profile, source_geometry, label_boxes, layout_plan, check_raster_geometry
        settings = copy.deepcopy(config['settings'])
        settings.update(bleed_mm=0, safe_mm=0)
        profile = print_profile(config)
        profile['geometry_guard_mm'] = max(profile['geometry_guard_mm'], math.sqrt(2) * 25.4 / settings['dpi'] + .01)
        trim = tuple(settings[key] for key in ('card_width_mm', 'card_height_mm'))
        upload = tuple(value + 2 * profile['trim_allowance_mm'] for value in trim)
        checked = set()
        with RENDER_LOCK:
            register_fonts()
            for card in cards_for(config):
                if card['asset_id'] in checked:
                    continue
                asset = registry['assets'][card['asset_id']]
                geometry = source_geometry(asset, settings, __import__(__name__))
                labels = label_boxes(card, asset, settings, __import__(__name__))
                plan = layout_plan(geometry, labels, trim, profile)
                check_raster_geometry(plan, trim, upload, settings['dpi'], profile)
                checked.add(card['asset_id'])

    def job_folder(self, identifier):
        if not JOB_ID.fullmatch(identifier):
            raise FileNotFoundError('无效的导出编号。')
        for root in [self.exports, *self.legacy_exports]:
            folder = root / identifier
            if (folder / 'job.json').is_file():
                return folder
        raise FileNotFoundError('导出编号不存在。')

    def job(self, identifier):
        return read_json(self.job_folder(identifier) / 'job.json')

    def download(self, identifier):
        folder = self.job_folder(identifier)
        job = read_json(folder / 'job.json')
        if job['status'] != 'complete':
            raise FileNotFoundError('导出尚未完成。')
        return project_path(folder / job.get('archive_relpath', 'ai-girls-deck.zip'), within=folder)

    def worker(self):
        while True:
            identifier = self.jobs.get()
            folder = self.exports/identifier
            job = self.job(identifier)
            def progress(value,message):
                job.update(status='running',progress=value,message=message)
                write_json(folder/'job.json',job)
            try:
                result = self.export_builder(folder, read_json(folder/'snapshot/snapshot.json'), progress)
                archive = project_path(result['archive'], within=folder)
                job.update(archive_relpath=archive.relative_to(folder).as_posix(), summary=result.get('summary', {}))
                if result.get('screen_archive'):
                    screen = project_path(result['screen_archive'], within=folder)
                    job['screen_archive_relpath'] = screen.relative_to(folder).as_posix()
                    job['screen_download_url'] = f'/api/exports/{identifier}/screen-download'
                job.update(status='complete',progress=100,message='制作包已完成',finished_at=now(),download_url=f'/api/exports/{identifier}/download')
            except Exception as error:
                job.update(status='failed',error=f'{type(error).__name__}: {error}',message='导出失败，请检查具体错误后重试。',finished_at=now())
            write_json(folder/'job.json',job)
            self.jobs.task_done()

def valid_box(box):
    return isinstance(box,list) and len(box)==4 and all(type(value) in (int,float) and math.isfinite(value) for value in box) and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1

def register_fonts():
    for name,path in FONTS.items():
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name,str(path)))

def placement(asset,settings):
    w,h = settings['card_width_mm'],settings['card_height_mm']
    with Image.open(asset['art_source']) as picture:
        iw,ih = picture.size
    area = asset.get('layout',{}).get('art_box',[2/63,2/88,61/63,86/88])
    ax,ay,aw,ah = area[0]*w,area[1]*h,(area[2]-area[0])*w,(area[3]-area[1])*h
    scale = min(aw/iw,ah/ih)
    tw,th = iw*scale,ih*scale
    return ax+(aw-tw)/2, h-ay-(ah+th)/2,tw,th

def art_box(asset,settings,box):
    x,y,w,h = placement(asset,settings)
    return x+box[0]*w,y+(1-box[3])*h,(box[2]-box[0])*w,(box[3]-box[1])*h

def symbol(page,suit,x,y,sw,sh):
    page.saveState(); page.translate(x,y); page.scale(sw,sh)
    p = page.beginPath()
    if suit == 'DIAMONDS':
        p.moveTo(0,.5); p.lineTo(.5,0); p.lineTo(0,-.5); p.lineTo(-.5,0); p.close()
    elif suit == 'HEARTS':
        p.moveTo(0,-.5); p.curveTo(-.12,-.38,-.5,-.08,-.5,.2); p.curveTo(-.5,.46,-.17,.6,0,.32); p.curveTo(.17,.6,.5,.46,.5,.2); p.curveTo(.5,-.08,.12,-.38,0,-.5); p.close()
    elif suit == 'SPADES':
        p.moveTo(0,.5); p.curveTo(.15,.3,.5,.04,.5,-.16); p.curveTo(.5,-.43,.15,-.49,.03,-.22); p.lineTo(.08,-.4); p.lineTo(.24,-.5); p.lineTo(-.24,-.5); p.lineTo(-.08,-.4); p.lineTo(-.03,-.22); p.curveTo(-.15,-.49,-.5,-.43,-.5,-.16); p.curveTo(-.5,.04,-.15,.3,0,.5); p.close()
    else:
        for cx,cy in [(0,.22),(-.25,-.05),(.25,-.05)]:
            p.circle(cx,cy,.26)
        p.moveTo(-.22,-.5); p.lineTo(.22,-.5); p.lineTo(.06,-.25); p.lineTo(-.06,-.25); p.close()
    page.drawPath(p,stroke=0,fill=1); page.restoreState()

def check_safe(bounds,w,h,safe,label):
    x,y,bw,bh = bounds
    if min(x,y,w-x-bw,h-y-bh) < safe-1e-5:
        raise ValueError(f'{label}超出裁切线内{safe}毫米安全区；请调整逐图排版框。')

def draw_face(page,asset,rank,suit,settings,bleed=True):
    w,h,b = settings['card_width_mm'],settings['card_height_mm'],settings['bleed_mm'] if bleed else 0
    page.saveState()
    page.setFillColor(HexColor(asset.get('layout',{}).get('background','#FFFFFF')))
    page.rect(0,0,(w+2*b)*mm,(h+2*b)*mm,stroke=0,fill=1)
    page.translate(b*mm,b*mm)
    x,y,aw,ah = placement(asset,settings)
    page.drawImage(asset['art_source'],x*mm,y*mm,aw*mm,ah*mm,mask='auto')
    color = '#B12F3D' if suit in ('HEARTS','DIAMONDS','RED_JOKER') else '#20242D'
    layout = asset['layout']
    for key,rotation in [('top_index_box',0),('bottom_index_box',180)]:
        x,y,bw,bh = art_box(asset,settings,layout[key])
        page.saveState(); page.translate((x+bw/2)*mm,(y+bh/2)*mm); page.rotate(rotation)
        page.setFillColor(HexColor(color))
        if rank == 'JOKER':
            size = min(9,bw*.85*mm/pdfmetrics.stringWidth('J','DeckBold',1),bh*.85*mm/5)
            total = size*5
            check_safe((x+(bw-size/mm)/2,y+(bh-total/mm)/2,size/mm,total/mm),w,h,settings['safe_mm'],asset['id']+'王角标')
            page.setFont('DeckBold',size)
            for i,letter in enumerate('JOKER'):
                page.drawCentredString(0,total/2-size*(i+.85),letter)
        else:
            size = min(16,bw*.86*mm/pdfmetrics.stringWidth(rank,'DeckBold',1),bh*.42*mm/.74)
            rh,sw,sh,gap = size*.72,min(4,bw*.75)*mm,min(3.8,bh*.32)*mm,min(.6,bh*.05)*mm
            total = rh+sh+gap
            ink_w = max(pdfmetrics.stringWidth(rank,'DeckBold',size),sw)/mm
            check_safe((x+(bw-ink_w)/2,y+(bh-total/mm)/2,ink_w,total/mm),w,h,settings['safe_mm'],asset['id']+'角标')
            page.setFont('DeckBold',size); page.drawCentredString(0,total/2-rh,rank)
            symbol(page,suit,0,-total/2+sh/2,sw,sh)
        page.restoreState()
    for entry in layout['name_boxes']:
        x,y,bw,bh = art_box(asset,settings,entry['box'])
        page.saveState(); page.translate((x+bw/2)*mm,(y+bh/2)*mm); page.rotate(entry.get('rotation_deg',0))
        if asset.get('name_asset'):
            with Image.open(asset['name_asset']) as picture:
                iw,ih=picture.size
            scale = min(bw*.9/iw,bh*.9/ih)
            nw,nh = iw*scale,ih*scale
            check_safe((x+(bw-nw)/2,y+(bh-nh)/2,nw,nh),w,h,settings['safe_mm'],asset['id']+'品牌名')
            page.drawImage(asset['name_asset'],-nw*mm/2,-nh*mm/2,nw*mm,nh*mm,mask='auto')
        else:
            name = asset['display_name']; font = 'DeckChinese' if any(ord(c)>127 for c in name) else 'DeckSans'
            size = min(7,bw*.9*mm/pdfmetrics.stringWidth(name,font,1),bh*.82*mm)
            nw,nh=pdfmetrics.stringWidth(name,font,size)/mm,size/mm
            check_safe((x+(bw-nw)/2,y+(bh-nh)/2,nw,nh),w,h,settings['safe_mm'],asset['id']+'名称')
            page.setFillColor(HexColor(layout.get('name_color','#34494C'))); page.setFont(font,size)
            page.drawCentredString(0,-size*.35,name)
        page.restoreState()
    page.restoreState()

def cards_for(config):
    cards=[]
    for rank,role,_ in FAMILIES[:-1]:
        for suit,color in SUITS:
            cards.append({'id':f'{len(cards)+1:02d}-{rank}-{suit}','rank':rank,'role':role,'suit':suit,'asset_id':config['slots'][role][color]})
    for color in ('black','red'):
        cards.append({'id':f'{len(cards)+1:02d}-{color.upper()}-JOKER','rank':'JOKER','role':'doubao','suit':color.upper()+'_JOKER','asset_id':config['slots']['doubao'][color]})
    return cards

def draw_image(page,asset,w,h,b):
    # Trim artwork stays at exactly (b,b,w,h). Each outer region repeats its nearest edge.
    page.saveState(); page.setFillColor(HexColor('#FFFFFF')); page.rect(0,0,(w+2*b)*mm,(h+2*b)*mm,stroke=0,fill=1)
    if asset.get('has_bleed'):
        page.drawImage(asset['art_source'],0,0,(w+2*b)*mm,(h+2*b)*mm,mask='auto')
    else:
        if b:
            with Image.open(asset['art_source']) as picture:
                picture=picture.copy()
                iw,ih=picture.size
                regions=[
                    ((0,0,1,ih),(0,b,b,h)),
                    ((iw-1,0,iw,ih),(w+b,b,b,h)),
                    ((0,0,iw,1),(b,h+b,w,b)),
                    ((0,ih-1,iw,ih),(b,0,w,b)),
                    ((0,0,1,1),(0,h+b,b,b)),
                    ((iw-1,0,iw,1),(w+b,h+b,b,b)),
                    ((0,ih-1,1,ih),(0,0,b,b)),
                    ((iw-1,ih-1,iw,ih),(w+b,0,b,b)),
                ]
                for source_box,(x,y,rw,rh) in regions:
                    page.drawImage(ImageReader(picture.crop(source_box)),x*mm,y*mm,rw*mm,rh*mm,mask='auto')
        page.drawImage(asset['art_source'],b*mm,b*mm,w*mm,h*mm,mask='auto')
    page.restoreState()

def add_srgb(path):
    data=path.read_bytes()
    if b'sRGB' not in data[:200]:
        kind,payload=b'sRGB',b'\0'
        chunk=struct.pack('>I',1)+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)&0xffffffff)
        offset=8+12+struct.unpack('>I',data[8:12])[0]
        path.write_bytes(data[:offset]+chunk+data[offset:])

def raster_page(pdf, target, page_number, dpi):
    with pdfium.PdfDocument(str(pdf)) as document:
        page = document[page_number - 1]
        bitmap = page.render(scale=dpi / 72)
        try:
            bitmap.to_pil().convert('RGB').save(target, dpi=(dpi, dpi))
        finally:
            bitmap.close()
            page.close()


def build_export(folder, snapshot, progress):
    from merchant_export import build_merchant_export
    return build_merchant_export(folder, snapshot, progress)

def preview_png(asset,rank,suit,settings):
    from merchant_export import preview_merchant_png
    with RENDER_LOCK:
        return preview_merchant_png(asset, rank, suit, settings)
