"""Exact generator seeds survive parsing, display and saved annotation roundtrip."""
import json

import pytest

from core.annotation import ImageAnnotation
from core.generation_metadata import format_generation_record, parse_generation_payload


@pytest.mark.parametrize("seed", [2**53 + 1, 6237327949615356897, 2**64 - 1])
@pytest.mark.parametrize("representation", ["swarm_integer", "swarm_string", "infotext", "comfy"])
def test_large_seed_preserves_generator_identity_through_saved_roundtrip(seed, representation):
    if representation.startswith("swarm"):
        raw = json.dumps({"sui_image_params": {
            "prompt": "A blue mug and a red apple", "seed":
            str(seed) if representation == "swarm_string" else seed,
            "steps": 12, "cfgscale": 5.0, "width": 512, "height": 512,
        }, "unrecognized_evidence": {"keep": "exactly"}})
    elif representation == "infotext":
        raw = f"A blue mug and a red apple\nSteps: 12, CFG scale: 5, Seed: {seed}, Size: 512x512"
    else:
        raw = json.dumps({"prompt": {"7": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 12, "cfg": 5.0,
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
