"""PP-OCRv5 -> MiniCPM3 body processing -> PDFs. Execute on the CIX board.

Only board model outputs supply document text. Chat transcriptions are not input.
"""
import argparse, collections, datetime, difflib, hashlib, json, re, socket
import statistics, subprocess, time, urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

MODEL_DIR = Path('/home/radxa/tu/model/ppocrv5_mobile')
RECOGNITION_MODEL = MODEL_DIR / 'ch_PP-OCRv5_rec_mobile_finetuned_20261112.onnx'
RECOGNITION_LABEL = 'PP-OCRv5 mobile rec, fine-tuned 2026-11-12'

def ocr_params(recognition_model=RECOGNITION_MODEL):
    from rapidocr import OCRVersion, ModelType
    return {'Det.ocr_version': OCRVersion.PPOCRV5, 'Rec.ocr_version': OCRVersion.PPOCRV5,
            'Cls.ocr_version': OCRVersion.PPOCRV5, 'Det.model_type': ModelType.MOBILE,
            'Rec.model_type': ModelType.MOBILE, 'Cls.model_type': ModelType.MOBILE,
            'Det.limit_side_len': 1536, 'Det.limit_type': 'max', 'Det.mean': [.485,.456,.406],
            'Det.std': [.229,.224,.225], 'Global.max_side_len': 3000,
            'Global.model_root_dir': str(MODEL_DIR), 'Rec.model_path': str(recognition_model),
            'Global.text_score': .45, 'EngineConfig.onnxruntime.intra_op_num_threads': 4,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1}

def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def norm(s):
    return re.sub(r'\s', '', s)

def rect(it):
    b = it['box']
    return min(p[0] for p in b), min(p[1] for p in b), max(p[0] for p in b), max(p[1] for p in b)

def body_items(items):
    components = []
    for it in sorted((x for x in items if re.search('[一-鿿]', x['text'])), key=lambda x: rect(x)[0]):
        x0, _, x1, _ = rect(it)
        if components and x0 <= components[-1]['end'] + 45:
            components[-1]['items'].append(it)
            components[-1]['end'] = max(components[-1]['end'], x1)
        else:
            components.append({'items': [it], 'end': x1})
    if not components:
        return []
    main = max(components, key=lambda c: sum(len(x['text']) for x in c['items']))['items']
    slopes = [(i['box'][1][1]-i['box'][0][1])/(i['box'][1][0]-i['box'][0][0])
              for i in main if abs(i['box'][1][0]-i['box'][0][0]) > 50]
    slope = statistics.median(slopes) if slopes else 0
    return sorted(main, key=lambda i: (rect(i)[1]+rect(i)[3])/2-slope*((rect(i)[0]+rect(i)[2])/2-600))

def text_of(rec):
    return ''.join(i['text'] for i in rec['body_items'])

def jac(a, b):
    a, b = ({s[k:k+3] for k in range(max(0, len(s)-2))} for s in (a, b))
    return len(a & b)/max(1, len(a | b))

