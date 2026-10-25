"""Managed frozen entry; development module entry stays SDK-independent.

Frozen loader/package qualification remains a separate gate. Neither SDK
housekeeping nor Qt startup precedes inherited native peer qualification.
"""
_HELD_NORMAL_SESSIONS = []


def run(argv=None, *, executable=None, channel_factory=None,
        expected_launcher_sha256=None, sdk_factory=None, manager_launch=None,
        update_service_factory=None):
    import sys
    from suite_core.manager.bootstrap_entry import EntryHold, NormalEntrySession, classify_invocation
    from suite_core.manager.entry_binding import validate_normal_launch, launch_manager
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if classify_invocation(arguments).kind != 'normal':
        import os
        if not os.environ.get('COLLECTIGEN_STOCK_HOOK_CONTEXT'):
            raise EntryHold('stock_hook_transport_unqualified')
        from core.manager_stock_hook_admission import run_stock_hook
        return run_stock_hook(arguments, sdk_factory=sdk_factory,
                              expected_launcher_sha256=expected_launcher_sha256)
    admitted = validate_normal_launch(arguments, executable=sys.executable if executable is None else executable)
    # Existing owned service must first participate in helper grant/registration.
    if any(value.partition('=')[0].startswith('--owned-ollama') or
           value == '--allow-reviewed-owned-ollama' for value in arguments):
        from suite_core.manager.launcher_main import OWNED_HELPER_PRODUCTION_QUALIFIED
        if not OWNED_HELPER_PRODUCTION_QUALIFIED:
            raise EntryHold('owned_helper_actor_registration_unqualified')
    if expected_launcher_sha256 is None:
        # Independent of the offer: exact admitted stable launcher image on disk.
        # Root's release/package admission is still required before its spawn.
        import hashlib
        from suite_core.installation import _path
        launcher = _path(admitted.layout.code_root / 'CollectiGen.exe')
        digest = hashlib.sha256()
        with launcher.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
        expected_launcher_sha256 = digest.hexdigest()
    if channel_factory is None:
        from core.manager_launch_owner import WindowsControlChannel
        channel_factory = WindowsControlChannel.from_inherited_environment
    channel = channel_factory()
    session = None
    try:
        session = NormalEntrySession.accept(channel, admitted,
                                            expected_launcher_sha256=expected_launcher_sha256)
        if sdk_factory is None:
            import velopack
            sdk_factory = velopack.App
        sdk_factory().set_auto_apply_on_startup(False).run()
        target = launch_manager if manager_launch is None else manager_launch
        options = dict(normal_entry_session=session, argv=arguments,
                       executable=sys.executable if executable is None else executable)
        if update_service_factory is None and getattr(session,'update_profile',None) is not None:
            try:
                from suite_core.manager.compiled_update_config import PROFILE
            except ImportError as error:
                raise EntryHold('explicit_update_service_factory_required') from error
            from core.manager_update_host import CompiledManagerUpdateProfile
            if type(PROFILE) is not CompiledManagerUpdateProfile:
                raise EntryHold('explicit_compiled_update_profile_required')
            update_service_factory=PROFILE.child_service
        if update_service_factory is not None:
            if not callable(update_service_factory):
                raise EntryHold('trusted_update_service_factory_required')
            from core.manager_update_service import ManagerUpdateService
            service = update_service_factory(session=session, admitted=admitted)
            if type(service) is not ManagerUpdateService:
                raise EntryHold('explicit_manager_update_service_required')
            options['manager_update_service'] = service.bind_normal_entry(admitted)
            profile=getattr(session,'update_profile',None)
            if profile is not None:
                expected=service.host_profile(admitted,transport_id=profile['transport_id'])
                if expected!=profile:
                    raise EntryHold('explicit_child_update_host_profile_changed')
                reply=session.request_manager_update('configure',{},profile_sha256=profile['profile_sha256'])
                if reply['status']!='reviewed' or reply['result']!=profile:
                    raise EntryHold('original_update_host_configuration_held')
        elif getattr(session,'update_profile',None) is not None:
            raise EntryHold('explicit_update_service_factory_required')
        result = target(**options)
        if session.state != 'local_accepted':
            raise EntryHold('manager_returned_without_local_scope_acceptance')
        channel.close()
        return result
    except BaseException as error:
        # Never close an uncertain session or claim final physical process drain.
        # Root retains native process/Job authority independently of this object.
        if session is not None:
            _HELD_NORMAL_SESSIONS.append(session)
            try: error.normal_entry_session = session
            except BaseException: pass
        else:
            channel.close()  # No entry/helper work was admitted.
        raise


if __name__ == '__main__':
    raise SystemExit(run())
