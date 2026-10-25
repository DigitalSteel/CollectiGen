"""Local content-free overhead learning, separate from inference/benchmarks."""
from dataclasses import asdict
import os,platform
from pathlib import Path
from .inference_timing import InferenceTimingStore

def storage_device(path):
    try:return Path(path).stat().st_dev
    except (OSError,ValueError,TypeError):return None

def cpu_profile():
    processor=platform.processor()
    return {'processor':processor,'machine':platform.machine(),'cpus':os.cpu_count()} if processor else None

def cost_identity(store,context):
    if store is None:return None
    return store,InferenceTimingStore.fingerprint({'overhead_schema':1,**context})

def observe_cost(identity,seconds,units=1):
    if identity is None:return
    try:identity[0].observe_cost(identity[1],seconds,units)
    except Exception:pass  # optional telemetry cannot invalidate canonical saves

def cost_range(identity,remaining):
    if remaining<=0:return 0.0,0.0,0
    if identity is None:return None,None,0
    try:
        estimate=identity[0].estimate_cost(identity[1],remaining)
        return estimate.low_seconds,estimate.high_seconds,estimate.batches
    except Exception:return None,None,0

class RuntimeCostLearning:
    def begin_cost_request(self):
        self._pending_load_costs=[];self._loading_cost_identity=None;self._saving_cost_identity=None
        self._cost_scope_device=None;self._load_cost_expected={}

    def set_cost_scope(self,paths):
        devices={storage_device(Path(path).parent) for path in paths}
        self._cost_scope_device=next(iter(devices)) if len(devices)==1 and None not in devices else None

    def _cost_store_for(self,verified):
        if getattr(self,'_timing_store',None) is None:return None
        if verified:return self._timing_store
        if getattr(self,'_ephemeral_cost_store',None) is None:self._ephemeral_cost_store=InferenceTimingStore()
        return self._ephemeral_cost_store

    def prepare_loading_cost(self,spec):
        if getattr(self,'_timing_store',None) is None or not hasattr(spec,'key'):return None
        try:
            runtime=asdict(spec.key);hardware=cpu_profile()
            if 'cuda' in spec.key.provider.casefold():
                hardware=None;probe=getattr(self.runtime_manager,'hardware_probe',None)
                if probe is not None:
                    profile=probe(spec.key.device_index)
                    if profile is not None:hardware={'gpu':profile.name,'memory_mib':profile.total_memory_mib,'index':spec.key.device_index}
            model_device=storage_device(self.model_dir)
            store=self._cost_store_for(hardware is not None and model_device is not None)
            identity=cost_identity(store,{'phase':'model-load','runtime':runtime,'hardware':hardware,'model_storage':model_device})
            self._loading_cost_identity=identity
            if not hasattr(self,'_load_cost_expected'):self._load_cost_expected={}
            if identity is not None:self._load_cost_expected[identity[1]]=spec.key
            return identity
        except Exception:return None

    def note_loading_cost(self,identity,seconds,provider):
        if identity is not None:
            if not hasattr(self,'_pending_load_costs'):self._pending_load_costs=[]
            self._pending_load_costs.append((identity,seconds,provider))

    def finish_loading_cost(self,success):
        pending=getattr(self,'_pending_load_costs',[]);self._pending_load_costs=[]
        if not success:return
        actual=getattr(self,'_provider','')
        device=None
        if not actual:
            try:
                device=self._benchmark_device();actual=device.get('kind','')
            except Exception:actual=''
        def normalize(value):
            text=str(value).casefold()
            return 'cuda' if 'cuda' in text else 'cpu' if 'cpu' in text else text
        for identity,seconds,provider in pending:
            expected=getattr(self,'_load_cost_expected',{}).get(identity[1])
            if device is not None and expected is not None:
                if device.get('quantization')!=expected.quantization:continue
                if expected.dtype not in {'','auto'} and device.get('precision')!=expected.dtype:continue
                if normalize(actual)=='cuda' and device.get('index',expected.device_index)!=expected.device_index:continue
            if normalize(actual)==normalize(provider):observe_cost(identity,seconds)

    def remaining_loading_cost(self):return cost_range(getattr(self,'_loading_cost_identity',None),1)

    def saving_identity(self,record,analysis,auto_merge,refresh_matching):
        if getattr(self,'_timing_store',None) is None:return None
        try:
            volume=storage_device(record.sidecar_path.parent);hardware=cpu_profile()
            # Only numeric shape buckets retained; never the annotation/prose.
            import json
            payload=len(json.dumps(record.annotation.to_dict(),ensure_ascii=True).encode('utf-8'))
            output=len(analysis.plain_language or '')+len(analysis.tags)*32
            history=record.history_context is not None
            history_root=getattr(getattr(getattr(record.history_context,'store',None),'scope',None),'history_root',None)
            history_device=storage_device(history_root) if history else 'none'
            verified=volume is not None and hardware is not None and history_device is not None
            self._saving_context_verified=verified
            extension=record.image_path.suffix.casefold()
            if extension not in {'.png','.jpg','.jpeg','.webp','.gif','.bmp','.tif','.tiff'}:extension='other'
            context={'phase':'result-save','kind':analysis.kind,'model':analysis.model_id,'revision':analysis.model_revision,
                    'storage':volume,'hardware':hardware,'payload_bucket':payload.bit_length(),'output_bucket':max(1,output).bit_length(),
                    'history':history,'history_storage':history_device,'image_format':extension,'auto_merge':bool(auto_merge),'refresh':bool(refresh_matching)}
            identity=cost_identity(self._cost_store_for(verified),context)
            self._saving_cost_identity=identity
            self._saving_cost_device=volume
            return identity
        except Exception:return None

    def remaining_saving_cost(self,remaining):
        if not getattr(self,'_saving_context_verified',False) or getattr(self,'_cost_scope_device',None) is None or getattr(self,'_saving_cost_device',None)!=self._cost_scope_device:
            return None,None,0
        return cost_range(getattr(self,'_saving_cost_identity',None),remaining)