def make_plan(rec, endpoint, out, max_tokens=256):
    items = rec['body_items']
    left = min(rect(i)[0] for i in items)
    indent = [k+1 for k, i in enumerate(items) if rect(i)[0] > left+30]
    lines = '\n'.join(f'{k+1}: {it["text"]}' for k, it in enumerate(items))
    candidates = sorted({1} | set(indent))
    prompt = ('整理编号OCR行，只输出JSON。starts列出自然段首行号，须含1，只能从缩进行号中选择。'
              'edits最多2处，仅修复明确错字或识别重复；old和new均不超过8字，old须为该行唯一原文子串。'
              '不改正确文字，不替换同义词，不整句改写；不确定则edits为空数组。保留跨页残句，不补写、不总结。'
              f'原图缩进行号供参考：{indent}。\n{lines}')
    schema = {'type':'object','properties':{
        'starts':{'type':'array','items':{'type':'integer','enum':candidates},'minItems':1,'maxItems':len(candidates)},
        'edits':{'type':'array','maxItems':2,'items':{'type':'object','properties':{
            'line':{'type':'integer','minimum':1,'maximum':len(items)},
            'old':{'type':'string','minLength':1,'maxLength':8},
            'new':{'type':'string','maxLength':8}},
            'required':['line','old','new'],'additionalProperties':False}}},
        'required':['starts','edits'],'additionalProperties':False}
    payload = {'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0,
               'max_tokens': max_tokens, 'response_format': {'type':'json_schema','json_schema':{
                   'name':'body_plan','strict':True,'schema':schema}}}
    req = urllib.request.Request(endpoint+'/v1/chat/completions', data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    start = time.time()
    with urllib.request.urlopen(req, timeout=300) as response:
        result = json.load(response)
    raw = result['choices'][0]['message']['content']
    dump(out, {'request': payload, 'response': result, 'elapsed_seconds': time.time()-start})
    if result['choices'][0].get('finish_reason') == 'length':
        raise ValueError('Model output reached token limit')
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.S).strip()
    plan = json.loads(raw[raw.index('{'):raw.rindex('}')+1])
    if not isinstance(plan.get('starts'), list) or not isinstance(plan.get('edits', []), list):
        raise ValueError('Invalid body plan schema')
    return plan

def apply_plan(rec, plan):
    original = [i['text'] for i in rec['body_items']]
    texts = original.copy()
    changes, rejected = [], []
    for edit in plan.get('edits', [])[:2]:
        try:
            k = int(edit['line'])-1
            old, new = edit['old'], edit['new']
            assert 0 <= k < len(texts) and isinstance(old, str) and isinstance(new, str)
            assert old and old != new and texts[k].count(old) == 1 and max(len(old),len(new)) <= 8
            # A text-only small model cannot overrule confident image recognition
            # or delete genuine characters at a printed line boundary.
            cjk = lambda value: ''.join(re.findall('[一-鿿A-Za-z0-9]', value))
            if cjk(old) != cjk(new):
                assert rec['body_items'][k]['score'] < .97
                single_substitution = len(old) == len(new) and sum(a != b for a,b in zip(old,new)) == 1
                repeated_span = bool(new) and len(old) == 2*len(new) and old == new+new
                assert single_substitution or repeated_span
            else:
                # Do not insert punctuation into the middle of an OCR text span.
                assert not cjk(old) and not cjk(new)
            updated = texts[k].replace(old, new, 1)
            assert difflib.SequenceMatcher(None, texts[k], updated, autojunk=False).ratio() >= .65
            texts[k] = updated
            changes.append(edit)
        except (KeyError, ValueError, TypeError, AssertionError):
            rejected.append(edit)
    similarity = difflib.SequenceMatcher(None, ''.join(original), ''.join(texts), autojunk=False).ratio()
    if similarity < .96:
        rejected.extend(changes)
        texts, changes, similarity = original, [], 1.0
    starts = sorted({1} | {int(v) for v in plan['starts'] if str(v).isdigit() and 1 <= int(v) <= len(texts)})
    paragraphs = [''.join(texts[a-1:b-1]) for a, b in zip(starts, starts[1:]+[len(texts)+1])]
    return {'source_order': rec['order'], 'book_page': rec['book_page'], 'frame_index': rec['frame_index'],
            'paragraphs': paragraphs, 'model_plan': plan, 'applied_edits': changes,
            'rejected_edits': rejected, 'character_similarity_to_ocr': similarity,
            'model': 'MiniCPM3-4B Q4_K_M', 'body_processed_by_model': True}

