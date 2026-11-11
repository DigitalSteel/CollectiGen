# Image planning from Media Organizer

In Dataset, select an image and use **Image Actions > Remix This Image** or the generative choices under **Change This Image**. The original image and its Image Knowledge remain unchanged. Edit the prompt and settings, then use **More Draft Actions > Export** to save a generation request.

In Suite Manager, open **Applications > Plan an Image**, import that request, and review it. Source-image references may require reattaching the exact original through Manager's existing Remix controls. Resource selection, connection review and deliberate execution belong to Manager. Request export does not copy image pixels or start a generator.

Organizer's image planner reuses `GenerationDialog(runtime_allowed=False)` and the canonical generation-plan writer. It cannot check a generator, bind local runtime models, prepare or submit a job, recover a session, or run a comparison. `generation_workflow` skips saved-runtime lookup before constructing it. Existing all-in-one and Manager defaults remain enabled by their existing host authority.

Video generation remains outside Organizer: **Animate This Image** gives the existing Manager **Plan a Video** next action without opening a video runtime.

Owners: SUITE-10B / GEN-06 / GEN-08, bounded child `SUITE-10B/organizer-offline-generation-entry`. Entry policy: `suite_core/media_organizer/entry.py`; image planning: `qt_app/generation_workflow.py`, `qt_app/generation_dialog.py`; video boundary: `qt_app/video_generation_workflow.py`. Focused generated-fixture coverage: `tests/test_organizer_generation_planning.py`.

This source checkpoint does not claim signed package delivery, live adapter qualification or full suite parity.
