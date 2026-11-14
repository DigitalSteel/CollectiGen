# Suite Manager a.0.7.11 — 2026-11-14

- Restores the existing Anima worker program and a required Training execution/readiness source file in Manager packages. These files were referenced by the reviewed Training services but omitted from the frozen export.
- Preserves existing model/runtime qualification, selected-resource, preparation, cancellation and recovery checks. This packaging fix does not change the trainer or start a model/training run.

Use Manager's reviewed update when offered. The existing automatic-upgrade floor remains a.0.7.9. Older launchers require the complete Windows ZIP; an update of Manager alone keeps the installed launcher.

No Reader/C3A, real-library, model or trainer execution is included in this packaging qualification.
