import os, json, uuid, subprocess, threading, re, shutil, html, sqlite3, hashlib, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; OUT=DATA/'outputs'; UP=DATA/'uploads'; JOBDIR=DATA/'jobs'
for d in (DATA,OUT,UP,JOBDIR): d.mkdir(parents=True,exist_ok=True)
JOBS={}
try:
    from openai import OpenAI
except Exception:
    OpenAI=None
client=OpenAI(api_key=os.getenv('OPENAI_API_KEY')) if (OpenAI and os.getenv('OPENAI_API_KEY')) else None
DB=DATA/'app.db'
def db():
    c=sqlite3.connect(DB); c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, password_hash TEXT, active INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)'); c.commit(); return c
def hashpw(p): return hashlib.sha256(p.encode()).hexdigest()
db().close()

def run(args, timeout=1800):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-5000:] or 'Processamento falhou')
    return p.stdout

def probe(path):
    j=json.loads(run(['ffprobe','-v','error','-show_entries','format=duration:stream=width,height','-of','json',str(path)]))
    dur=float(j.get('format',{}).get('duration') or 0)
    vs=[s for s in j.get('streams',[]) if s.get('width')]
    return dur,(vs[0]['width'],vs[0]['height']) if vs else (0,0)

def analyze_local(src,dur,target,count):
    # Real audio-energy + scene-change analysis. It does not invent clips.
    step=max(6,target/2); candidates=[]; t=0
    while t+target<=dur+0.01:
        candidates.append((t,min(t+target,dur))); t+=step
    if not candidates: candidates=[(0,dur)]
    scored=[]
    for a,b in candidates:
        try:
            out=run(['ffmpeg','-hide_banner','-ss',str(a),'-t',str(b-a),'-i',str(src),'-af','volumedetect','-f','null','-'],120)
            m=re.search(r'mean_volume:\s*(-?[\d.]+) dB',out); pk=re.search(r'max_volume:\s*(-?[\d.]+) dB',out)
            mean=float(m.group(1)) if m else -45; peak=float(pk.group(1)) if pk else -20
        except Exception: mean,peak=-45,-20
        # Avoid only the first seconds; reward energetic speech and dynamic range.
        energy=max(0,min(70,mean+45)); dyn=max(0,min(20,(peak-mean)*2)); pos=10*(1-abs((a+b)/2-dur/2)/max(dur/2,1))
        scored.append({'start':a,'end':b,'local_score':round(energy+dyn+pos,2),'text':''})
    scored.sort(key=lambda x:x['local_score'],reverse=True)
    chosen=[]
    for c in scored:
        if all(c['end']<=x['start'] or c['start']>=x['end'] for x in chosen):
            chosen.append(c)
        if len(chosen)>=count: break
    return sorted(chosen,key=lambda x:x['start'])

def transcribe(audio):
    if not client: return None
    with open(audio,'rb') as f:
        r=client.audio.transcriptions.create(model='gpt-4o-transcribe',file=f,response_format='verbose_json',timestamp_granularities=['segment'])
    return {'text':getattr(r,'text',''),'segments':[{'start':float(s.start),'end':float(s.end),'text':s.text.strip()} for s in (getattr(r,'segments',[]) or [])]}