def export_pdfs(pages, recs, out, output_label='EMEET_20261107'):
    from PIL import Image
    from pypdf import PdfReader
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.platypus import SimpleDocTemplate, Paragraph, PageBreak
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    pdfmetrics.registerFont(TTFont('SimFang', '/home/radxa/Project/pp-ocr-v4/simfang.ttf'))
    style = ParagraphStyle('Body', fontName='SimFang', fontSize=12, leading=21, firstLineIndent=24,
                           spaceAfter=7, alignment=TA_JUSTIFY, wordWrap='CJK')
    head = ParagraphStyle('Head', fontName='SimFang', fontSize=12, leading=18, spaceAfter=20)
    text_pdf = out/f'{output_label}_V5_MiniCPM3_文字版.pdf'
    story = []
    for p in pages:
        if story:
            story.append(PageBreak())
        label = f'原书第{p["book_page"]}页' if p['book_page'] is not None else f'原始选帧{p["source_order"]}'
        story.append(Paragraph(label, head))
        for text in p['paragraphs']:
            story.append(Paragraph(escape(text), style))
    SimpleDocTemplate(str(text_pdf), pagesize=A4, leftMargin=65, rightMargin=65, topMargin=60,
                      bottomMargin=60, title='PP-OCRv5 MiniCPM3 正文处理版', author=socket.gethostname()).build(story)
    search_pdf = out/f'{output_label}_V5_可搜索原图版.pdf'
    c = canvas.Canvas(str(search_pdf), pageCompression=1)
    c.setTitle('PP-OCRv5 可搜索原图版')
    c.setAuthor(socket.gethostname())
    for r in recs:
        w, h = Image.open(r['image']).size
        c.setPageSize((w/2, h/2));c.saveState();c.scale(.5, .5)
        c.drawImage(r['image'], 0, 0, width=w, height=h)
        for it in r['body_items']:
            x0,y0,x1,y1 = rect(it);bw,bh = x1-x0,y1-y0;text = it['text']
            fs = min(bh*.85, bw/max(1, len(text))*1.06)
            if fs < 3 or bw < 8 or bh < 6:
                continue
            t = c.beginText();t.setTextRenderMode(3);t.setFont('SimFang', fs)
            t.setHorizScale(bw/max(.01, pdfmetrics.stringWidth(text, 'SimFang', fs))*100)
            t.setTextOrigin(x0,h-y1+bh*.12);t.textOut(text);c.drawText(t)
        c.restoreState()
        label = '原书第'+str(r['book_page'])+'页' if r['book_page'] is not None else '原始选帧'+str(r['order'])
        key = 'p'+str(r['order']);c.bookmarkPage(key);c.addOutlineEntry(label,key,0);c.showPage()
    c.save()
    readers = {p.name: PdfReader(p) for p in (text_pdf, search_pdf)}
    assert all(len(r.pages) == len(pages) for r in readers.values())
    for pdfpage, p in zip(readers[text_pdf.name].pages, pages):
        assert not pdfpage.images
        label = f'原书第{p["book_page"]}页' if p['book_page'] is not None else f'原始选帧{p["source_order"]}'
        assert norm(pdfpage.extract_text()) == norm(label+''.join(p['paragraphs']))
    for p in (text_pdf, search_pdf):
        d = out/('render_text' if p == text_pdf else 'render_search');d.mkdir(exist_ok=True)
        subprocess.run(['/usr/bin/pdftoppm','-scale-to','1100','-png',str(p),str(d/'page')],check=True)
    return {'text_pdf':str(text_pdf),'searchable_pdf':str(search_pdf),'pages':len(pages),
            'text_roundtrip_exact':True,'text_pdf_has_no_images':True,'both_rendered':True}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default='/home/radxa/tu/workspace/emeet_20261107')
    ap.add_argument('--output', default='/home/radxa/tu/workspace/emeet_20261107_v5_minicpm3')
    ap.add_argument('--endpoint', default='http://127.0.0.1:8090')
    ap.add_argument('--label',default='EMEET_20261107')
    args = ap.parse_args()
    source, out = Path(args.source), Path(args.output)
    out.mkdir(parents=True, exist_ok=True);(out/'llm_raw').mkdir(exist_ok=True)
    def status(stage, done=0, total=0, **kwargs):
        dump(out/'status.json', {'stage':stage,'done':done,'total':total,'updated_at':datetime.datetime.now().astimezone().isoformat(),**kwargs})
        print(f'[{stage}] {done}/{total} {kwargs}',flush=True)
    manifest = json.loads((source/'manifest.json').read_text())
    inputs = [p for p in manifest['pages'] if p.get('include',True)]
    raw_path = out/'ocr_v5.jsonl'
    records = [json.loads(l) for l in raw_path.read_text().splitlines()] if raw_path.exists() else []
    recognition_hash = hashlib.sha256(RECOGNITION_MODEL.read_bytes()).hexdigest()
    if any(r.get('recognition_sha256') != recognition_hash for r in records):
        raise SystemExit('Existing OCR results use other weights. Choose a new --output directory.')
    complete = {r['order'] for r in records}
    if any(p['order'] not in complete for p in inputs):
        from rapidocr import RapidOCR
        import cv2
        params = ocr_params()
        engine = RapidOCR(params=params)
        dump(out/'ocr_config.json', {k:str(v) for k,v in params.items()})
        with raw_path.open('a') as f:
            for p in inputs:
                if p['order'] in complete:
                    continue
                image = source/p['crop_image'];t = time.time();result = engine(str(image))
                if result.boxes is None or result.txts is None or not len(result.txts):
                    status('no_text',len(records),len(inputs),source_order=p['order'])
                    raise RuntimeError(f'No text detected in source page {p["order"]}')
                items = [{'box':b.tolist(),'text':text,'score':float(score)} for b,text,score in zip(result.boxes,result.txts,result.scores)]
                main_items = body_items(items)
                if not main_items:
                    status('no_text',len(records),len(inputs),source_order=p['order'])
                    raise RuntimeError(f'No body text for source page {p["order"]}')
                h = cv2.imread(str(image)).shape[0]
                left,right = min(rect(i)[0] for i in main_items),max(rect(i)[2] for i in main_items)
                numbers = [i for i in items if re.fullmatch(r'\d{1,3}', i['text']) and rect(i)[1] > h*.65
                           and left-100 < rect(i)[0] < right+100 and i['score']>.8]
                book_page = int(max(numbers,key=lambda i:rect(i)[1])['text']) if numbers else None
                rec = {'order':p['order'],'frame_index':p['frame_index'],'timestamp':p['timestamp'],
                       'image':str(image),'book_page':book_page,'items':items,'body_items':main_items,
                       'mean_score':statistics.mean(i['score'] for i in main_items),'seconds':time.time()-t,
                       'n_chars':sum(len(i['text']) for i in main_items),'model':RECOGNITION_LABEL,
                       'recognition_sha256':recognition_hash,
                       'runtime':'ONNX Runtime CPUExecutionProvider'}
                f.write(json.dumps(rec,ensure_ascii=False)+'\n');f.flush();records.append(rec)
                status('ocr_v5',len(records),len(inputs),source_order=p['order'],book_page=book_page,
                       mean_score=round(rec['mean_score'],4),seconds=round(rec['seconds'],2))
    records.sort(key=lambda r:r['order'])
    groups=[]
    for r in records:
        matched=None
        for group in groups:
            known_numbers={p['book_page'] for p in group if p['book_page'] is not None}
            if r['book_page'] is not None and known_numbers and r['book_page'] not in known_numbers:
                continue
            if max(jac(text_of(p),text_of(r)) for p in group)>.60:
                matched=group
                break
        if matched is None:
            groups.append([r])
        else:
            matched.append(r)
    selected = []
    for group in groups:
        best = dict(max(group,key=lambda r:r['n_chars']*r['mean_score']**3))
        best['first_seen_order']=group[0]['order']
        numbers = {r['book_page'] for r in group if r['book_page'] is not None}
        if best['book_page'] is None and len(numbers) == 1:
            best['book_page'] = next(iter(numbers))
            best['book_page_source'] = 'OCR consensus from the duplicate-frame group'
        selected.append(best)
    dump(out/'selection.json',{'duplicate_groups':[[r['order'] for r in g] for g in groups if len(g)>1],
         'selected_orders':[r['order'] for r in selected],'book_pages':[r['book_page'] for r in selected],
         'dedup_scope':'All selected video frames, including nonadjacent return-to-page frames',
         'selection_policy':'Prefer recognized main-page text coverage weighted by confidence; preserve first-seen page sequence'})
    done_path=out/'body_minicpm3.jsonl'
    processed=[json.loads(l) for l in done_path.read_text().splitlines()] if done_path.exists() else []
    selected_ids={r['order'] for r in selected}
    discarded=[p for p in processed if p['source_order'] not in selected_ids]
    if discarded:
        dump(out/'body_plans_for_unselected_frames.json',discarded)
    processed=[p for p in processed if p['source_order'] in selected_ids]
    done_path.write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in processed))
    done={p['source_order'] for p in processed}
    with done_path.open('a') as f:
        for r in selected:
            if r['order'] in done:
                continue
            status('minicpm3_body',len(processed),len(selected),processing_source_order=r['order'])
            for attempt in range(2):
                try:
                    plan=make_plan(r,args.endpoint,out/'llm_raw'/f'p{r["order"]:03d}_attempt{attempt+1}.json',256 if attempt==0 else 512)
                    page=apply_plan(r,plan)
                    break
                except Exception as exc:
                    status('minicpm3_retry',len(processed),len(selected),source_order=r['order'],error=str(exc))
                    if attempt==1:
                        raise
            f.write(json.dumps(page,ensure_ascii=False)+'\n');f.flush();processed.append(page)
            status('minicpm3_body',len(processed),len(selected),completed_source_order=r['order'],
                   paragraphs=len(page['paragraphs']),edits=len(page['applied_edits']))
    # Reapply the current text-preservation policy to cached model plans on resume.
    # This never introduces human/chat text or invokes the model unnecessarily.
    by_source = {r['order']:r for r in selected}
    processed = [apply_plan(by_source[p['source_order']],p['model_plan']) for p in processed]
    sequence={r['order']:i for i,r in enumerate(selected)}
    processed.sort(key=lambda p:sequence[p['source_order']])
    replacement=out/'body_minicpm3.revalidated.jsonl'
    replacement.write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in processed))
    replacement.replace(done_path)
    label=re.sub(r'[^\w.\-]','_',args.label)
    (out/f'{label}_V5_MiniCPM3_正文.txt').write_text('\n\n'.join(
        (f'原书第{p["book_page"]}页' if p['book_page'] is not None else f'原始选帧{p["source_order"]}')+'\n\n'+'\n\n'.join(p['paragraphs']) for p in processed))
    status('export',0,2)
    exports=export_pdfs(processed,selected,out,label)
    weights={name:hashlib.sha256((MODEL_DIR/name).read_bytes()).hexdigest() for name in
             ('ch_PP-OCRv5_det_mobile.onnx', 'ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx')}
    weights[RECOGNITION_MODEL.name] = recognition_hash
    dump(out/'run_manifest.json',{'hostname':socket.gethostname(),'source_workspace':str(source),
        'source_video':manifest.get('video'),'output_label':label,
        'generated_at':datetime.datetime.now().astimezone().isoformat(),'ocr':RECOGNITION_LABEL,
        'recognition_model':str(RECOGNITION_MODEL),
        'ocr_device':'Board CPU, ONNX Runtime CPUExecutionProvider; not CIX NPU',
        'version':'V3_PP-OCRv5_MiniCPM3_SinglePage_20261107','processing_page_mode':'single_page',
        'body_model':'MiniCPM3-4B Q4_K_M at '+args.endpoint,'body_role':'Paragraph structure and bounded localized text corrections',
        'all_body_pages_processed_by_model':all(p['body_processed_by_model'] for p in processed),
        'text_source':'Board PP-OCRv5 and board MiniCPM3 only; no chat transcription or local corrected text',
        'model_hashes':weights,'body_pages_processed':len(processed),
        'model_request_attempts':len(list(out.glob('llm_raw*/p*.json'))),
        'applied_edits':sum(len(p['applied_edits']) for p in processed),
        'ocr_seconds':sum(r['seconds'] for r in records),
        'text_preservation_policy':'Confident OCR text preserved; character edits limited to low-confidence single substitutions or exact duplicate removal; no completion of missing content',
        'exports':exports})
    status('complete',len(processed),len(selected),exports=exports)

if __name__ == '__main__':
    main()
