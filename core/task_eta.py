"""Per-request remaining-cost aggregation; no clock, scheduler or telemetry writes."""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class RemainingCost:
    label: str
    units: int
    low: float|None=None
    high: float|None=None

    @property
    def known(self):
        return (self.units==0 or (isinstance(self.low,(int,float)) and isinstance(self.high,(int,float))
                and math.isfinite(self.low) and math.isfinite(self.high) and 0<=self.low<=self.high))

class RemainingTaskEstimate:
    def __init__(self):self.parts={};self.state='running'

    def update(self,key,label,units,low=None,high=None):
        self.parts[key]=RemainingCost(label,max(0,int(units)),low,high)

    def complete(self,*keys):
        for key in keys:
            if key in self.parts:
                old=self.parts[key];self.parts[key]=RemainingCost(old.label,0,0,0)

    def text(self):
        if self.state in {'cancelled','held','queued'}:
            return f'Task ETA: {self.state}; completion time unknown; estimates do not count down while waiting'
        pending=[part for part in self.parts.values() if part.units]
        if not pending:return 'Task ETA: no remaining planned work'
        unknown=[part.label for part in pending if not part.known]
        known=[part for part in pending if part.known]
        missing='; unknown costs: '+', '.join(dict.fromkeys(unknown)) if unknown else ''
        if not known:return 'Task ETA: unknown'+missing
        lo=max(0,math.floor(sum(part.low for part in known)/5)*5)
        hi=max(5,math.ceil(sum(part.high for part in known)/5)*5)
        duration=f'under {hi}s' if lo==0 else f'~{lo}-{hi}s'
        if unknown:return f'Task ETA: partial {duration} known remaining work + {len(unknown)} unknown costs'+missing+'; qualified costs: '+', '.join(part.label for part in known)
        return f'Task ETA: {duration} remaining planned work; uncertain, updated as work completes; qualified costs: '+', '.join(part.label for part in known)

def compact_task_eta(message):
    if 'Task ETA:' not in message:return None
    body=message.split('Task ETA:',1)[1].split(';',1)[0].strip()
    if body.startswith('partial'):
        import re
        count=re.search(r'\+ (\d+) unknown',body)
        duration=body.split(' known remaining work',1)[0]
        return 'Task: '+duration+(f' + {count.group(1)} unknown' if count else ' + unknown')
    if body.startswith(('held','queued','cancelled','unknown')):return 'Task: '+body
    if body.startswith('no remaining'):return 'Task: finished'
    return 'Task: '+body.split(' remaining planned work',1)[0]
