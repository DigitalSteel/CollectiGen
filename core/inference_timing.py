"""Local, content-free observations; controlled benchmarks remain separate."""
from dataclasses import dataclass
import hashlib,json,math,sqlite3,threading,time
from pathlib import Path
from statistics import median

@dataclass(frozen=True)
class CaptionEstimate:
    detail: str
    low_seconds: float|None=None
    high_seconds: float|None=None

@dataclass(frozen=True)
class InferenceEstimate:
    batches: int
    images: int
    low_seconds: float|None=None
    high_seconds: float|None=None

    def text(self,remaining):
        count=f'{self.batches} inference batches / {self.images} images'
        if remaining<=0:return 'WD inference ETA: no remaining images'
        if self.low_seconds is None:return f'WD inference ETA: collecting timings ({count})'
        low=max(0,math.floor(self.low_seconds/5)*5);high=max(5,math.ceil(self.high_seconds/5)*5)
        duration=f'under {high}s' if low==0 else f'~{low}–{high}s'
        return f'WD inference ETA: {duration} for up to {remaining} remaining images ({count}); cached work may shorten this'

class InferenceTimingStore:
    """A bounded statistical window, never a limit on user work.

    Rows contain only an opaque configuration fingerprint, timestamp, successful
    inference seconds and input count. No filenames, prompts, tags or results.
    Construction is inert. Failed storage falls back to this process's samples.
    """
    def __init__(self,path=None,*,wall_clock=time.time,retention_seconds=30*86400):
        self.path=Path(path) if path is not None else None
        self.wall_clock=wall_clock;self.retention_seconds=retention_seconds
        self._memory={};self._lock=threading.RLock();self.persistence_available=self.path is not None

    @staticmethod
    def fingerprint(configuration):
        return hashlib.sha256(json.dumps(configuration,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('utf-8')).hexdigest()

    def _connect(self,create):
        if not create and (self.path is None or not self.path.is_file()):return None
        if create:self.path.parent.mkdir(parents=True,exist_ok=True)
        connection=sqlite3.connect(self.path,timeout=.2)
        version=connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,1):
            connection.close();raise sqlite3.DatabaseError('Unsupported timing storage version')
        if create:
            connection.execute('CREATE TABLE IF NOT EXISTS samples (key TEXT NOT NULL, at REAL NOT NULL, seconds REAL NOT NULL, images INTEGER NOT NULL)')
            connection.execute('CREATE INDEX IF NOT EXISTS timing_key ON samples(key,at)')
            connection.execute('PRAGMA user_version=1')
        return connection

    def observe(self,key,seconds,images):
        if (not isinstance(key,str) or len(key)!=64 or any(ch not in '0123456789abcdef' for ch in key)
            or type(images) is not int or images<=0 or isinstance(seconds,bool)
            or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<=0):return False
        now=self.wall_clock()
        with self._lock:
            values=self._memory.setdefault(key,[]);values.append((now,float(seconds),images));del values[:-48]
            if self.path is not None:
                connection=None
                try:
                    connection=self._connect(True)
                    with connection:
                        connection.execute('INSERT INTO samples VALUES(?,?,?,?)',(key,now,float(seconds),images))
                        connection.execute('DELETE FROM samples WHERE at<?',(now-self.retention_seconds,))
                        connection.execute('DELETE FROM samples WHERE key=? AND rowid NOT IN (SELECT rowid FROM samples WHERE key=? ORDER BY at DESC,rowid DESC LIMIT 48)',(key,key))
                    self.persistence_available=True
                except (OSError,sqlite3.Error):self.persistence_available=False
                finally:
                    if connection is not None:connection.close()
        return True

    def estimate(self,key,remaining):
        now=self.wall_clock()
        with self._lock:
            rows=list(self._memory.get(key,()))
            if self.path is not None and self.persistence_available:
                connection=None
                try:
                    connection=self._connect(False)
                    if connection is not None:rows=list(reversed(connection.execute('SELECT at,seconds,images FROM samples WHERE key=? ORDER BY at DESC,rowid DESC LIMIT 48',(key,)).fetchall()))
                except (OSError,sqlite3.Error):self.persistence_available=False
                finally:
                    if connection is not None:connection.close()
        valid=[(seconds,images) for at,seconds,images in rows if now-self.retention_seconds<=at<=now+1
               and isinstance(seconds,(int,float)) and math.isfinite(seconds) and seconds>0 and type(images) is int and images>0]
        count=len(valid);images=sum(units for _,units in valid)
        if count<3:return InferenceEstimate(count,images)
        values=sorted(seconds/units for seconds,units in valid);middle=median(values)
        # Empirical spread plus a minimum uncertainty margin; this is not a
        # confidence interval or an assumption that memory contention is gone.
        low=min(middle*.75,values[math.floor((count-1)*.1)])
        high=max(middle*1.25,values[math.ceil((count-1)*.9)],max(seconds/units for seconds,units in valid[-3:])*1.25)
        return InferenceEstimate(count,images,low*max(0,remaining),high*max(0,remaining))


    def observe_cost(self,key,seconds,units=1):
        if (not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key)
                or isinstance(seconds,bool) or not isinstance(seconds,(int,float))
                or not math.isfinite(seconds) or seconds<=0 or type(units) is not int or units<=0):return False
        now=self.wall_clock()
        with self._lock:
            if not hasattr(self,'_cost_memory'):self._cost_memory={}
            values=self._cost_memory.setdefault(key,[]);values.append((now,float(seconds),units));del values[:-48]
            connection=None
            if self.path is not None:
                try:
                    connection=self._connect(True)
                    with connection:
                        connection.execute('CREATE TABLE IF NOT EXISTS cost_samples (key TEXT NOT NULL, at REAL NOT NULL, seconds REAL NOT NULL, units INTEGER NOT NULL)')
                        connection.execute('INSERT INTO cost_samples VALUES(?,?,?,?)',(key,now,float(seconds),units))
                        connection.execute('DELETE FROM cost_samples WHERE at<?',(now-self.retention_seconds,))
                        connection.execute('DELETE FROM cost_samples WHERE key=? AND rowid NOT IN (SELECT rowid FROM cost_samples WHERE key=? ORDER BY at DESC,rowid DESC LIMIT 48)',(key,key))
                    self.persistence_available=True
                except (OSError,sqlite3.Error):self.persistence_available=False
                finally:
                    if connection is not None:connection.close()
        return True

    def estimate_cost(self,key,remaining):
        now=self.wall_clock()
        with self._lock:
            rows=list(getattr(self,'_cost_memory',{}).get(key,()))
            connection=None
            if self.path is not None and self.persistence_available:
                try:
                    connection=self._connect(False)
                    if connection is not None and connection.execute("SELECT 1 FROM sqlite_master WHERE name='cost_samples'").fetchone():
                        rows=list(reversed(connection.execute('SELECT at,seconds,units FROM cost_samples WHERE key=? ORDER BY at DESC,rowid DESC LIMIT 48',(key,)).fetchall()))
                except (OSError,sqlite3.Error):self.persistence_available=False
                finally:
                    if connection is not None:connection.close()
        valid=[(sec,units) for at,sec,units in rows if now-self.retention_seconds<=at<=now+1
                and isinstance(sec,(int,float)) and math.isfinite(sec) and sec>0 and type(units) is int and units>0]
        if len(valid)<3:return InferenceEstimate(len(valid),sum(n for _,n in valid))
        rates=sorted(sec/units for sec,units in valid);mid=median(rates)
        low=min(mid*.65,rates[math.floor((len(rates)-1)*.1)])
        high=max(mid*1.5,rates[math.ceil((len(rates)-1)*.9)],max(sec/units for sec,units in valid[-3:])*1.5)
        return InferenceEstimate(len(valid),sum(n for _,n in valid),low*max(0,remaining),high*max(0,remaining))

    def observe_caption(self,key,seconds,tokens=None):
        """Successful image generation; tokens are re-tokenized completion counts.

        Missing counts remain missing, never replaced with word counts.
        """
        if (not isinstance(key,str) or len(key)!=64 or any(c not in '0123456789abcdef' for c in key)
                or isinstance(seconds,bool) or not isinstance(seconds,(int,float))
                or not math.isfinite(seconds) or seconds<=0):return False
        if type(tokens) is not int or tokens<=0:tokens=None
        now=self.wall_clock()
        with self._lock:
            memory=getattr(self,'_caption_memory',None)
            if memory is None:self._caption_memory=memory={}
            values=memory.setdefault(key,[]);values.append((now,float(seconds),tokens));del values[:-48]
            connection=None
            if self.path is not None:
                try:
                    connection=self._connect(True)
                    with connection:
                        connection.execute('CREATE TABLE IF NOT EXISTS caption_samples (key TEXT NOT NULL, at REAL NOT NULL, seconds REAL NOT NULL, tokens INTEGER)')
                        connection.execute('INSERT INTO caption_samples VALUES(?,?,?,?)',(key,now,float(seconds),tokens))
                        connection.execute('DELETE FROM caption_samples WHERE at<?',(now-self.retention_seconds,))
                        connection.execute('DELETE FROM caption_samples WHERE key=? AND rowid NOT IN (SELECT rowid FROM caption_samples WHERE key=? ORDER BY at DESC,rowid DESC LIMIT 48)',(key,key))
                    self.persistence_available=True
                except (OSError,sqlite3.Error):self.persistence_available=False
                finally:
                    if connection is not None:connection.close()
        return True

    def caption_detail(self,key,remaining,max_tokens):
        return self.caption_estimate(key,remaining,max_tokens).detail

    def caption_estimate(self,key,remaining,max_tokens):
        now=self.wall_clock()
        with self._lock:
            rows=list(getattr(self,'_caption_memory',{}).get(key,()))
            connection=None
            if self.path is not None and self.persistence_available:
                try:
                    connection=self._connect(False)
                    if connection is not None and connection.execute("SELECT 1 FROM sqlite_master WHERE name='caption_samples'").fetchone():
                        rows=list(reversed(connection.execute('SELECT at,seconds,tokens FROM caption_samples WHERE key=? ORDER BY at DESC,rowid DESC LIMIT 48',(key,)).fetchall()))
                except (OSError,sqlite3.Error):self.persistence_available=False
                finally:
                    if connection is not None:connection.close()
        values=[(seconds,tokens) for at,seconds,tokens in rows if now-self.retention_seconds<=at<=now+1
                and isinstance(seconds,(int,float)) and math.isfinite(seconds) and seconds>0]
        n=len(values);known=[(sec,tok) for sec,tok in values if type(tok) is int and tok>0]
        basis=f'{n} successful images; completion tokens available for {len(known)}/{n}'
        if remaining<=0:return CaptionEstimate('JoyCaption inference ETA: no remaining images; '+basis,0,0)
        if n<3:return CaptionEstimate('JoyCaption inference ETA: collecting timings ('+basis+')')
        durations=sorted(sec for sec,_ in values);mid=median(durations)
        low=min(mid*.65,durations[math.floor((n-1)*.1)])
        high=max(mid*1.5,durations[math.ceil((n-1)*.9)],max(sec for sec,_ in values[-3:])*1.5)
        token_detail='future caption length unknown; token-based extrapolation unavailable'
        if len(known)>=3:
            rates=sorted(sec/tok for sec,tok in known)
            # Conservative envelope, not a linear-generation guarantee. Future
            # lengths and contention are unknown; prefill is included in timings.
            high=max(high,rates[math.ceil((len(rates)-1)*.9)]*max_tokens*1.25,max(sec/tok for sec,tok in known[-3:])*max_tokens*1.25)
            lengths=[tok for _,tok in known]
            token_detail=f'observed completion tokens {min(lengths)}-{max(lengths)}; envelope allows requested max {max_tokens}; re-tokenized counts, not exact generated IDs'
        lo=max(0,math.floor(low*remaining/5)*5);hi=max(5,math.ceil(high*remaining/5)*5)
        duration=f'under {hi}s' if lo==0 else f'~{lo}-{hi}s'
        return CaptionEstimate(f'JoyCaption inference ETA: {duration} for up to {remaining} remaining images ({basis}); {token_detail}; future length/contention uncertain; cached work may shorten this',low*remaining,high*remaining)


