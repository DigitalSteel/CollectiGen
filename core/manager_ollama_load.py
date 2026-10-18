"""Explicit existing-model CPU preload over the original guarded Ollama service."""
from dataclasses import replace

from core.manager_ollama_service import installed_choices, encoded
from suite_core.discovery.ollama_readonly import ReadRoute, ReadState


def fresh_load_choices(binding, report, now):
    if binding is None or report is None or not binding.scope.accepts(report):
        raise ValueError("Refresh the selected connection first.")
    for route in (ReadRoute.VERSION, ReadRoute.LOADED):
        row = report.observation(route)
        if row.state is not ReadState.AVAILABLE or row.freshness_at(now, report.limits).value != "fresh":
            raise ValueError("Refresh current runtime and loaded-model observations first.")
    return installed_choices(report, now)


def load_existing_model(service, selected, *, ticket, pending, cancelled, clock, resources):
    """One POST at most; no pull, unload, config write or server ownership."""
    def inventory():
        service.validate_chat_scope()
        if cancelled():
            raise ValueError("Load canceled before admission.")
        report = service.binding.registration.client.discover()
        choices = fresh_load_choices(service.binding, report, clock())
        # loaded is presentation evidence; name/digest/size are immutable selection.
        if not any((m.name, m.digest, m.size_bytes) ==
                   (selected.name, selected.digest, selected.size_bytes) for m in choices):
            raise ValueError("The selected model changed. Refresh before loading.")
        if report.observation(ReadRoute.LOADED).models:
            raise ValueError("Ollama already has a loaded model. Refresh and use it; no workload is replaced.")
        return report

    before = inventory()
    sample = resources()
    ram = [r for r in sample.readings if r.telemetry is not None
           and r.telemetry.free_ram_bytes is not None and r.state_at(clock()) == "measured"]
    if len(ram) != 1:
        raise ValueError("Fresh available RAM measurement required.")
    plan = service.plan("load", selected, clock(), available_ram_bytes=ram[0].telemetry.free_ram_bytes)
    import json
    payload = json.loads(plan.payload_bytes)
    payload["options"]["num_gpu"] = 0
    plan = replace(plan, payload_bytes=encoded(payload),
                   expected_runtime_version=before.observation(ReadRoute.VERSION).version)
    completion = service.work(ticket, plan, reviewed_fingerprint=plan.fingerprint,
        current_pending=pending, cancelled=cancelled, now=clock, inventory=inventory)
    service.admit(completion, current_pending=pending, cancelled=cancelled, now=clock())
    report = service.binding.registration.client.discover()
    if report.observation(ReadRoute.VERSION).version != plan.expected_runtime_version:
        raise ValueError("Runtime changed during load. Refresh before continuing.")
    choices = fresh_load_choices(service.binding, report, clock())
    loaded = report.observation(ReadRoute.LOADED)
    if cancelled() or not any(m.name == selected.name and m.digest == selected.digest
                              and m.size_bytes == selected.size_bytes for m in choices) or not any(
            m.name == selected.name and m.digest == selected.digest and type(m.context_length) is int
            and m.context_length >= 4096 for m in loaded.models or ()):
        raise ValueError("Load outcome needs a fresh inventory review.")
    service.validate_chat_scope()
    return report
