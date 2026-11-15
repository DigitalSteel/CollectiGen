"""Injected, read-only external description backend; never selects a provider.

No default dispatcher, credential storage or external model lifecycle hooks.
Context and callbacks are data/seams, not proof of actual-server/JIT admission.
"""
from dataclasses import asdict, dataclass
import hashlib
import math
import unicodedata

from core.ai_interrogation import InterrogationResult, validate_interrogation_result
from core.lmstudio_description import (
    DescriptionError, DescriptionResult, DescriptionStatistics, MAX_DESCRIPTION_CHARS,
    STAT_FIELDS, encode_description,
)
from core.lmstudio_description_transport import DescriptionTransportError, SAFE_CODES
from core.lmstudio_discovery import InstanceConfig, MAX_CONTEXT
from core.lmstudio_image_preparation import (
    ImagePreparationError, PREPARATION_ID, PILLOW_VERSION, check_cancelled,
    prepare_image, verify_source_current,
)

BACKEND_CODES = frozenset("""
invalid_context invalid_dispatch invalid_dispatch_result dispatch_failed
analysis_preparation_failed source_byte_limit source_identity_unavailable
source_unavailable source_changed prepared_image_too_large image_preparation_failed
image_cleanup_failed invalid_cancellation lmstudio_cancelled
""".split()) | SAFE_CODES


class LMStudioBackendError(RuntimeError):
    def __init__(self, code, submission_state="not_sent", *, cleanup_failed=False):
        self.code = code if code in BACKEND_CODES else "dispatch_failed"
        self.submission_state = submission_state if submission_state in {
            "not_sent", "outcome_unknown"} else "outcome_unknown"
        self.cleanup_failed = cleanup_failed is True
        super().__init__(self.code)


class LMStudioBackendCancelled(InterruptedError):
    def __init__(self, submission_state, *, cleanup_failed=False):
        self.code = "lmstudio_cancelled"
        self.submission_state = submission_state if submission_state in {
            "not_sent", "outcome_unknown"} else "outcome_unknown"
        self.cleanup_failed = cleanup_failed is True
        super().__init__(self.code)


def _require(condition, code="invalid_context"):
    if not condition:
        raise LMStudioBackendError(code)


@dataclass(frozen=True, slots=True, repr=False)
class LMStudioContext:
    model_key: str
    expected_instance_id: str
    prompt: str
    temperature: float = 0
    max_output_tokens: int = 256
    model_revision: str | None = None
    quantization: str | None = None
    server_version: str | None = None
    observed_instance_config: InstanceConfig | None = None

    def __post_init__(self):
        invalid = False
        try:
            encode_description(model_key=self.model_key, expected_instance_id=self.expected_instance_id,
                prompt=self.prompt, temperature=self.temperature, max_output_tokens=self.max_output_tokens,
                image_bytes=b"context-validation-only", image_mime="image/png")
        except DescriptionError:
            invalid = True
        _require(not invalid)
        for value in (self.model_revision, self.quantization, self.server_version):
            _require(value is None or (type(value) is str and len(value) <= 512
                and value == value.strip() and not any(unicodedata.category(c) in {
                    "Cc", "Cf", "Cs"} for c in value)))
        config = self.observed_instance_config
        _require(config is None or type(config) is InstanceConfig)
        if config is not None:
            for key, value in asdict(config).items():
                if value is None:
                    continue
                if key in {"flash_attention", "offload_kv_cache_to_gpu"}:
                    _require(type(value) is bool)
                else:
                    _require(type(value) is int and 0 < value <= MAX_CONTEXT)

    def settings(self):
        return dict(provider="lmstudio", protocol="native-v1-chat",
            server_version=self.server_version or None, loaded_instance_id=self.expected_instance_id,
            quantization=self.quantization or None,
            requested_generation=dict(temperature=float(self.temperature),max_output_tokens=self.max_output_tokens),
            observed_instance_config=asdict(self.observed_instance_config) if self.observed_instance_config else None,
            effective_inference_settings="not_observed",
            image_preparation=dict(recipe=PREPARATION_ID,pillow_version=PILLOW_VERSION))


