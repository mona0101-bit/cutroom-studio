"""Local video worker. JSON in on stdin; progress and results as JSON lines."""
import sys, os, json, re, html, subprocess, pathlib, math, zipfile, hashlib, shutil
ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / '.runtime' / 'python'))
os.environ['HF_HOME'] = str(ROOT / '.runtime' / 'models')
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
os.environ['PYTHONIOENCODING'] = 'utf-8'

def emit(**data):
    print(json.dumps(data, ensure_ascii=True), flush=True)

class Log:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): print(msg, file=sys.stderr, flush=True)

def ydl_options(folder):
    import imageio_ffmpeg
    opts = {'quiet': True, 'no_warnings': True, 'logger': Log(), 'noplaylist': True,
            'socket_timeout': 25, 'retries': 2, 'extractor_retries': 1, 'concurrent_fragment_downloads': 4,
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
            'outtmpl': str(folder / 'source.%(ext)s')}
    node = os.environ.get('CUTROOM_NODE')
    if node: opts['js_runtimes'] = {'node': {'path': node}}
    return opts

def normalize(cues):
    result = []
    for c in sorted(cues, key=lambda x: x['start']):
        text = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', c['text']))).strip()
        if not text or re.fullmatch(r'\[[^]]+\]', text): continue
        start, end = max(0, float(c['start'])), float(c['end'])
        if end <= start: continue
        if result and start <= result[-1]['start']:
            if result[-1]['text'] != text: result[-1]['text'] += ' ' + text
            result[-1]['end'] = max(end, result[-1]['end'])
        else: result.append({'start': round(start, 3), 'end': round(end, 3), 'text': text})
    return result