def compact_wd_progress(message):
    """Short visible summary; the caller retains full accessible detail."""
    marker="WD inference ETA:"
    if marker not in message:return None
    import re
    header=message.split(";",1)[0].casefold()
    model=next((label for value,label in (("eva02","EVA02"),("vit","ViT"),("swinv2","SwinV2"),("convnext","ConvNext")) if value in header),"WD")
    provider="CUDA" if "cuda" in header else "CPU" if "cpu" in header else ""
    body=message.split(marker,1)[1].strip()
    match=re.search(r"(\d+) inference batches",body);count=match.group(1) if match else "0"
    if body.startswith("collecting"):estimate=f"Learning ETA ({count} samples)"
    elif body.startswith("unavailable"):estimate="ETA unavailable"
    elif body.startswith("no remaining"):estimate="Inference complete"
    else:
        duration=body.split(" for up to",1)[0].split(";",1)[0].strip()
        estimate=f"ETA {duration} ({count} samples)"
    if header.startswith("loading"):estimate="Loading; "+estimate
    return " • ".join(value for value in (model,provider,estimate) if value)


def has_adaptive_inference_eta(message):
    return any(marker in message for marker in ('WD inference ETA:','JoyCaption inference ETA:'))

def compact_inference_progress(message):
    from .task_eta import compact_task_eta
    stage=_compact_inference_progress(message)
    task=compact_task_eta(message)
    if stage and task:return stage+' \u2022 '+task
    if task:
        phase=message.split('; Task ETA:',1)[0].rsplit(': ',1)[0]
        return phase+' \u2022 '+task if phase and not phase.startswith('Task ETA:') else task
    return stage

def _compact_inference_progress(message):
    if 'JoyCaption inference ETA:' not in message:return compact_wd_progress(message)
    import re
    header=message.split(';',1)[0].casefold()
    provider='CUDA' if 'cuda' in header else 'CPU' if 'cpu' in header else ''
    body=message.split('JoyCaption inference ETA:',1)[1].strip()
    match=re.search(r'(\d+) successful images',body);count=match.group(1) if match else '0'
    if body.startswith('collecting'):label=f'Learning ETA ({count} images)'
    elif body.startswith('unavailable'):label='ETA unavailable'
    elif body.startswith('no remaining'):label='Inference complete'
    else:label=f"ETA {body.split(' for up to',1)[0]} ({count} images)"
    if header.startswith('loading'):label='Loading; '+label
    return ' \u2022 '.join(value for value in ('JoyCaption',provider,label) if value)