class LMStudioDescriptionBackend:
    _provider = "LM Studio (external)"
    analysis_preparation_phase = "Preparing local-server analysis"

    def __init__(self, context, *, dispatch):
        _require(type(context) is LMStudioContext)
        _require(callable(dispatch), "invalid_dispatch")
        self._context = context
        self._dispatch = dispatch

    @property
    def model_id(self):
        return self._context.model_key

    def evidence_identity(self):
        return dict(model_id=self.model_id, model_revision=self._context.model_revision or "",
                    kind="plain_language", prompt=self._context.prompt, settings=self._context.settings())

    def _report_phase(self, phase):
        progress = getattr(self, "_runtime_progress", None)
        if callable(progress):
            progress(phase)

    def analyze(self, image):
        return self.analyze_with_cancellation(image, None)

    def analyze_with_cancellation(self, image, should_cancel):
        state, code, cancelled, cleanup_failed = "not_sent", None, False, False
        inputs = {}
        prepared = request = response = answer = None
        try:
            check_cancelled(should_cancel)
            self._report_phase("Preparing image for local server")
            check_cancelled(should_cancel)
            prepared = prepare_image(image, should_cancel=should_cancel)
            check_cancelled(should_cancel)
            context = self._context
            inputs = dict(model_key=context.model_key, expected_instance_id=context.expected_instance_id,
                prompt=context.prompt, image_bytes=prepared.encoded, image_mime=prepared.mime,
                temperature=context.temperature, max_output_tokens=context.max_output_tokens)
            request = encode_description(**inputs)
            request_hash = hashlib.sha256(request.body).hexdigest()
            request = None
            check_cancelled(should_cancel)
            self._report_phase("Waiting for local description")
            check_cancelled(should_cancel)
            state = "outcome_unknown"
            response = self._dispatch(**inputs, cancelled=should_cancel or (lambda: False))
            check_cancelled(should_cancel)
            valid = type(response) is DescriptionResult
            if valid:
                valid = (type(response.model_key) is str
                    and type(response.instance_id) is str and type(response.request_sha256) is str
                    and response.model_key == context.model_key
                    and response.instance_id == context.expected_instance_id
                    and response.request_sha256 == request_hash
                    and type(response.description) is str
                    and 0 < len(response.description) <= MAX_DESCRIPTION_CHARS
                    and bool(response.description.strip())
                    and not any(unicodedata.category(c) == "Cs" for c in response.description)
                    and type(response.statistics) is DescriptionStatistics)
            if valid:
                for name in STAT_FIELDS:
                    number = getattr(response.statistics, name)
                    valid = valid and (number is None or (
                        type(number) in (int,float) and number >= 0
                        and (type(number) is int or math.isfinite(number))))
            if not valid:
                raise LMStudioBackendError("invalid_dispatch_result")
            self._report_phase("Checking description and source")
            verify_source_current(image, prepared, should_cancel=should_cancel)
            audit = prepared.audit()
            audit.update(request_sha256=request_hash, model_instance_id=response.instance_id,
                         transport_statistics=asdict(response.statistics))
            answer = InterrogationResult(model_id=context.model_key,kind="plain_language",
                plain_language=response.description,prompt=context.prompt,
                model_revision=context.model_revision or "",settings=context.settings(),raw_output=audit)
            validate_interrogation_result(answer)
            check_cancelled(should_cancel)
        except DescriptionTransportError as error:
            code = error.code
            state = error.submission_state
            cleanup_failed = error.cleanup_failed
            cancelled = code == "description_cancelled"
        except InterruptedError:
            cancelled = True
        except (ImagePreparationError, LMStudioBackendError) as error:
            candidate = str(error)
            code = candidate if candidate in BACKEND_CODES else "analysis_preparation_failed"
            cleanup_failed = code == "image_cleanup_failed"
        except Exception:
            code = "dispatch_failed" if state == "outcome_unknown" else "analysis_preparation_failed"
        finally:
            inputs.clear()
            prepared = request = response = image = candidate = None
        if cancelled:
            answer = None
            raise LMStudioBackendCancelled(state,cleanup_failed=cleanup_failed)
        if code is not None:
            answer = None
            raise LMStudioBackendError(code,state,cleanup_failed=cleanup_failed)
        return answer
