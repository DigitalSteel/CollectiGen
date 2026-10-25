"""Write an explicitly requested synthetic snapshot, never a native catalog."""
import argparse
from threading import RLock
from suite_core.knowledge import Category, EffectiveTag, Lookup, Snapshot, Source
from .publication import Publication, publish


def main():
    parser = argparse.ArgumentParser(description="Create a synthetic Encyclopedia demo snapshot")
    parser.add_argument("destination", help="New publication file in an existing developer directory")
    args = parser.parse_args()
    snapshot = Snapshot(Source("Synthetic developer demo", "demo-1", "synthetic", ("species",)),
        (EffectiveTag("demo:fox", "Red Fox", "species", ("Vulpes",)),
         EffectiveTag("demo:wolf", "Wolf", "species")),
        (Category("species", "Species", "Life / Species"),),
        (Lookup("red fox", "canonical_name", "demo:fox", ("demo:fox",)),
         Lookup("vulpes", "canonical_alias", "demo:fox", ("demo:fox",)),
         Lookup("wolf", "canonical_name", "demo:wolf", ("demo:wolf",)),
         Lookup("animal", "ambiguous", owner_ids=("demo:fox", "demo:wolf"))),
        removed_ids=("demo:retired",))
    publish(Publication("synthetic-demo", 1, snapshot), args.destination,
            expected_token=None, revision=lambda: "demo-1", guard=RLock())


if __name__ == "__main__":
    main()
