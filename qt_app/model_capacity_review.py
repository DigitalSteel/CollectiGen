"""Shared plain-language review for making room before complete model preparation."""
from pathlib import Path


def capacity_review_text(capacity, *, retained=False):
    if capacity is None: return ""
    if capacity.ready and not capacity.victims:
        return "Fast storage has room for this set. No existing Fast copies will be removed."
    def size(value):
        if value is None: return "Unknown"
        return f"{value / 1024**3:,.2f} GiB" if value >= 1024**3 else f"{value:,} bytes"
    heading = ("Recorded least-used cleanup plan; recovery rechecks remaining copies" if retained else
        "Make room using least-used models: " + ("ready for review" if capacity.ready else "preparation held"))
    lines = [heading,
        f"Space to reclaim: {size(capacity.required_bytes)} · Eligible selected copies: {size(capacity.reclaimable_bytes)}",
        f"New copies: {size(capacity.new_bytes)} · Fast limit: {size(capacity.cache_limit_bytes) if getattr(capacity, 'cache_limit_mode', 'fixed') == 'fixed' else 'No fixed cache cap'} · "
        f"Free disk space: {size(capacity.free_bytes)} · Keep free: {size(capacity.minimum_free_bytes)}",
        capacity.message]
    for victim in capacity.victims:
        lines.append(f"Remove Fast copy: {Path(victim.path).name}\n{victim.path}\n"
            f"{size(victim.bytes)} · Recorded successes: {victim.successful_use_count} · Last successful use: {victim.last_success_at}")
    if capacity.victims:
        lines.append("Preparing this entire set may remove only these reviewed Fast copies. Archive originals remain. "
            "Keep Warm, active and manually pinned copies are protected. Cancelled work retains exact recovery; removed Fast copies are not automatically restored.")
    else:
        lines.append("Preparation is held. No Fast copies will be removed. Review storage settings or manual cleanup.")
    return "\n\n".join(lines)
