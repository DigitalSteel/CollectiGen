"""One non-killing standalone Root, after the original Manager fully retires."""
from functools import partial
from time import sleep

from core.encyclopedia_legacy_handoff import LegacyReaderHandoff
from core.encyclopedia_legacy_host import CompiledLegacyReaderProfile
from suite_core.discovery.component_staging import require

_HELD_LEGACY_SUPERVISORS=[]


class LegacyReaderSupervisor:
    def __init__(self, profile, plan):
        require(type(profile) is CompiledLegacyReaderProfile and type(plan) is LegacyReaderHandoff,
                'typed_original_legacy_handoff_required')
        self.profile,self.plan=profile,plan
        self.native=self.child=self.identity=None
        self.started=False
        self.retired=False
        self.exit_code=None
        self.executor=None
        self.failed_start=False

    @property
    def close_held(self):
        return not self.retired or self.executor is not None and self.executor.close_held

    def recheck(self):
        if self.executor is not None and self.executor.close_held:
            return False  # Only the original running worker mutates native state.
        if self.started and self.native is not None and self.child is None:
            self.child=self.native.recover_failed_root_child((str(self.plan.binding.bootstrap_program),))
            if self.child is None:
                self.retired=True
        self.poll()
        if self.executor is not None and not self.executor.close_held:
            self.executor.begin_close()
        return not self.close_held

    @property
    def can_cancel_unstarted(self):
        if self.executor is not None and self.executor.close_held:return False
        if not self.failed_start or self.native is None or self.child is None:return False
        job=self.native.job
        if (job is None or job.creation_returned is not True or job.resume_entered
                or not job.process or not job.thread
                or job.creation_command!=(str(self.plan.binding.bootstrap_program),)):return False
        self.native.check_retained_handle(self.child)
        return self.child.job is job and self.child._handle==job.process and self.child.pid==job.pid and self.child.poll() is None

    def cancel_unstarted(self, *, confirmed=False):
        require(confirmed is True and self.can_cancel_unstarted,'explicit_original_unstarted_cancel_required')
        self.native.terminate_owned(self.child)

    def start(self, environment):
        require(not self.started,'legacy_original_attempt_already_started')
        self.started=True  # A failed/uncertain birth cannot be replayed.
        self.plan.revalidate()
        self.profile.verify_bootstrap(self.plan.binding.root)
        from core.ollama_owned_windows import WindowsOwnedProcess
        from core.training_process_windows import WindowsTrainingJob
        self.native=WindowsOwnedProcess()
        self.native.job_factory=partial(WindowsTrainingJob,interactive=True,kill_on_close=False)
        program=self.plan.binding.bootstrap_program
        try:
            self.child=self.native.spawn_command((str(program),),program.parent,dict(environment))
            self.identity=self.native.identity(self.child,program)
        except BaseException:
            self.failed_start=True
            raise

    def poll(self):
        if self.retired:return True
        if not self.started:return False
        if self.native is None:
            self.retired=True  # Preparation failed before a native constructor.
            return True
        require(self.child is not None,
                'original_partial_legacy_birth_retained')
        if self.identity is None:
            require(self.failed_start,'original_failed_legacy_start_required')
            self.native.check_retained_handle(self.child)
            require(self.child.job is self.native.job and self.child._handle==self.native.job.process
                    and self.child.pid==self.native.job.pid,'original_partial_legacy_handle_required')
        else:
            self.native.check_identity(self.child,self.identity,self.plan.binding.bootstrap_program)
        code=self.child.poll()
        if code is None or self.native.job.active()!=0:return False
        if self.failed_start and self.native.drain is None:
            # No reader worker was constructed. With original exit + Job zero,
            # close that attempt's retained output endpoint; no health is granted.
            if self.native.job.stdout is not None:
                self.native.job.stdout.close();self.native.job.stdout=None
            self.native.drained.set()
        if (not self.native.drained.is_set()
                or self.native.drain is not None and self.native.drain.is_alive()):return False
        require(not self.native.drain_failed,'legacy_original_output_drain_failed')
        self.native.close(self.child)
        self.exit_code=code
        self.retired=True
        return True

    def reader_visible(self):
        from core.encyclopedia_connection import _visible_reader_window
        from core.training_process_windows import NativeObservationError
        try:
            members=self.native.job.process_ids()
        except NativeObservationError:
            # The last member can exit after poll while its original output
            # worker is still draining. Empty membership is not a new process
            # observation and must not convert ordinary Close into a fault.
            if self.native.job.active()==0:return False
            raise
        return any(_visible_reader_window(pid) for pid in members)

    def run(self, environment):
        from suite_core.encyclopedia.pending_task_ui import RootTaskProgress
        from .resource_executor import ResourceExecutorOwner
        self.executor=ResourceExecutorOwner()
        progress=RootTaskProgress()
        progress.phase('Opening Reader',
            'Manager has closed safely. Checking and opening the selected Reader; Manager will return after the Reader closes.')
        visible=False
        def service():
            nonlocal visible
            self.start(environment)
            while not self.poll():
                # Inspect only members of this retained original Job, never
                # discover/adopt an unrelated desktop process or window.
                current=self.reader_visible()
                if current and not visible:progress.reader_ready()
                elif visible and not current:
                    progress.phase('Finishing legacy Reader Close',
                        'The Reader window has closed. Waiting for its launcher and background tasks before reopening Manager.')
                visible=current
                sleep(.1)
            require(self.exit_code==0,'legacy_standalone_root_exit_failed')
            progress.phase('Returning to Manager',
                'Checking the saved Reader installation before reopening Manager. Your catalogs and settings are kept.')
            current=self.plan.binding.current()
            require(current.phase in {'healthy','rolled_back'}
                    and current.selected.version in {'0.2.2-alpha.1','0.3.1-alpha.1'},
                    'legacy_signed_healthy_result_required')
            self.profile.verify_bootstrap(self.plan.binding.root)
            return current
        try:
            result=progress.wait(self.executor.submit(service),self.executor)
            self.executor.begin_close()
            progress.finish()
            return result
        except BaseException:
            if self not in _HELD_LEGACY_SUPERVISORS:_HELD_LEGACY_SUPERVISORS.append(self)
            progress.finish()
            raise
