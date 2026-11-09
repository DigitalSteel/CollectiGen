"""Operation labels and the small-tensor bound shared by Merge callers."""

OPERATIONS = ("blend_checkpoints", "combine_loras", "bake_loras")
MAX_ELEMENTS = 4096  # Component proof only; not a model resource estimate.
