# Manager video results in Organizer Ingest

Owner: GEN-06 / VID-03; delivery route: existing Media Organizer 0.5 candidate.

In Organizer, open Ingest, choose **Add Images > Review Manager Result**, and select the receipt path copied by Manager. Original videos open the existing Video Ingest review. For a trimmed clip, select its finishing receipt and then the original Manager video receipt when asked. Review the exact clip and choose Copy; the original clip and receipts stay retained, and the copied family keeps its generation/timeline lineage and needs-review status. Cancel writes no imported family.

`core/video_ingest.py::ManagerVideoIngestHandoff` binds explicitly selected bounded receipts without scanning. It reuses `VideoExecutionService.verified_video_outputs` with resize recovery disabled, `verified_trimmed_output`, and `VideoIngestService` for fresh preview/copy/retry. `qt_app/ingest_page.py::IngestPage.choose_manager_generation_result` owns the existing entry. Any required decoder uses the existing AppState admission/lifetime from a background preview; it does not install components or start a generator. The image receipt route remains with `GenerationIngestHandoff`.

Malformed, changed, foreign-workspace or incomplete receipts and changed outputs cannot reuse a preview. A trim also needs its exact original receipt; no path is inferred by scanning. An occupied destination remains protected by canonical Ingest family-copy rules. Source checks use generated transport/codec fixtures; they do not claim real decoder, model, native generation, installed delivery or full Video parity. Host execution restrictions, signed delivery and existing recovery boundaries remain in force.
