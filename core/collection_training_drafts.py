import json
"""Bind explicitly reviewed Collections through the existing atomic draft writer."""
from .concept_groups import group_fingerprint
from .collection_training_version import capture_collection_training_version


class CollectionTrainingDraftStore:
    def __init__(self,store,context):
        self.store,self.context=store,context

    def __getattr__(self,name):
        return getattr(self.store,name)

    def save_plan(self,plan,name,**options):
        store,collection_id,fingerprint=self.context
        # Keep the canonical Collection lease through the single draft writer.
        with store.locked_snapshot() as (snapshot,_revision):
            group=snapshot["groups"].get(collection_id)
            if group is None or group_fingerprint(group)!=fingerprint:
                raise ValueError("Collection changed. Reselect it and review the draft again.")
            version=capture_collection_training_version(store,collection_id,plan)
            if group_fingerprint(version.to_dict()["collection"])!=fingerprint:
                raise ValueError("Collection changed during draft capture.")
            return self.store.save_plan(plan,name,collection_version=json.loads(version.to_bytes()),**options)