def captions(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    tracks = list(YouTubeTranscriptApi().list(video_id))
    if not tracks: raise ValueError('No caption track')
    tracks.sort(key=lambda t: (not t.language_code.startswith('en'), t.is_generated))
    track = tracks[0]
    data = track.fetch()
    return normalize([{'start': c.start, 'end': c.start+c.duration, 'text': c.text} for c in data]), track.language_code

def get_metadata(video_id):
    import requests
    r = requests.get('https://www.youtube.com/oembed', params={'url': f'https://www.youtube.com/watch?v={video_id}', 'format': 'json'}, timeout=15)
    r.raise_for_status()
    return r.json()

def download_source(video_id, folder, audio=False):
    from yt_dlp import YoutubeDL
    # A separate HQ cache prevents old 720p downloads being silently reused.
    output = folder / ('audio' if audio else 'source-hq')
    opts = ydl_options(folder)
    opts.update({'format': 'bestaudio/best' if audio else 'bestvideo[height<=2160]+bestaudio/best[height<=2160]/best',
                 'merge_output_format': 'mp4',
                 'outtmpl': str(output) + '.%(ext)s'})
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
        file = pathlib.Path(ydl.prepare_filename(info))
        if not audio and output.with_suffix('.mp4').exists(): file = output.with_suffix('.mp4')
    if not file.exists(): raise RuntimeError('The source download did not finish.')
    return file, info

def analyze(data, folder):
    video_id = data['videoId']
    emit(progress='Reading the video’s captions…', percent=12)
    try: meta = get_metadata(video_id)
    except Exception: meta = {'title': 'YouTube video'}
    duration = None
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL(ydl_options(folder)) as ydl:
            metadata = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
        if metadata.get('is_live'): raise ValueError('Wait until the live stream has finished.')
        duration = metadata.get('duration')
        meta['title'] = metadata.get('title',meta['title'])
    except (ValueError, PermissionError): raise
    except Exception as exc: print('Duration lookup: '+str(exc),file=sys.stderr)
    try:
        cues, language = captions(video_id)
        method = 'YouTube captions'
        if not cues: raise ValueError('Caption track was empty')
    except PermissionError: raise
    except Exception as exc:
        print('Caption retrieval: '+str(exc), file=sys.stderr)
        emit(progress='Captions unavailable. Downloading audio to transcribe…', percent=25)
        try: audio, details = download_source(video_id, folder, audio=True)
        except PermissionError: raise
        except Exception as err:
            detail = str(err).lower()
            if 'unavailable' in detail or 'removed' in detail:
                raise RuntimeError('YouTube reports that this video is unavailable. Check that the link opens on YouTube, or try another link.') from err
            raise RuntimeError('YouTube did not allow this processor to retrieve the video. It may require sign-in or be restricted. Try another public link.') from err
        meta['title'] = details.get('title', meta['title'])
        duration = details.get('duration') or duration
        emit(progress='Transcribing speech locally. The first run also downloads the speech model…', percent=40)
        from faster_whisper import WhisperModel
        model = WhisperModel('base', device='cpu', compute_type='int8', download_root=str(ROOT / '.runtime' / 'models'))
        segments, info = model.transcribe(str(audio), beam_size=3, vad_filter=True)
        cues = []
        for segment in segments:
            cues.append({'start': segment.start, 'end': segment.end, 'text': segment.text})
            if len(cues) % 20 == 0:
                emit(progress=f'Transcribing speech · {int(segment.end//60)} minutes read…', percent=min(90, 40+int(50*segment.end/max(1,info.duration))))
        cues, language, method = normalize(cues), info.language, 'Local speech transcription'
    if not cues and not duration: raise RuntimeError('No spoken words or video duration could be read.')
    result = {'videoId': video_id, 'title': meta.get('title', 'YouTube video'), 'author': meta.get('author_name', ''),
              'cues': cues, 'language': language, 'method': method, 'duration': duration or cues[-1]['end'], 'durationExact': bool(duration)}
    (folder / 'transcript.json').write_text(json.dumps(result), encoding='utf-8')
    emit(result=result)

def ass_time(value):
    cs = max(0, round(value*100))
    return f'{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}'

def analyze_twitch(data, folder):
    from yt_dlp import YoutubeDL
    import av
    url=data['sourceUrl']
    if not re.fullmatch(r'https://clips\.twitch\.tv/[A-Za-z0-9_-]+',url): raise ValueError('Invalid Twitch clip link.')
    emit(progress='Downloading your Twitch clip…',percent=15)
    opts=ydl_options(folder)
    opts.update({'format':'best[ext=mp4]/best','outtmpl':str(folder/'source-hq.%(ext)s')})
    with YoutubeDL(opts) as ydl:
        details=ydl.extract_info(url,download=True)
        source=pathlib.Path(ydl.prepare_filename(details))
    if source.suffix!='.mp4': raise ValueError('This Twitch clip has no supported MP4 download.')
    with av.open(str(source)) as media:
        duration=(media.duration or 0)/1000000 or details.get('duration')
        has_audio=bool(media.streams.audio)
    if not duration or not math.isfinite(duration): raise ValueError('Could not read the Twitch clip duration.')
    cues=[];language='und';note=''
    if has_audio:
        try:
            emit(progress='Reading speech in your Twitch clip…',percent=45)
            from faster_whisper import WhisperModel
            model=WhisperModel('base',device='cpu',compute_type='int8',download_root=str(ROOT/'.runtime/models'))
            segments,info=model.transcribe(str(source),beam_size=3,vad_filter=True)
            cues=normalize([{'start':s.start,'end':s.end,'text':s.text} for s in segments]);language=info.language
        except Exception as exc:
            print('Twitch transcription: '+str(exc),file=sys.stderr)
            note='Speech transcription was unavailable. You can still download or split the clip without captions.'
    result={'videoId':'twitch_clip','provider':'twitch','sourceUrl':url,'sourceFile':source.name,'title':details.get('title','Twitch clip'),'author':details.get('uploader',''),'cues':cues,'language':language,'method':'Twitch clip · local speech transcription' if cues else 'Twitch clip','duration':duration,'durationExact':True,'note':note}
    (folder/'transcript.json').write_text(json.dumps(result),encoding='utf-8')
    emit(result=result)

def reuse_file(source, destination):
    try: os.link(source, destination)
    except OSError: shutil.copy2(source, destination)

def render_video(data, folder, report=emit):
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    project = pathlib.Path(data['projectPath'])
    transcript = json.loads((project / 'transcript.json').read_text('utf-8'))
    start, end = float(data['start']), float(data['end'])
    if not (math.isfinite(start) and math.isfinite(end) and 0<=start<end<=transcript['duration']+1):
        raise ValueError('Choose a valid range within the video.')
    report(progress='Getting the best available source, up to 4K…', percent=10)
    sources = list(project.glob('source-hq.*'))
    source = next((p for p in sources if p.suffix in ['.mp4', '.webm', '.mkv', '.mov', '.m4v']), None)
    if source is None: source, _ = download_source(transcript['videoId'], project)
    # Include the source, transcript, timing, style and renderer version in the key.
    identity = {'version': 2, 'source': [source.name, source.stat().st_size, source.stat().st_mtime_ns],
                'transcript': transcript, 'start': start, 'end': end, 'style': data.get('style','lime')}
    key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    cache = project/'render-cache'/key
    if (cache/'render.json').exists() and (cache/'clip.mp4').exists():
        report(progress='Reusing your finished clip—no encoding needed…',percent=95)
        reuse_file(cache/'clip.mp4',folder/'clip.mp4')
        result = json.loads((cache/'render.json').read_text('utf-8'))
        result['cached'] = True
        (folder/'render.json').write_text(json.dumps(result),encoding='utf-8')
        return result
    import av
    with av.open(str(source)) as media:
        video = media.streams.video[0]
        source_width, source_height = video.width, video.height
        fps = min(60, max(1, float(video.average_rate or 30)))
        has_audio = bool(media.streams.audio)
        # Keep real timing and avoid duplicating low-FPS footage to claim smoother video.
        fps_string = str(video.average_rate or 30) if float(video.average_rate or 30)<=60 else '60'
    colors = {'lime': '&H0069FAD5', 'white': '&H00FFFFFF', 'pink': '&H00C59EFF'}
    accent = colors.get(data.get('style'), colors['lime'])
    ass = '[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
    ass += f"Style: Default,Arial,68,{accent},&H00FFFFFF,&H00101010,&H70000000,-1,0,0,0,100,100,0,0,1,5,2,2,100,200,420,1\n"
    ass += '\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    active_cues = [c for c in transcript['cues'] if c['start']<end and c['end']>start]
    def clean(text):
        return text.replace('\\','').replace('{','').replace('}','').replace('\n',' ')
    for ci, c in enumerate(active_cues):
        if c['start']>=end or c['end']<=start: continue
        words = c['text'].split()
        groups = [words[i:i+4] for i in range(0,len(words),4)]
        next_start = active_cues[ci+1]['start'] if ci+1<len(active_cues) else end
        lo, hi = c['start']-start, min(c['end'],next_start)-start
        if hi<=lo: continue
        for i, group in enumerate(groups):
            a, b = lo+(hi-lo)*i/len(groups), lo+(hi-lo)*(i+1)/len(groups)
            if b<=0 or a>=end-start: continue
            a,b=max(0,a),min(end-start,b)
            # Approximate word highlights within each caption line; not forced alignment.
            word_cs = max(1,round((b-a)*100/len(group)))
            text = ' '.join('{\\kf'+str(word_cs)+'}'+clean(word) for word in group)
            pop = r'{\fscx96\fscy96\t(0,100,\fscx100\fscy100)\fad(40,60)}'
            ass += f'Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,{pop}{text}\n'
    (folder/'captions.ass').write_text(ass, encoding='utf-8')
    report(progress='Rendering Full HD with a moving backdrop and animated captions…', percent=55)
    # Two synchronized copies: full-frame subject plus a softly blurred edge-to-edge backdrop.
    # Blur at a smaller resolution to avoid wasting CPU on pixels that will be blurred away.
    vf = "[0:v]split=2[bg][fg];[bg]scale=270:480:force_original_aspect_ratio=increase:flags=lanczos,crop=270:480,boxblur=12:2,eq=brightness=-0.06:saturation=0.85,scale=1080:1920:flags=bicubic,setsar=1[back];[fg]scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,setsar=1[front];[back][front]overlay=(W-w)/2:(H-h)/2:shortest=1,subtitles=captions.ass,format=yuv420p[out]"
    command = [ffmpeg,'-y','-nostdin','-hide_banner','-loglevel','error','-ss',str(start),'-i',str(source),'-t',str(end-start),'-filter_complex',vf,'-map','[out]','-map','0:a:0?','-r',fps_string,'-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p']
    if has_audio:
        command += ['-af','loudnorm=I=-16:TP=-1.5:LRA=11','-c:a','aac','-b:a','192k','-ar','48000']
    command += ['-movflags','+faststart',str(folder/'clip.mp4')]
    try:
        subprocess.run(command, cwd=folder, check=True, capture_output=True, timeout=max(1200, min(21600, (end-start)*30+600)))
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.decode('utf-8',errors='replace')[-5000:],file=sys.stderr)
        raise
    result = {'file':'clip.mp4','width':1080,'height':1920,'duration':end-start,'sourceWidth':source_width,'sourceHeight':source_height,'fps':fps,'qualityNote':('The source is low resolution; exporting in Full HD cannot restore missing detail.' if min(source_width,source_height)<720 else 'Full HD export from the highest available source up to 4K.'),'presentation':'Blurred motion background, full source framing, animated captions'}
    (folder/'render.json').write_text(json.dumps(result),encoding='utf-8')
    cache.mkdir(parents=True,exist_ok=True)
    if not (cache/'clip.mp4').exists(): reuse_file(folder/'clip.mp4',cache/'clip.mp4')
    (cache/'render.json').write_text(json.dumps(result),encoding='utf-8')
    return result

