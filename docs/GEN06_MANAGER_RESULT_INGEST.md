# Review a Suite Manager result in Organizer

In Manager, open a verified image in Results & Queue and choose **Copy Receipt
Path for Organizer**. In Organizer, choose **Ingest > Add Images > Review Manager
Result**, paste that receipt path, review the copy preview, then confirm Copy.

This ports the existing canonical handoff into the current Organizer candidate.
It uses the exact saved generation request, receipt and image/sidecar hashes,
then the existing Ingest preview, verified transfer and recovery owners. A changed
result needs a fresh review. The source stays in Manager; the Ingest copy retains
generation lineage and needs review. Prompt text does not confirm Tags or a
Description. Cancelling a preview imports nothing.

Owners: GEN-06 / FLOW-01 / SUITE-10B. Delivery uses the existing unpublished
Organizer 0.5.0 package alongside Manager 0.6.0. This source change grants no
Root/Reader launch or library access. Signed installed delivery and full suite
parity remain separate open outcomes.

Implementation owners: `core/generation_execution.py::GenerationIngestHandoff`
pins the saved result; `core/ingest_import.py::IngestImportPlanningService`
revalidates the pinned image family; `qt_app/ingest_page.py::IngestPage.choose_manager_generation_result`
opens the normal preview. Public regression coverage is
`tests/test_manager_result_ingest.py`.