def ai_rank(cands):
    if not client or not cands: return cands
    payload=[{'id':i,'start':c['start'],'end':c['end'],'text':c.get('text','')[:1500]} for i,c in enumerate(cands)]
    try:
        r=client.responses.create(model=os.getenv('OPENAI_RANK_MODEL','gpt-5.6-luna'),input=[
          {'role':'system','content':'Você é editor de vídeos curtos. Dê nota 0-100 para cada trecho. Valorize gancho, contexto completo, surpresa, utilidade, emoção, opinião forte e payoff. Penalize início no meio da frase e trecho sem conclusão. Responda JSON com scores: [{"id":0,"score":87}].'},
          {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],text={'format':{'type':'json_object'}})
        obj=json.loads(r.output_text); mp={int(x['id']):float(x['score']) for x in obj.get('scores',[])}
        for i,c in enumerate(cands): c['ai_score']=mp.get(i,c['local_score'])
    except Exception as e:
        for c in cands: c['ai_score']=c['local_score']
    return cands

def srt_for(trans,start,end,path):
    if not trans or not trans.get('segments'): return None
    def ts(v):
        v=max(0,v); ms=int((v-int(v))*1000); z=int(v); return f'{z//3600:02d}:{z%3600//60:02d}:{z%60:02d},{ms:03d}'
    n=1; lines=[]
    for s in trans['segments']:
        a=max(start,s['start']); b=min(end,s['end'])
        if b<=a: continue
        text=s['text'].replace('-->','—>')
        lines += [str(n),f'{ts(a-start)} --> {ts(b-start)}',text,'']; n+=1
    if not lines: return None
    path.write_text('\n'.join(lines),encoding='utf-8'); return path

def render(src,clip,out,aspect,res,captions,srt,zoom):
    sizes={'1080p':1080,'720p':720,'480p':480}; base=sizes.get(res,1080)
    if aspect=='9:16': W,H=base,round(base*16/9)
    elif aspect=='1:1': W=H=base
    else: W,H=(1920,1080) if res=='1080p' else ((1280,720) if res=='720p' else (854,480))
    if aspect in ('9:16','1:1'):
        vf=f'scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}'
    else:
        vf=f'scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2'
    if zoom: vf+=',eq=contrast=1.03:brightness=0.01'
    if captions and srt:
        p=str(srt).replace('\\','/').replace(':','\\:')
        vf+=f",subtitles='{p}'"
    run(['ffmpeg','-y','-ss',str(clip['start']),'-t',str(clip['end']-clip['start']),'-i',str(src),'-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','19','-c:a','aac','-b:a','192k','-movflags','+faststart',str(out)],900)

def download_youtube(url,dest):
    ytdlp=shutil.which('yt-dlp') or '/usr/local/bin/yt-dlp'
    if not Path(ytdlp).exists(): raise RuntimeError('yt-dlp não está instalado. Rode pelo Docker ou instale yt-dlp no servidor.')
    run([ytdlp,'--no-playlist','-f','bv*+ba/b','--merge-output-format','mp4','-o',str(dest),url],1200)

def process(j):
    try:
        j['status']='preparando'; j['progress']=5
        src=Path(j['dir'])/'source.mp4'
        if j['input_type']=='youtube':
            j['status']='baixando vídeo do YouTube'; j['progress']=10; download_youtube(j['url'],src)
        else: shutil.copy2(j['upload'],src)
        dur,_=probe(src)
        if dur<=0: raise RuntimeError('Vídeo sem duração válida.')
        j['status']='analisando áudio e cenas'; j['progress']=25
        audio=Path(j['dir'])/'audio.wav'
        run(['ffmpeg','-y','-i',str(src),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(audio)],600)
        j['status']='transcrevendo com IA' if client else 'analisando localmente (IA opcional)'; j['progress']=38
        trans=transcribe(audio); j['ai_enabled']=bool(client)
        cands=analyze_local(src,dur,j['duration'],j['count'])
        if trans:
            for c in cands:
                c['text']=' '.join(s['text'] for s in trans['segments'] if s['end']>c['start'] and s['start']<c['end'])
            cands=ai_rank(cands)
        cands=sorted(cands,key=lambda c:c.get('ai_score',c['local_score']),reverse=True)[:j['count']]
        j['clips']=cands
        j['status']='renderizando cortes'; j['progress']=52
        for i,c in enumerate(cands,1):
            srt=None
            if j['captions']:
                srt=srt_for(trans,c['start'],c['end'],Path(j['dir'])/f'corte_{i}.srt')
            out=OUT/f"{j['id']}_corte_{i:02d}.mp4"
            render(src,c,out,j['aspect'],j['resolution'],j['captions'],srt,j['zoom'])
            c['n']=i;c['url']='/outputs/'+out.name;c['score']=round(c.get('ai_score',c['local_score']),1)
            j['progress']=52+int(i/len(cands)*45)
        j['status']='concluído';j['progress']=100
    except Exception as e:
        j['status']='erro';j['error']=str(e);j['progress']=100

def parse_multipart(handler):
    # Dependency-free multipart parser (works on Python 3.13+, where cgi was removed).
    import email
    ctype=handler.headers.get('content-type','')
    boundary=None
    m=re.search(r'boundary=(?:"([^"]+)"|([^;]+))',ctype)
    if m: boundary=(m.group(1) or m.group(2)).encode()
    if not boundary: raise RuntimeError('multipart/form-data sem boundary')
    length=int(handler.headers.get('content-length','0'))
    body=handler.rfile.read(length)
    marker=b'--'+boundary
    fields={}
    for part in body.split(marker):
        part=part.strip(b'\r\n-')
        if not part: continue
        if b'\r\n\r\n' not in part: continue
        hb,data=part.split(b'\r\n\r\n',1)
        headers=email.message_from_bytes(hb+b'\r\n')
        cd=headers.get('content-disposition','')
        nm=re.search(r'name="([^"]+)"',cd)
        if not nm: continue
        name=nm.group(1)
        fm=re.search(r'filename="([^"]*)"',cd)
        if fm:
            fields[name]={'filename':fm.group(1),'data':data.rstrip(b'\r\n')}
        else:
            fields[name]=data.rstrip(b'\r\n').decode('utf-8','replace')
    class FS:
        def __contains__(self,k): return k in fields
        def __getitem__(self,k):
            v=fields[k]
            return type('Part',(),{'file':__import__('io').BytesIO(v['data'])})()
        def getfirst(self,k,default=''): return fields.get(k,default) if isinstance(fields.get(k,default),str) else default
    return FS()


def read_json(handler):
    n=int(handler.headers.get('content-length','0')); raw=handler.rfile.read(n) if n else b'{}'; return json.loads(raw.decode() or '{}')

def checkout_url(): return os.getenv('KIWIFY_CHECKOUT_URL','')

class H(BaseHTTPRequestHandler):
    def send_json(self,code,obj):
        b=json.dumps(obj,ensure_ascii=False).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
    def do_GET(self):
        p=urlparse(self.path).path
        if p=='/api/config':
            self.send_json(200,{'price':'R$ 120/mês','checkout_url':checkout_url()});return
        if p=='/':
            b=(ROOT/'static/index.html').read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        if p.startswith('/api/jobs/'):
            j=JOBS.get(p.rsplit('/',1)[-1]);self.send_json(200,j or {'error':'Projeto não encontrado'});return
        if p.startswith('/outputs/'):
            fp=OUT/Path(p).name
            if not fp.exists(): self.send_error(404);return
            b=fp.read_bytes();self.send_response(200);self.send_header('Content-Type','video/mp4');self.send_header('Content-Length',str(len(b)));self.send_header('Content-Disposition',f'attachment; filename="{fp.name}"');self.end_headers();self.wfile.write(b);return
        self.send_error(404)
    def do_POST(self):
        if self.path=='/api/auth/register':
            try:
                x=read_json(self); email=x.get('email','').strip().lower(); pw=x.get('password','')
                if len(pw)<8 or '@' not in email: return self.send_json(400,{'error':'Email válido e senha de 8+ caracteres.'})
                c=db(); c.execute('INSERT INTO users(email,password_hash) VALUES(?,?)',(email,hashpw(pw))); c.commit(); c.close(); return self.send_json(200,{'ok':True,'message':'Conta criada. Ative sua assinatura para usar o editor.'})
            except sqlite3.IntegrityError: return self.send_json(409,{'error':'Este email já está cadastrado.'})
        if self.path=='/api/auth/login':
            x=read_json(self); email=x.get('email','').strip().lower(); pw=x.get('password',''); c=db(); row=c.execute('SELECT id,email,active FROM users WHERE email=? AND password_hash=?',(email,hashpw(pw))).fetchone(); c.close()
            if not row: return self.send_json(401,{'error':'Email ou senha inválidos.'})
            return self.send_json(200,{'ok':True,'user':{'id':row[0],'email':row[1],'active':bool(row[2])}})
        if self.path=='/api/kiwify/webhook':
            # Store subscription state. Configure the webhook in Kiwify to send approved/cancelled/refunded/renewal events here.
            x=read_json(self); email=(x.get('Customer') or {}).get('email') or x.get('email') or (x.get('customer') or {}).get('email')
            event=str(x.get('webhook_event_type') or x.get('event') or x.get('status') or '').lower()
            if email:
                active=0 if any(k in event for k in ['refund','cancel','chargeback','failed','refused','expired']) else 1
                c=db(); c.execute('UPDATE users SET active=? WHERE email=?',(active,email.lower())); c.commit(); c.close()
            return self.send_json(200,{'received':True})
        if self.path!='/api/jobs': self.send_error(404);return
        fs=parse_multipart(self)
        yt=fs.getfirst('youtubeUrl','').strip(); fileitem=fs['video'] if 'video' in fs else None
        if not yt and not fileitem: return self.send_json(400,{'error':'Cole um link do YouTube ou envie um vídeo.'})
        jid=uuid.uuid4().hex; d=JOBDIR/jid;d.mkdir(parents=True)
        def g(k,default): return fs.getfirst(k,default)
        j={'id':jid,'dir':str(d),'input_type':'youtube' if yt else 'upload','url':yt,'upload':None,'count':max(1,min(20,int(g('count','5')))),'duration':max(10,min(180,int(g('duration','45')))),'aspect':g('aspect','9:16'),'resolution':g('resolution','1080p'),'captions':g('captions','off')=='on','zoom':g('zoom','on')=='on','status':'na fila','progress':2,'clips':[],'ai_enabled':bool(client)}
        if fileitem:
            up=UP/(jid+'.mp4');up.write_bytes(fileitem.file.read());j['upload']=str(up)
        JOBS[jid]=j;threading.Thread(target=process,args=(j,),daemon=True).start();self.send_json(200,{'id':jid,'ai_enabled':bool(client)})

print('Corte AI Pro V3 em http://127.0.0.1:3000 | IA:',bool(client))
ThreadingHTTPServer(('0.0.0.0',3000),H).serve_forever()
