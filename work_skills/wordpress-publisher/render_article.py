#!/usr/bin/env python3
"""Read-only renderer for a server-captured public article bundle; no egress."""
import hashlib,io,json,os,re,sys,tempfile,zipfile
from pathlib import Path,PurePosixPath
from urllib.parse import urldefrag

bundle=Path(sys.argv[1]);output=Path(sys.argv[2]);output.mkdir(parents=True,exist_ok=True)
raw=zipfile.ZipFile(bundle)
if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts for n in raw.namelist()):raise ValueError('Invalid snapshot paths')
manifest=json.loads(raw.read('manifest.json'));source=raw.read('page.html').decode()
wrapper=Path(tempfile.mkdtemp(prefix='alli-chrome-'))/'chromium'
wrapper.write_text('#!/usr/bin/python3\nimport os,sys\nos.setresgid(1000,1000,1000)\nos.setresuid(1000,1000,1000)\nos.execv("/usr/lib/chromium/chromium",["/usr/lib/chromium/chromium"]+sys.argv[1:])\n');wrapper.chmod(0o755);wrapper.parent.chmod(0o755)
if os.geteuid()==0:
    os.chown(output,1000,1000);os.setgroups([]);os.setresgid(1000,1000,1000);os.setresuid(1000,1000,1000)
from playwright.sync_api import sync_playwright
from PIL import Image,ImageOps,ImageDraw
widths={320,390,1440}
for b in re.findall(r'(?:max|min)-width\s*:\s*(\d+)px',source):
    n=int(b)
    if 320<n<1800:widths.update((n-1,n+1))
if len(widths)>19:raise ValueError('Too many breakpoints; inspect the design before rendering')
audit=dict(source_hash=manifest['source_hash'],phase=manifest['phase'],url=manifest['url'],viewports=[])
assets=manifest.get('assets',{})
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=str(wrapper),headless=True,chromium_sandbox=True)
    context=browser.new_context(java_script_enabled=False,device_scale_factor=1)
    def route_request(route):
        url=urldefrag(route.request.url)[0]
        if url==manifest['url']:route.fulfill(status=200,content_type='text/html',body=source);return
        item=assets.get(url)
        if item:route.fulfill(status=200,content_type=item['content_type'],headers={'Access-Control-Allow-Origin':'*'},body=raw.read(item['file']));return
        route.abort()
    context.route('**/*',route_request)
    page=context.new_page();panels=[]
    for width in sorted(widths):
        page.set_viewport_size(dict(width=width,height=900 if width>=1100 else 844))
        page.goto(manifest['url'],wait_until='load');page.evaluate('document.fonts.ready')
        metrics=page.evaluate('''() => {
          const root=document.querySelector('.codex-html-experience')?.shadowRoot||document;
          const headings=[...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(e=>e.getBoundingClientRect().height>0);
          let split=0,clip=0,overlap=0;const rects=[];
          for(const el of headings){
            const r=el.getBoundingClientRect();rects.push({text:el.textContent,x:r.x,y:r.y+scrollY,width:r.width,height:r.height});
            let text='',nodes=[];const walk=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
            while(walk.nextNode()){nodes.push({node:walk.currentNode,start:text.length});text+=walk.currentNode.textContent;}
            for(const match of text.matchAll(/\\S+/g)){
              const a=match.index,b=a+match[0].length;
              const left=[...nodes].reverse().find(n=>n.start<=a),right=[...nodes].reverse().find(n=>n.start<b);
              if(!left||!right)continue;const range=document.createRange();range.setStart(left.node,a-left.start);range.setEnd(right.node,b-right.start);
              const pieces=[...range.getClientRects()].filter(x=>x.width>0);if(new Set(pieces.map(x=>Math.round(x.top))).size>1)split++;
              if(pieces.some(x=>x.left< -1||x.right>innerWidth+1))clip++;
            }
          }
          for(let a=0;a<rects.length;a++)for(let b=a+1;b<rects.length;b++){
            const x=rects[a],y=rects[b];if(Math.min(x.x+x.width,y.x+y.width)-Math.max(x.x,y.x)>1&&Math.min(x.y+x.height,y.y+y.height)-Math.max(x.y,y.y)>1)overlap++;
          }
          const descendants=[...root.querySelectorAll('*')].filter(e=>e.getBoundingClientRect().width>0);
          const overflow=Math.max(0,document.documentElement.scrollWidth-innerWidth,...descendants.map(e=>Number.isFinite(e.scrollWidth-e.clientWidth)&&e.scrollWidth-e.clientWidth>1?e.scrollWidth-e.clientWidth:0));
          return {width:innerWidth,overflow_px:overflow,split_words:split,clipped_headings:clip,overlapping_headings:overlap,headings:rects};
        }''')
        if not metrics['headings']:raise ValueError('Article did not render any headings')
        audit['viewports'].append(metrics)
        if width in (320,390,1440):page.screenshot(path=str(output/f'{width}.png'),full_page=False)
        if width==390:
            page.screenshot(path=str(output/'390-full.png'),full_page=True)
            full=Image.open(output/'390-full.png').convert('RGB')
            for h in metrics['headings']:
                top=max(0,int(h['y'])-12);bottom=min(full.height,top+int(h['height'])+170)
                panels.append(full.crop((0,top,390,bottom)))
        # CSS-only controls may be exercised; scripts, forms and network writes are disabled.
        for control in page.locator('input[type=radio],input[type=checkbox]').all()[:12]:
            if control.is_visible():
                was=control.is_checked();control.set_checked(True)
                if not was and control.get_attribute('type')=='checkbox':control.set_checked(False)
    browser.close()
if panels:
    cellw=410;cellh=max(i.height for i in panels)+30
    sheet=Image.new('RGB',(cellw*2,cellh*((len(panels)+1)//2)),'#eeeae0')
    for i,im in enumerate(panels):sheet.paste(im,((i%2)*cellw+10,(i//2)*cellh+10))
    sheet.save(output/'headings.png')
(output/'audit.json').write_text(json.dumps(audit,indent=2))
print(json.dumps(dict(output=str(output),phase=audit['phase'],source_hash=audit['source_hash'],viewports=[{k:v for k,v in r.items() if k!='headings'} for r in audit['viewports']]),indent=2))
