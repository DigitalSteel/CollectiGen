"""Worker-ready full-vocabulary search projection shared by Tag palettes."""
from __future__ import annotations
from dataclasses import dataclass
from .tag_collection import normalize_tag
from .tag_ordering import ORDER_GROUP_RANK


@dataclass(frozen=True)
class PreparedTagPaletteIndex:
    known_keys: frozenset[str]
    ordered_records: tuple
    ordered_entries: tuple
    exact_identity_index: dict[str,tuple]
    descendant_counts: dict[str,int]


def prepare_tag_palette_index(knowledge,categories) -> PreparedTagPaletteIndex:
    steps=prepare_tag_palette_index_steps(knowledge,categories)
    while True:
        try:next(steps)
        except StopIteration as completed:return completed.value


def prepare_tag_palette_index_steps(knowledge,categories):
    """Build the full-vocabulary search projection without creating Qt widgets."""
    records=tuple(knowledge.tags.values()) if knowledge is not None else ()
    known_keys=frozenset(normalize_tag(record.display) for record in records)
    # Stable passes preserve the original rank/priority/display ordering and
    # insertion-order ties without one tracked sort-key tuple per record.
    ordered=list(records)
    ordered.sort(key=lambda item:item.display.casefold())
    ordered.sort(key=lambda item:item.ordering.priority,reverse=True)
    ordered.sort(key=lambda item:ORDER_GROUP_RANK.get(item.ordering.group,99))
    ordered_records=tuple(ordered)
    yield
    entries=[]; exact_first={}; exact_more={}; category_paths={}; descendant_counts={}
    for position,record in enumerate(ordered_records):
        key=normalize_tag(record.display)
        names=tuple(knowledge.identity_names(record))
        if record.category not in category_paths:
            category_paths[record.category]=(
                (tuple(categories.path_ids(record.category)),categories.path_label(record.category))
                if categories is not None else ((),"")
            )
        path_ids,path_label=category_paths[record.category]
        entries.append((record,key," ".join((*names,path_label)).casefold(),path_ids))
        for category_id in path_ids:
            descendant_counts[category_id]=descendant_counts.get(category_id,0)+1
        for name in names:
            name_key=normalize_tag(name)
            if name_key in exact_first:
                exact_more.setdefault(name_key,[]).append(record)
            else:
                exact_first[name_key]=record
        if position%256==255:
            yield
    return PreparedTagPaletteIndex(
        known_keys=known_keys,
        ordered_records=ordered_records,
        ordered_entries=tuple(entries),
        exact_identity_index={key:(first,*exact_more.get(key,()))
                              for key,first in exact_first.items()},
        descendant_counts=descendant_counts,
    )
