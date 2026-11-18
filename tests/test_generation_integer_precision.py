"""Exact generator seeds survive parsing, display and saved annotation roundtrip."""
import json

import pytest

from core.annotation import ImageAnnotation
from core.generation_metadata import format_generation_record, parse_generation_payload


@pytest.mark.parametrize("seed", [2**53 + 1, 6237327949615356897, 2**64 - 1])
@pytest.mark.parametrize("representation", [
    "swarm_integer", "swarm_string", "swarm_decimal_string", "infotext",
    "infotext_decimal", "comfy", "comfy_exponent_string",
])
def test_large_seed_preserves_generator_identity_through_saved_roundtrip(seed, representation):
    if representation.startswith("swarm"):
        raw = json.dumps({"sui_image_params": {
            "prompt": "A blue mug and a red apple", "seed":
            {"swarm_string": str(seed), "swarm_decimal_string": f"{seed}.0"}.get(representation, seed),
            "steps": 12, "cfgscale": 5.0, "width": 512, "height": 512,
        }, "unrecognized_evidence": {"keep": "exactly"}})
    elif representation.startswith("infotext"):
        seed_text = f"{seed}.0" if representation == "infotext_decimal" else str(seed)
        raw = f"A blue mug and a red apple\nSteps: 12, CFG scale: 5, Seed: {seed_text}, Size: 512x512"
    else:
        raw = json.dumps({"prompt": {"7": {"class_type": "KSampler", "inputs": {
            "seed": f"{seed}0e-1" if representation == "comfy_exponent_string" else seed,
            "steps": 12, "cfg": 5.0,
            "sampler_name": "euler", "scheduler": "normal",
        }}}})

    record = parse_generation_payload(raw)
    assert record.seed == seed
    assert type(record.seed) is int
    assert record.stages[0].seeds == [seed]
    assert f"Seed: {seed}" in format_generation_record(record)
    assert record.raw == raw

    annotation = ImageAnnotation("synthetic.png", generation_records=[record])
    saved = json.dumps(annotation.to_dict())
    reopened = ImageAnnotation.from_dict(json.loads(saved), "synthetic.png").generation_records[0]
    assert reopened.seed == seed
    assert reopened.stages[0].seeds == [seed]
    assert reopened.raw == raw


def test_infotext_keeps_exact_variation_seed_and_bounds_exponent_expansion():
    variation = 2**64 - 1
    raw = f"Synthetic still life\nSteps: 12, Seed: 42, Variation seed: {variation}.0"
    record = parse_generation_payload(raw)
    assert record.stages[0].seeds == [42, variation]
    assert record.raw == raw
    oversized = parse_generation_payload(
        {"sui_image_params": {"prompt": "Synthetic still life", "seed": "1e1000000"}}
    )
    assert oversized.seed == "1e1000000"


def test_exact_seed_fix_preserves_fractional_sampling_and_unknown_numeric_evidence():
    raw = {"sui_image_params": {
        "prompt": "Synthetic still life", "seed": 42,
        "steps": 12, "cfgscale": 5.5, "width": 512, "height": 512,
        "unknown_generator_value": "retain me",
    }}
    record = parse_generation_payload(raw)
    assert record.seed == 42
    assert record.cfg_scale == 5.5
    assert record.raw == raw
    assert any(value == "retain me" for value in record.extras.values())


@pytest.mark.parametrize("seed", [2**53 + 1, 6237327949615356897, 2**64 - 1])
@pytest.mark.parametrize("representation", ["swarm_decimal", "comfy_exponent", "generic_decimal"])
def test_unquoted_json_seed_stays_exact(seed, representation):
    if representation == "swarm_decimal":
        raw = f'{{"sui_image_params":{{"seed":{seed}.0,"cfgscale":5.5}}}}'
    elif representation == "comfy_exponent":
        raw = f'{{"prompt":{{"7":{{"class_type":"KSampler","inputs":{{"seed":{seed}0e-1,"cfg":5.5}}}}}}}}'
    else:
        raw = f'{{"prompt":"Blue cup","Seed":{seed}.0,"cfg":5.5,"unknown":[1.25,4.5]}}'

    record = parse_generation_payload(raw)
    assert record.seed == seed and type(record.seed) is int
    assert record.stages[0].seeds == [seed]
    assert record.cfg_scale == 5.5 and record.raw == raw
    if representation == "generic_decimal":
        unknown = record.extras["unknown"]
        assert unknown == [1.25, 4.5] and all(type(number) is float for number in unknown)
    annotation = ImageAnnotation("synthetic.png", generation_records=[record])
    restored = ImageAnnotation.from_dict(json.loads(json.dumps(annotation.to_dict())), "synthetic.png")
    assert restored.generation_records[0].seed == seed
    assert restored.generation_records[0].raw == raw
