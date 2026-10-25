"""Explicit composition overlay; reviewed Media Organizer/Encyclopedia unchanged.

No imports of either application's runtime, no service construction, no data
discovery. Wrap the already-created reviewed MediaOrganizerWindow class and attach
an already-selected EncyclopediaWindow using host-supplied admission and adapters.
"""
from dataclasses import dataclass, replace


HOSTED_MEMBERS = ("dataset", "evidence_queries", "knowledge", "events", "jobs")


class CompositionBoundaryError(ValueError):
    """Missing admission/ownership must be resolved by the host, not inferred."""


@dataclass
class HostedExamplesAttachment:
    """Borrowed browser binding; native jobs retain execution/drain authority."""
    organizer_window: object
    encyclopedia_window: object
    binding: object
    detached: bool = False

    def detach(self):
        if self.detached:
            return
        # Do not detach a later foreign browser binding installed by another owner.
        if getattr(self.encyclopedia_window, "_hosted_binding", None) is self.binding:
            self.encyclopedia_window.detach_hosted_library_session()
        else:
            self.binding.close()
        self.detached = True
        if getattr(self.organizer_window, "_encyclopedia_examples_attachment", None) is self:
            self.organizer_window._encyclopedia_examples_attachment = None


def with_hosted_encyclopedia_examples(reviewed_window_type: type) -> type:
    """Add only the hosted-session call and accepted-close detachment lifecycle.

    Composition passes the exact existing shell.state into the Encyclopedia
    owner's public attach interface. It never captures/publishes a catalog or
    supplies an invented Publication, scope, query engine, index, GUI dispatcher
    or task owner. The caller owns matching Knowledge/Publication admission.
    """
    class HostedMediaOrganizerWindow(reviewed_window_type):
        def attach_encyclopedia_examples(self, encyclopedia_window, *,
                                        admitted_publication, task_factory, post_to_gui):
            organizer = getattr(self, "organizer", None)
            if organizer is None:
                raise CompositionBoundaryError("An admitted Media Organizer host is required.")
            # Retain the reviewed facade's settings/state/service identity guards.
            organizer._check_context()
            session = self.state
            if any(getattr(session, name, None) is None for name in HOSTED_MEMBERS):
                raise CompositionBoundaryError("Existing hosted library service boundaries are required.")
            if (admitted_publication is None
                    or admitted_publication != getattr(encyclopedia_window, "publication", None)
                    or getattr(encyclopedia_window, "catalog_session", None) is None):
                raise CompositionBoundaryError("Select the matching host-admitted Publication before attachment.")
            if not callable(task_factory) or not callable(post_to_gui):
                raise CompositionBoundaryError("Existing native task and GUI dispatch adapters are required.")
            if not callable(getattr(encyclopedia_window, "attach_hosted_library_session", None)):
                raise CompositionBoundaryError("The reviewed Encyclopedia hosted interface is required.")
            current = getattr(self, "_encyclopedia_examples_attachment", None)
            if current is not None and not current.detached:
                if current.encyclopedia_window is not encyclopedia_window:
                    raise CompositionBoundaryError("Detach the current examples window before attaching another.")
                current.detach()
            binding = encyclopedia_window.attach_hosted_library_session(
                session, admitted_publication=admitted_publication,
                task_factory=task_factory, post_to_gui=post_to_gui)
            attachment = HostedExamplesAttachment(self, encyclopedia_window, binding)
            self._encyclopedia_examples_attachment = attachment
            return attachment

        def detach_encyclopedia_examples(self):
            attachment = getattr(self, "_encyclopedia_examples_attachment", None)
            if attachment is not None:
                attachment.detach()

        def closeEvent(self, event):
            # Native close owns unsaved decisions, cancellation and actual job drain.
            result = super().closeEvent(event)
            if event.isAccepted():
                self.detach_encyclopedia_examples()
            return result

    HostedMediaOrganizerWindow.__name__ = "HostedMediaOrganizerWindow"
    return HostedMediaOrganizerWindow


def compose_startup_runtime(reviewed_runtime):
    """Use unchanged reviewed run_startup with this subclass below its window.

    Runtime adapters are retained by identity; only the base-window constructor
    is wrapped. The reviewed entry then makes its normal MediaOrganizerWindow on
    top. No native loader, startup, window or Encyclopedia is called here.
    """
    return replace(reviewed_runtime,
                   base_window=with_hosted_encyclopedia_examples(reviewed_runtime.base_window))