def render_batch(data, folder):
    clips = data['clips']
    results = []
    archive = folder/'clips.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as bundle:
        for index,clip in enumerate(clips):
            part=folder/f'part-{index+1:03d}'
            part.mkdir(exist_ok=True)
            def report(**message):
                percent=round(100*(index+message.get('percent',0)/100)/len(clips))
                emit(progress=f"Clip {index+1} of {len(clips)} · {message.get('progress','Rendering…')}",percent=percent)
            result=render_video({**data,**clip},part,report)
            emit(readyPart=index+1)
            filename=f'part-{index+1:03d}.mp4'
            bundle.write(part/'clip.mp4',filename)
            results.append({'part':index+1,'filename':filename,'start':clip['start'],'end':clip['end'],'title':clip.get('title',''),**result})
        bundle.writestr('clip-order.json',json.dumps(results,indent=2))
    result={'file':'clips.zip','count':len(results),'clips':results}
    (folder/'batch.json').write_text(json.dumps(result),encoding='utf-8')
    return result

if __name__=='__main__':
    try:
        data=json.load(sys.stdin)
        folder=pathlib.Path(data['jobPath']).resolve()
        folder.mkdir(parents=True,exist_ok=True)
        if not re.fullmatch(r'[\w-]{11}',data.get('videoId','')): raise ValueError('Invalid YouTube video ID.')
        if data.get('action')=='render': emit(result=render_video(data,folder))
        elif data.get('action')=='batch': emit(result=render_batch(data,folder))
        elif data.get('provider')=='twitch': analyze_twitch(data,folder)
        else: analyze(data,folder)
    except Exception as exc:
        print(repr(exc), file=sys.stderr, flush=True)
        emit(error=('Windows blocked access to the video runtime. Close this studio server and open Start-Cutroom to restart it under your Windows account.' if isinstance(exc,PermissionError) else str(exc) if not isinstance(exc,subprocess.CalledProcessError) else 'The video renderer could not complete this clip. Try a shorter clip.'))
        sys.exit(1)
