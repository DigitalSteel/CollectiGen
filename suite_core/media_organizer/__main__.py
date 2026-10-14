"""Source entry; native execution remains separately unqualified."""
from . import entry
from .organizer_recovery_entry import compose_recovery_runtime


def cli_main(argv=None):
    import argparse
    from pathlib import Path
    from core.application_profile import bind_profile_root, default_organizer_profile
    parser = argparse.ArgumentParser(description="Media Organizer — local Library, Dataset and Ingest")
    parser.add_argument("--profile-root", type=Path,
                        help="CollectiGen profile root (containing data/user); defaults to your local Organizer profile")
    parser.add_argument("--runtime-profile",choices=("collectigen-core","organizer-image"),
                        help="Explicit private runtime admission policy")
    parser.add_argument("--runtime-root",type=Path,help="Explicit private runtime root containing python")
    parser.add_argument("--validate-runtime",action="store_true")
    parser.add_argument("--repair-runtime",action="store_true",help="Explicitly run existing private runtime repair")
    args = parser.parse_args(argv)
    if bool(args.runtime_profile)!=bool(args.runtime_root):
        parser.error("Runtime profile and runtime root must be selected together.")
    if (args.validate_runtime or args.repair_runtime) and args.runtime_profile is None:
        parser.error("Runtime validation or repair requires an explicit profile and root.")
    from .runtime_startup import RuntimeSelection,OrganizerRuntimeError
    selection=None
    if args.runtime_profile is not None:
        try:
            selection=RuntimeSelection(args.runtime_profile,args.runtime_root,Path(__file__).resolve().parents[2])
            if args.repair_runtime:return selection.repair()
            if args.validate_runtime:
                selection.validate()
                print("Media Organizer selected private runtime is ready.")
                return 0
        except (OSError,ValueError,RuntimeError) as exc:
            parser.error(str(exc))
    try:
        bind_profile_root(args.profile_root or default_organizer_profile(),
                          program_root=Path(__file__).resolve().parents[2])
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    try:
        return main() if selection is None else main(runtime_selection=selection)
    except OrganizerRuntimeError as exc:
        print(f"Media Organizer cannot start: {exc}")
        return 2


def main(*, native_loader=None, label_library_item=None,runtime_selection=None):
    loader = native_loader or (entry.load_native_runtime if runtime_selection is None
                              else lambda:entry.load_native_runtime(runtime_selection=runtime_selection))
    def composed_loader():
        options = {} if label_library_item is None else {"label_library_item": label_library_item}
        return compose_recovery_runtime(loader(), **options)
    return (entry.main(native_loader=composed_loader) if runtime_selection is None else
            entry.main(native_loader=composed_loader,runtime_selection=runtime_selection))


if __name__ == "__main__":
    raise SystemExit(cli_main())
