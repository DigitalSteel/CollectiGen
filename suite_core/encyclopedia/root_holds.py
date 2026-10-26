"""Stable hold identities shared by module-entry dispatch and recovery UI."""


class OriginalRootHold:
    """Live supervision of one failed root; no operation retry or replacement."""
    def __init__(self, backend, control, executor, error):
        from threading import Event
        self.backend, self.control, self.executor, self.error = backend, control, executor, error
        self.close_acknowledged = False
        self.released = Event()

    def release_after_recovery(self, *, confirmed=False):
        """Separate explicit recovery must settle the original backend first."""
        if confirmed is not True or self.executor.close_held or self.backend.close_held:
            raise RuntimeError('Original root recovery and physical worker drain required')
        self.backend.close()
        self.executor.begin_close()
        self.released.set()

    def wait(self):
        while not self.released.wait(.1):
            pass
        return 0

    def present(self):
        from .root_recovery_ui import present_original_root_hold
        try:
            return present_original_root_hold(self)
        except BaseException as error:
            self.presentation_error = error
            return self.wait()


class OriginalDeploymentHold:
    """Owner-only factory failure: retain originals, never pretend a backend."""
    def __init__(self,owner,executor,error):
        from core.manager_launch_owner import DeploymentOwner
        from threading import Event
        if type(owner) is not DeploymentOwner:
            raise TypeError('Original failed DeploymentOwner required')
        self.owner,self.executor,self.error=owner,executor,error
        self.control=None
        self.close_acknowledged=False
        self.released=Event()

    def release_after_recovery(self,*,confirmed=False):
        raise RuntimeError('Original owner-only startup remains held until qualified original cleanup is available')

    def wait(self):
        return OriginalRootHold.wait(self)

    def present(self):
        return OriginalRootHold.present(self)
