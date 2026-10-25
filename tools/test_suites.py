"""Plan and run proportional CollectiGen pytest suites.

The ordinary ``pytest -q`` command remains the complete release gate. This
module makes smaller verification repeatable without pretending that a focused
run proves unrelated application behavior.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class Suite:
    description: str
    test_patterns: tuple[str, ...]


SUITES: dict[str, Suite] = {
    "hf-repository-inspection": Suite(
        "Pinned selected Hugging Face metadata, quantization holds and exact registry identity suggestions; no model execution.",
        ("tests/test_huggingface_repository_inspection.py", "tests/test_model_provider_evidence.py"),
    ),
    "app-capability-contracts": Suite(
        "Inert schema negotiation, independent availability dimensions and bounded recipient-owned search handoff contracts.",
        ("tests/test_app_capabilities.py", "tests/test_app_availability.py",
         "tests/test_app_handoff_contract.py"),
    ),
    "relation-native": Suite(
        "Native enrolled live-refinement snapshots and original scoped review, Undo, Save/reopen on disposable source Qt fixtures.",
        ("tests/test_relation_native_snapshot.py", "tests/test_relation_native_editor_ui.py"),
    ),
    "relation-rules": Suite(
        "Detached typed relation/rule admission, immutable dependency bindings and explicit unsupported execution.",
        ("tests/test_relation_rule_service.py",),
    ),
    "default-compatibility": Suite(
        "Pure complete-native default reconciliation, retained closure and immutable EffectiveTag projection; no activation.",
        ("tests/test_default_compatibility.py", "tests/test_default_compatibility_capture.py",
         "suite_core/tests/test_catalog_migration_preview.py"),
    ),
    "organizer-save-close": Suite(
        "First guarded Image Knowledge Save, stopped-owner callbacks, collision review and native serialized compound recommendations.",
        ("tests/test_image_data_editor_first_save.py",
         "tests/test_content_safety_close_callback.py",
         "tests/test_tag_collision_review.py",
         "tests/test_tag_active_refinement.py"),
    ),
    "connected-version": Suite(
        "Source observations/projections preserve connection identity, availability and truthful version status.",
        ("tests/test_connected_version_observation.py",
         "tests/test_connected_version_projection.py"),
    ),
    "organizer-runtime-source": Suite(
        "Explicit Organizer portable profile policy and retained startup/profile navigation seams.",
        ("tests/test_organizer_portable_runtime.py",
         "tests/test_organizer_startup_navigation.py",
         "tests/test_organizer_startup_profile.py"),
    ),
    "organizer-video-qualification": Suite(
        "Proposed Video dependency tests and separately invoked disposable actual decoder/official-wheel qualification.",
        ("tests/test_organizer_video_connected_native.py",
         "tests/test_organizer_video_dependencies.py",
         "tests/test_organizer_video_official_wheel.py"),
    ),
    "organizer-help-setup": Suite(
        "Organizer-owned Help identity/notes and supported Setup controls using stock Qt fixtures.",
        ("suite_core/tests/test_media_organizer_help_identity.py",
         "suite_core/tests/test_media_organizer_setup_scope.py"),
    ),
    "ollama-owned-runtime": Suite(
        "Verified owned publication, retained process lifecycle, worker admission and synthetic model-transfer seams.",
        ("tests/test_ollama_package_preparation.py", "tests/test_ollama_publication_plan.py",
         "tests/test_ollama_owned_executor.py", "tests/test_installation_plus_filename.py",
         "tests/test_first_live_cleanup.py", "tests/test_manager_ollama_service.py",
         "tests/test_ollama_pull_stream.py", "tests/test_manager_ollama_runtime.py",
         "tests/test_ollama_cache_admission.py", "tests/test_ollama_cache_binding.py",
         "tests/test_owned_external_command_spawn.py","tests/test_manager_direct_admission.py"),
    ),
    "module-management-ui": Suite(
        "Read-only Setup Modules inventory, exact dependency previews, stale holds and synthetic Qt controls.",
        ("tests/test_module_management.py", "tests/test_modules_page.py",
         "tests/test_module_card.py",
         "tests/test_resource_capacity.py",
        ),
    ),
    "suite-installation-registry": Suite(
        "Inactive pure installation locator, existing explicit inventories and synthetic two-consumer proof.",
        ("suite_core/tests/test_installation_registry.py",),
    ),
    "offline-encyclopedia-export": Suite(
        "Synthetic frozen native saved-view export and atomic publication holds.",
        ("suite_core/tests/test_offline_encyclopedia_export.py",),
    ),
    "standalone-encyclopedia": Suite(
        "Standalone Encyclopedia/native catalog review and separate source distribution closure.",
        ("suite_core/tests/test_encyclopedia.py", "suite_core/tests/test_encyclopedia_distribution.py",
         "tests/test_encyclopedia_compiled_routing.py",
         "tests/test_encyclopedia_custody.py",
         "tests/test_encyclopedia_editing.py",
         "tests/test_encyclopedia_entry_adapter.py",
         "tests/test_encyclopedia_native_qt_runner.py",
         "tests/test_encyclopedia_qt_qualification.py",
         "tests/test_encyclopedia_root_custody.py",
         "tests/test_encyclopedia_data_compatibility.py", "tests/test_encyclopedia_named_codecs.py",
         "tests/test_effective_tag_snapshot_reader.py",
         "tests/test_encyclopedia_catalog.py",
         "tests/test_encyclopedia_connection.py",
         "tests/test_encyclopedia_empty_defaults.py",
         "tests/test_encyclopedia_enrollment.py",
         "tests/test_encyclopedia_entry_control.py",
         "suite_core/tests/test_encyclopedia_root_dispatch.py",
         "suite_core/tests/test_catalog_migration_preview.py",
         "suite_core/tests/test_foundation_retirement_review.py",
        "suite_core/tests/test_encyclopedia_image_gallery.py",
        "suite_core/tests/test_organizer_encyclopedia_actual_dataset_controls.py",
        "suite_core/tests/test_encyclopedia_entry_actions_ui.py",
        "suite_core/tests/test_encyclopedia_enrollment_ui.py",
        "suite_core/tests/test_encyclopedia_real_controller_ui.py",
        "suite_core/tests/test_encyclopedia_content_entry.py",
         "tests/test_encyclopedia_qualification_build.py",
        "tests/test_encyclopedia_frozen_subset.py",
         "suite_core/tests/test_encyclopedia_root_recovery_ui.py",
         "suite_core/tests/test_tag_database_epoch.py",
        "suite_core/tests/test_tag_database_apply_delivery.py",
         "suite_core/tests/test_encyclopedia_downloaded_defaults.py",
         "suite_core/tests/test_encyclopedia_editor_shell.py",
         "suite_core/tests/test_encyclopedia_restore_category_ui.py",
         "suite_core/tests/test_encyclopedia_adapter_delivery.py",
         "suite_core/tests/test_encyclopedia_organizer_sender.py",
         "tests/test_encyclopedia_failed_owner_recovery.py",
         "tests/test_encyclopedia_help_commands.py",
         "tests/test_encyclopedia_installation.py",
          "tests/test_encyclopedia_install_compatibility.py",
          "suite_core/tests/test_encyclopedia_observation_ui.py",
          "suite_core/tests/test_encyclopedia_setup_presentation_ui.py",
          "suite_core/tests/test_encyclopedia_pending_task_ui.py",
          "suite_core/tests/test_encyclopedia_startup_progress_ui.py",
         "tests/test_encyclopedia_qualified_vocabulary.py",
         "tests/test_encyclopedia_root_admission.py",
         "tests/test_encyclopedia_save_retry_publication.py",
         "tests/test_encyclopedia_update_check.py",
         "tests/test_encyclopedia_update_data_contract.py",
         "tests/test_standalone_encyclopedia_update.py",
        ),
    ),
    "shared-distribution": Suite(
        "Explicit shared package/source import closure and isolated copy/privacy fixtures.",
        ("suite_core/tests/test_shared_distribution.py",),
    ),
    "shared-pack-preview": Suite(
        "Partial Pack DTO privacy/bounds and native container differential parity.",
        ("suite_core/tests/test_pack_preview.py",),
    ),
    "shared-tag-lookup": Suite(
        "Detached Tag lookup and inactive native snapshot adapter; synthetic standalone/native parity.",
        ("suite_core/tests/test_shared_tag_lookup.py",),
    ),
    "suite-backend-contracts": Suite(
        "Independent backend contracts, synthetic consumer/resource proof, diagnostics, acquisition, and unused Ollama package preparation.",
        ("suite_core/tests/test_*.py", "tests/test_ollama_package_preparation.py",
         "tests/test_component_acquisition.py", "tests/test_suite_diagnostics.py",
         "tests/test_diagnostic_bridge_signal.py", "tests/test_diagnostic_host_candidate.py",
         "tests/test_diagnostic_lifecycle_binding.py", "tests/test_diagnostic_lifecycle_handoff.py",
         "tests/test_diagnostic_manager_help_candidate.py", "tests/test_diagnostic_manager_provider_candidate.py",
         "tests/test_diagnostic_patched_lifecycle.py", "tests/test_diagnostic_review_candidate.py",
         "tests/test_component_exact_offer.py",
         "tests/test_component_release_identity.py",
         "tests/test_component_release_presentation.py",
         "tests/test_component_release_transport.py",
         "tests/test_manager_backend_setup_flow.py",
         "tests/test_manager_dependency_status.py",
         "tests/test_manager_distribution_builder.py",
         "tests/test_manager_failed_normal_recovery.py",
         "tests/test_manager_llamacpp_provider.py",
         "tests/test_manager_native_member.py",
         "tests/test_manager_native_repair.py",
         "tests/test_manager_ollama_chat_stream.py",
         "tests/test_manager_ollama_workspace.py",
         "tests/test_manager_owned_package_entry.py",
         "tests/test_manager_owned_runtime_entry.py",
         "tests/test_manager_production_stage.py",
         "tests/test_manager_readiness_deadline.py",
         "tests/test_manager_runtime_sequences.py",
         "tests/test_manager_sealed_qualification_transport.py",
         "tests/test_manager_stock_hook_admission.py",
         "tests/test_manager_stock_hook_observation.py",
         "tests/test_manager_stock_hook_pipe.py",
         "tests/test_manager_stock_hook_provider.py",
         "tests/test_manager_stock_hook_transfer.py",
         "tests/test_manager_successor_admission.py",
         "tests/test_manager_update_continuation.py",
         "tests/test_manager_update_host.py",
         "tests/test_manager_update_wire_retirement.py",
         "tests/test_organizer_example_images.py",
         "tests/test_organizer_freeze_inputs.py",
        ),
    ),
    "history-versions": Suite(
        "Scoped image and structured-data capture, immutable history, selective restore and restart recovery.",
        ("tests/test_history_*.py", "tests/test_ingest_history.py", "tests/test_dataset_history.py", "tests/test_image_knowledge_history.py",
         "tests/test_image_record_integrity.py", "tests/test_image_record_recovery.py",
         "tests/test_dataset_usage_coverage.py",
         "tests/test_organizer_scan_observation_join.py",
         "tests/test_organization_history*.py", "tests/test_organization_copy_history.py",
         "tests/test_organization_recycle_history.py", "tests/test_recycle_history*.py",
         "tests/test_ingest_organization_history.py", "tests/test_dataset_organization_history.py", "tests/test_problems_history.py",
         "tests/test_collection_history*.py", "tests/test_collection_observation*.py", "tests/test_structured_history*.py"),
    ),
    "training-experiments": Suite(
        "Training experiment identity, evaluation, metrics, measured trials and qualified adapters.",
        ("tests/test_training_experiments*.py", "tests/test_training_evaluation*.py",
         "tests/test_training_checkpoint_export*.py", "tests/test_training_metrics*.py",
         "tests/test_training_trial.py", "tests/test_training_anima*.py",
         "tests/test_training_presets.py", "tests/test_training_resource_guidance.py",
         "tests/test_training_validation*.py", "tests/test_training_workflow_controller.py",
         "tests/test_training_run_dialog.py", "tests/test_training_page.py", "tests/test_training_family_routing.py",
         "tests/test_training_run_index.py", "tests/test_training_retained_history.py", "tests/test_training_plan_drafts.py",
         "tests/test_training_profile_companions.py",
         "tests/test_collection_training_export_ui.py", "tests/test_collection_training_drafts.py",
         "tests/test_collection_training_export_shell_ui.py",
         "tests/test_training_package_reopen.py", "tests/test_training_generation_handoff.py"),
    ),
    "folder-locations": Suite(
        "Central Setup folder editing and existing storage consumers.",
        ("tests/test_folder_locations.py", "tests/test_folder_locations_ui.py",
         "tests/test_model_storage_policy_editor.py", "tests/test_resource_browser_settings.py",
         "tests/test_setup_polish.py", "tests/test_settings_navigation.py",
         "tests/test_model_workspace_ui.py", "tests/test_resolution_dialog.py",
         "tests/test_external_write_policy.py"),
    ),
    "learning-library": Suite(
        "Bounded offline instructional topic indexing and literal search.",
        ("tests/test_learning_topic_search.py", "tests/test_learning_help_catalog.py",
         "tests/test_learning_help_search_ui.py", "tests/test_learning_help_entry_ui.py"),
    ),
    "resolution": Suite(
        "Decoded resolution policy, guarded derivative versions and shared gallery/dialog integration.",
        ("tests/test_resolution_*.py", "tests/test_image_derivatives.py", "tests/test_image_format_inspection.py",
         "tests/test_image_format_details_ui.py", "tests/test_image_format_details_entry_ui.py"),
    ),
    "concept-groups": Suite(
        "Training collections, automatic rules, guarded persistence and frozen training handoff.",
        ("tests/test_concept_group*.py", "tests/test_collection_rules.py"),
    ),
    "generation": Suite(
        "Neutral generation plans, explicit local execution, outcomes and shared UI handoffs.",
        ("tests/test_generation_*.py", "tests/test_prompt_preset_browser.py", "test_generation_adapters.py", "tests/test_swarm_runtime.py",
         "tests/test_swarm_preset_import.py", "tests/test_swarm_preset_source.py", "tests/test_swarm_preset_import_ui.py",
         "tests/test_swarm_setup_file_import.py",
         "tests/test_swarm_model_paths*.py", "tests/test_swarm_configuration_backup*.py",
         "tests/test_swarm_source_inventory.py", "tests/test_swarm_companion_inventory.py",
         "tests/test_swarm_companion_installation.py",
         "tests/test_swarm_companion_qualification.py",
         "tests/test_swarm_companion_package_ui.py",
         "tests/test_swarm_companion_loader_preparation.py",
         "tests/test_swarm_recovery_scope.py",
         "tests/test_swarm_companion_host_qualification.py", "tests/test_video_*.py",
         "tests/test_comfy_video_*.py", "tests/test_protected_video_qualification.py"),
    ),
    "model-workflows":Suite(
        "Model identities, explicit provider evidence, acquisition and workflow guidance.",
        ("tests/test_model_resource_*.py", "tests/test_model_workspace*.py", "tests/test_model_training_reference.py",
         "tests/test_model_folder_discovery.py", "tests/test_model_scan_review.py", "tests/test_model_import_*.py", "tests/test_model_artwork*.py",
         "tests/test_model_selection*.py", "tests/test_model_selector_ui.py", "tests/test_managed_model_identity.py",
         "tests/test_model_provider_evidence.py", "tests/test_model_workflow_guidance*.py",
         "tests/test_model_organization.py", "tests/test_model_setup_recommendations.py",
         "tests/test_model_detection.py", "tests/test_model_engine_locations.py",
         "tests/test_model_relocation.py", "tests/test_model_file_retirement.py",
         "tests/test_model_relocation_ui.py", "tests/test_model_storage_policy.py",
         "tests/test_model_cache.py", "tests/test_model_cache_reference.py", "tests/test_model_cache_ui.py", "tests/test_model_hot_paths.py",
         "tests/test_model_cache_capacity.py", "tests/test_model_capacity_ui.py",
         "tests/test_model_execution_cache.py", "tests/test_model_usage*.py", "tests/test_model_storage_recommendations.py",
         "tests/test_model_companion_readiness.py", "tests/test_model_companion_readiness_ui.py",
         "tests/test_model_companion_workflow_ui.py",
         "tests/test_swarm_model_import.py", "tests/test_swarm_model_import_ui.py", "tests/test_swarm_model_artwork.py",
         "tests/test_swarm_onboarding.py", "tests/test_swarm_onboarding_receipts.py",
         "tests/test_swarm_onboarding_settings.py", "tests/test_swarm_onboarding_ui.py",
         "tests/test_swarm_onboarding_workflow_ui.py",
         "tests/test_model_use_leases.py", "tests/test_resource_browser_view.py",
         "tests/test_video_contract_proposal.py"),
    ),
    "core-smoke":Suite(
        "Root-level core, Image Knowledge, and generation-adapter checks.",
        ("test_core.py","test_generation_adapters.py","test_image_knowledge.py"),
    ),
    "setup-ai":Suite(
        "Setup, onboarding, local-AI configuration, dependencies, and model evaluation.",
        (
            "tests/test_onboarding_ai_opt_in.py",
            "tests/test_onboarding_safe_stop.py",
            "tests/test_readiness_result_regression.py",
            "tests/test_recent_inference_slowdown.py",
            "tests/test_task_cost_baselines.py",
            "tests/test_total_task_eta.py",
            "tests/test_wd_adaptive_timing.py",

            "tests/test_setup_*.py","tests/test_onboarding.py",
            "tests/test_dependency_removal.py","tests/test_ai_*.py",
            "tests/test_optional_runtime_import_check.py",
            "tests/test_background_jobs.py",
            "tests/test_joy_*.py","tests/test_florence_caption.py",
            "tests/test_model_packs.py","tests/test_trainer_*.py",

         "tests/test_background_execution_scope.py",
        ),
    ),
    "settings-startup":Suite(
        "Settings compatibility, startup state, profiles, defaults, and recovery.",
        (
            "tests/test_application_upgrade.py","tests/test_default_overrides.py",
            "tests/test_image_authority_initialization.py",
            "tests/test_library_profiles.py","tests/test_offline_first.py",
            "tests/test_onboarding.py","tests/test_pytest_environment.py",
            "tests/test_native_crash_diagnostics.py",
            "tests/test_setup_*.py","tests/test_persistent_reload_scope.py",

         "tests/test_application_profile.py",
         "tests/test_startup_profile_root.py",
        ),
    ),
    "shell-ui":Suite(
        "Shared Qt shell, dialogs, navigation, selectors, and cross-page presentation.",
        (
            "tests/test_context_help_navigation.py",

            "tests/test_navigation_icons.py","tests/test_settings_navigation.py",
            "tests/test_command_*.py","tests/test_workspace_commands.py",
            "tests/test_dialog_commands.py",
            "tests/test_secondary_workspace_commands.py",
            "tests/test_description_exclusions_dialog.py",
            "tests/test_*_ui.py","tests/test_dashboard.py",
            "tests/test_design_language.py","tests/test_guided_review.py",
            "tests/test_gallery_*.py",
            "tests/test_notification_trace.py","tests/test_protected_previews.py",
            "tests/test_status_footer.py","tests/test_status_notifications.py",
            "tests/test_searchable_selectors.py","tests/test_vocabulary_dialog.py",
            "tests/test_workers.py","tests/test_foreground_readers.py",
            "tests/test_deferred_editor_delivery.py",
            "tests/test_tooltips.py",
            "tests/test_training_page.py", "tests/test_training_workspace*.py",
            "tests/test_training_package_reopen.py", "tests/test_training_generation_handoff.py",
            "tests/test_visual_activity_reservation.py",
            "tests/test_qt_visual_inspect.py",

         "tests/test_organizer_prompt_dialog.py",
        ),
    ),
    "ingest-dataset":Suite(
        "Ingest, Dataset, Image Knowledge, preparation, and training projections.",
        (
            "tests/test_duplicate_preparation_defaults.py",
            "tests/test_post_preparation_chip_removal.py",

            "tests/test_ingest_*.py","tests/test_dataset_*.py",
            "tests/test_image_*.py","tests/test_typed_image_knowledge.py",
            "tests/test_unified_preparation.py","tests/test_preparation_*.py",
            "tests/test_media_inventory.py","tests/test_media_library_ui.py",
            "tests/test_preferred_caption_preparation*.py",
            "tests/test_training_*.py","tests/test_export_profiles.py",
            "tests/test_model_resource_*.py",
            "tests/test_edit_service.py","tests/test_fact_expansion_policy.py",
            "tests/test_derived_refresh.py","tests/test_derived_refresh_diagnostics.py",
            "tests/test_refresh_diagnostics.py","tests/test_evidence_query.py",
            "tests/test_workflow_semantic_proof.py","tests/test_workflow_sensitivity_reuse.py",
            "tests/test_projection_*.py",
        ),
    ),
    "knowledge-tags":Suite(
        "Tag, category, relationship, conflict, and reviewed-default knowledge.",
        (
            "tests/test_ownership_acknowledgement.py",
            "tests/test_saved_tag_return_refresh.py",

            "tests/test_tag_*.py","tests/test_category_*.py",
            "tests/test_workspace_*.py",
            "tests/test_knowledge_*.py","tests/test_relationship_*.py",
            "tests/test_related_*.py","tests/test_modifier_*.py",
            "tests/test_conflict_*.py","tests/test_constraint_*.py",
            "tests/test_feral_conflicts.py",
            "tests/test_consumer_*.py","tests/test_default_*.py",
            "tests/test_allowed_dimension*.py","tests/test_entity_tag_relationships.py",
            "tests/test_confirmed_tag_identity.py",
            "tests/test_known_id_reservation.py",
            "tests/test_reviewed_tag_semantics.py",
            "tests/test_reviewed_tag_defaults_20260908.py",
            "tests/test_reviewed_color_coverage.py",
            "tests/test_breast_size_conflicts.py",
            "tests/test_pending_retired_tags.py",
            "tests/test_scoped_constraint_projection.py",
            "tests/test_reusable_value_authority.py",
            "tests/test_removed_vocabulary.py","tests/test_rule_intent_migration.py",
            "tests/test_fact_expansion_policy.py",

         "tests/test_foundation_capabilities.py",
         "tests/test_foundation_dependencies.py",
         "tests/test_legacy_modifier_runtime.py",
        ),
    ),
    "knowledge-characters":Suite(
        "Character identity, source, ownership, projection, and bridge workflows.",
        ("tests/test_character_*.py",),
    ),
    "automation":Suite(
        "Automation ownership, repositories, evidence intake, and source correction.",
        (
            "tests/test_automation_*.py","tests/test_aut04_ui.py",
            "tests/test_description_exclusions*.py",
            "tests/test_external_evidence_import.py",
            "tests/test_downstream_updates.py","tests/test_source_correction*.py",
            "tests/test_metadata_source_correction.py",
        ),
    ),
    "storage-recovery":Suite(
        "Libraries, Metadata writes, transfers, recycle, duplicates, and recovery.",
        (
            "tests/test_existing_library_restart_acceptance.py",
            "tests/test_installed_history_metadata_acceptance.py",
            "tests/test_installed_metadata_reconciliation_acceptance.py",

            "tests/test_library_*.py","tests/test_metadata_*.py",
            "tests/test_move_preview_concision.py","tests/test_move_refresh_scope.py",
            "tests/test_recycle_bin*.py","tests/test_duplicate_finder*.py",
            "tests/test_duplicate_preflight*.py",
            "tests/test_problems*.py",
            "tests/test_image_record_*.py","tests/test_verified_transfer.py",
            "tests/test_external_write_*.py","tests/test_federated_dataset.py",
            "tests/test_missing_tag_exception_transactions.py",
            "tests/test_model_resource_registry.py",
        ),
    ),
    "review-safety":Suite(
        "Review decisions, content protection, sharing, and authority safeguards.",
        (
            "tests/test_review_*.py","tests/test_share_safety*.py",
            "tests/test_content_sensitivity_scope.py",
            "tests/test_image_fact_decisions.py",
            "tests/test_external_write_policy.py",
            "tests/test_protected_previews.py",
        ),
    ),
    "release":Suite(
        "Versioning, public source, portable runtime, migrations, and offline release contracts.",
        (
            "tests/test_public_*.py","tests/test_portable_runtime.py","tests/test_distribution_readme.py",
            "tests/test_tester_documentation.py",
            "tests/test_versioning.py","tests/test_offline_first.py",
            "tests/test_brand_assets.py","tests/test_bug_reporting.py",
            "tests/test_migration_audit.py","tests/test_source_encoding.py",
            "tests/test_application_upgrade.py",
            "tests/test_application_update_cancel_classification.py",
            "tests/test_catalog_public_runtime_manifest.py", "tests/test_core_import_boundary.py",
        ),
    ),
    "tooling":Suite(
        "Repository verification, documentation, performance, and test infrastructure.",
        (
            "tests/test_testing_tools.py","tests/test_test_suites.py",
            "tests/test_codebase_map.py",
            "tests/test_profile_training_package.py",
            "tests/test_test_checkpoint.py","tests/test_qt_test_support.py",
            "tests/test_knowledge_change_review.py","tests/test_checkpoint_knowledge.py",
            "tests/test_workflow_evidence.py","tests/test_training_caption_experiment.py",
            "tests/test_training_adapter_probe.py","tests/test_training_runtime_qualification.py",
            "tests/test_training_file_inventory.py","tests/test_training_verification.py","tests/test_training_native_observation.py","tests/test_training_preflight.py",
            "tests/test_training_local_components.py",
            "tests/test_training_appcontainer.py",
            "tests/test_training_restricted_loading.py",
            "tests/test_training_offline_policy.py",
            "tests/test_training_registry_policy.py",
            "tests/test_training_wfp.py",
            "tests/test_training_startup_compatibility.py",
            "tests/test_training_capability_comparison.py",
            "tests/test_training_component_profile.py","tests/test_training_adapter_admission.py",
            "tests/test_training_tokenizer_contract.py","tests/test_training_tokenizer_runtime.py",
            "tests/test_training_selected_tokenizers.py",
            "tests/test_training_model_layout.py","tests/test_training_model_layout_runtime.py",
            "tests/test_training_tensor_contract.py","tests/test_training_tensor_runtime.py",
            "tests/test_training_sdxl_loading.py","tests/test_training_model_loading_runtime.py",
            "tests/test_training_local_processing.py",
            "tests/test_training_unet_step.py", "tests/test_training_unet_runtime.py",
            "tests/test_training_loop.py", "tests/test_training_loop_runtime.py",
            "tests/test_training_checkpoint.py",
            "tests/test_training_continuation.py",
            "tests/test_training_continuation_hooks.py",
            "tests/test_training_sdxl_hooks.py",
            "tests/test_training_continuation_execution.py",
            "tests/test_training_resume.py",
            "tests/test_training_image_effects_contract.py","tests/test_training_image_effects_runtime.py",
            "tests/test_training_dataset_package_contract.py","tests/test_training_dataset_package_runtime.py",
            "tests/test_training_package_effects.py",
            "tests/test_training_dataset_preparation.py",
            "tests/test_training_dataset_preparation_runtime.py",
            "tests/test_training_dataset_preparation_receipts.py",
            "tests/test_training_model_pairing_contract.py",
            "tests/test_training_model_pairing_runtime.py",
            "tests/test_training_conditioning_reference.py",
            "tests/test_training_conditioning_contract.py",
            "tests/test_training_conditioning_runtime.py",
            "tests/test_roadmap_slice.py","tests/test_bug_backlog_status.py",
            "tests/test_ui_journey_contract.py",
            "tests/test_documentation_archival.py",
            "tests/test_performance_architecture.py",
            "tests/test_navigation_profile_progress.py",
            "tests/test_editor_queue_performance.py",
            "tests/test_source_encoding.py","tests/test_pytest_environment.py",
        ),
    ),
}


@dataclass(frozen=True)
class ImpactRule:
    path_patterns: tuple[str,...]
    suites: tuple[str,...]
    visual: tuple[str,...]=()
    full_at_checkpoint: bool=False


IMPACT_RULES = (
    ImpactRule(("core/huggingface_repository_inspection.py", "core/model_provider_evidence.py",
                "tests/test_huggingface_repository_inspection.py"), ("hf-repository-inspection",)),
    ImpactRule(("core/default_compatibility.py", "core/default_compatibility_capture.py",
                "tests/test_default_compatibility.py", "tests/test_default_compatibility_capture.py"), ("default-compatibility",)),
    ImpactRule(("core/relation_native_snapshot.py",), ("relation-native",)),
    ImpactRule(("core/relation_rule_service.py",), ("relation-rules",)),
    ImpactRule(("core/manager_ollama_runtime.py", "core/manager_ollama_service.py",
                "core/ollama_cache_admission.py",
                "core/ollama_owned_executor.py", "core/ollama_owned_windows.py","core/manager_direct_admission.py",
                "core/ollama_publication_plan.py", "core/ollama_pull_outcome.py",
                "tests/ollama_runtime_qualification_fixture.py"), ("ollama-owned-runtime",)),
    ImpactRule(("core/application_update.py",), ("suite-backend-contracts", "release")),
    ImpactRule(("core/component_acquisition.py", "core/support_bundle.py"),
               ("suite-backend-contracts",)),
    ImpactRule(("core/__init__.py", "core/catalog_admission.py", "suite_core/catalog_identity_candidate.py",
                "suite_core/selected_catalog.py", "suite_core/knowledge/catalog_migration_preview.py",
                "suite_core/media_examples.py"), ("release",)),
    ImpactRule(("core/module_management.py", "qt_app/modules_page.py", "tests/test_module_management.py", "tests/test_modules_page.py"),
               ("module-management-ui",)),
    ImpactRule(("suite_core/installation*.py", "suite_core/INSTALLATION_*",
                "suite_core/examples/fake_module_discovery.py", "suite_core/tests/test_installation_registry.py"),
               ("suite-installation-registry",)),
    ImpactRule(("tools/offline_encyclopedia_export.py", "tools/offline_encyclopedia_fixture.py",
                "suite_core/tests/test_offline_encyclopedia_export.py"),
               ("offline-encyclopedia-export",)),
    ImpactRule(("suite_core/encyclopedia/*.py", "suite_core/encyclopedia/MODULE_MANIFEST.json",
                "suite_core/encyclopedia/RELEASE_HISTORY.json", "qt_app/browse_components.py",
                "suite_core/tests/test_encyclopedia.py", "suite_core/tests/test_encyclopedia_distribution.py"),
               ("standalone-encyclopedia",)),
    ImpactRule(("tools/Build-PortablePackage.ps1", "tools/public-source-manifest.json",
                "tools/component-source-manifest.json"),
               ("shared-distribution",)),
    ImpactRule(("suite_core/knowledge/pack_*.py", "core/knowledge_pack.py",
                "suite_core/tests/fixtures/pack_container_native_baseline.json"),
               ("shared-pack-preview",)),
    ImpactRule(("suite_core/knowledge/*.py", "suite_core/knowledge/*.json",
                "core/shared_tag_snapshot.py", "suite_core/tests/shared_tag_native_fixture.py"),
               ("shared-tag-lookup",)),
    ImpactRule(("suite_core/INVENTORY_*", "suite_core/examples/inspect_inventory.py"),
               ("suite-backend-contracts",)),
    ImpactRule(("suite_core/*.py", "suite_core/RELEASE_HISTORY.json",
                "suite_core/adapters/RELEASE_HISTORY.json", "suite_core/adapters/QUALIFICATION.json",
                "suite_core/discovery/RELEASE_HISTORY.json", "suite_core/discovery/QUALIFICATION.json",
                "suite_core/registry/RELEASE_HISTORY.json", "suite_core/registry/QUALIFICATION.json",
                "suite_core/registry/CATALOG_RELEASE_HISTORY.json", "suite_core/registry/CATALOG_QUALIFICATION.json",
                "suite_core/registry/CATALOG_UI_EXAMPLE_V1.json",
                "suite_core/registry/JOB_RELEASE_HISTORY.json", "suite_core/registry/JOB_QUALIFICATION.json",
                "suite_core/registry/JOB_UI_EXAMPLE_V1.json",
                "suite_core/COORDINATOR_RELEASE_HISTORY.json", "suite_core/COORDINATOR_QUALIFICATION.json",
                "suite_core/COORDINATOR_EXAMPLE_V1.json",
                "suite_core/tests/fixtures/*.json"), ("suite-backend-contracts",)),
    ImpactRule(
        ("core/history_versions.py", "core/image_knowledge_history.py", "core/history_legacy.py",
         "core/history_configuration.py", "core/history_workflow.py", "core/history_browser.py",
         "qt_app/history_versions_dialog.py", "qt_app/history_workflow.py", "tools/history_versions_visual.py",
         "core/image_tag_batch.py", "qt_app/image_tag_batch_dialog.py", "tools/history_workspace_visual.py",
         "core/image_identity_correction.py", "qt_app/image_identity_correction_widget.py", "tools/history_identity_visual.py",
         "core/conflict_review.py", "qt_app/conflict_review_dialog.py", "tools/history_conflict_visual.py",
         "core/image_schema_migration.py",
         "core/history_moves.py",
         "core/organization_history.py", "core/structured_history.py", "core/structured_history_configuration.py",
         "core/organization_copy_history.py", "core/organization_history_browser.py", "core/organization_history_scope.py",
         "core/organization_recycle_history.py", "core/history_recycle_support.py", "core/recycle_history_browser.py",
         "core/collection_history.py", "core/collection_history_browser.py", "core/structured_history_workflow.py",
         "qt_app/history_domain_panel.py", "qt_app/history_workspace_dialog.py", "qt_app/collection_history_panel.py",
         "tools/history_domains_visual.py", "tools/recycle_history_visual.py",
         "qt_app/structured_history_locations.py",
         "core/history_projection.py", "core/image_knowledge_reconciliation.py", "core/metadata_backup.py",
         "qt_app/metadata_backup_dialog.py", "tools/history_metadata_visual.py",
         "core/source_correction.py", "core/metadata_source_correction.py", "core/projection_exclusion.py",
         "qt_app/source_correction_dialog.py", "tools/history_source_visual.py",
         "core/guided_review.py", "qt_app/guided_review_dialog.py", "tools/history_guided_visual.py",
         "core/image_tag_removal.py", "qt_app/image_tag_removal_dialog.py", "tools/history_tag_removal_visual.py",
         "qt_app/ingest_page.py", "qt_app/dataset_page.py", "tools/ingest_history_visual.py", "tools/dataset_history_visual.py",
         "core/preparation_workflow.py", "core/batch_preparation.py", "core/ai_interrogation.py",
         "core/preferred_caption_preparation.py", "core/image_authority_initialization.py", "core/automatic_ai_analysis.py"),
        ("history-versions",),
    ),
    ImpactRule(
        ("core/video_*.py", "core/comfy_video_*.py", "qt_app/video_*.py",
         "core/swarm_preset_import.py", "core/swarm_preset_source.py", "qt_app/swarm_preset_import_dialog.py",
         "tools/video_*_visual.py", "core/generation_*.py", "qt_app/generation_*.py", "qt_app/prompt_preset_browser.py",
         "tools/generate_section_visual.py", "tools/generation_exploration_visual.py",
         "tools/problems_repair_visual.py", "tools/generation_regions_visual.py",
         "tools/qualify_video_offline.py", "tools/qualify_protected_video.py"),
        ("generation",),
    ),
    ImpactRule(
        ("runtime_contract.py", "requirements-runtime.lock", "requirements.txt",
         "tools/Build-PortableRuntime.ps1", "tools/Audit-PublicComponents.py",
         "tools/public-components.json"),
        ("release",),
    ),
    ImpactRule(
        ("qt_app/workflow_context.py",),
        ("generation", "model-workflows", "resolution", "ingest-dataset"),
    ),
    ImpactRule(
        ("tools/dialog_ribbon_visual.py", "tools/gallery_filters_visual.py"),
        ("shell-ui",),
    ),
    ImpactRule(
        ("core/folder_locations.py", "qt_app/folder_locations.py", "qt_app/model_storage_policy_editor.py", "tools/folder_locations_visual.py"),
        ("folder-locations",),
    ),
    ImpactRule(
        ("core/learning_topic_search.py", "core/learning_help_catalog.py",
         "qt_app/learning_help_search.py"),
        ("learning-library",),
    ),
    ImpactRule(
        ("core/dataset_usage_coverage.py", "core/organizer_scan_observation_join.py",
         "tests/test_dataset_usage_coverage.py", "tests/test_organizer_scan_observation_join.py"),
        ("history-versions",),
    ),
    ImpactRule(
        ("core/resolution_*.py", "core/image_derivatives.py", "core/image_format_inspection.py", "qt_app/resolution_*.py", "qt_app/image_format_details.py", "tools/resolution_visual.py", "tools/resolution_scale.py", "tools/qualify_resolution_ai.py"),
        ("resolution",),
    ),
    ImpactRule(
        ("core/concept_groups.py", "core/collection_rules.py", "qt_app/concept_group_dialog.py"),
        ("concept-groups",),
    ),
    ImpactRule(
        ("core/generation_planning.py", "core/generation_execution.py", "core/generation_transport.py", "core/swarm_runtime.py",
         "core/swarm_source_inventory.py", "core/swarm_companion_inventory.py",
         "core/swarm_companion_installation.py",
         "core/swarm_companion_qualification.py",
         "qt_app/swarm_companion_package_dialog.py", "tools/swarm_companion_package_visual.py",
         "integrations/swarmui/CollectiGenReadOnlyInventory/*.cs",
         "integrations/swarmui/CollectiGenReadOnlyInventory/*.csproj",
         "tests/fixtures/swarm_companion_contract/*.cs", "tools/check_swarm_companion_contract.ps1",
         "tests/fixtures/swarm_companion_host/*.cs", "tools/qualify_swarm_companion_host.ps1",
         "tools/prepare_swarm_companion_loader.py",
         "core/swarm_model_paths.py", "core/model_engine_locations.py", "core/model_hot_paths.py", "qt_app/swarm_model_paths_dialog.py",
         "core/swarm_configuration_backup.py", "qt_app/swarm_configuration_backup_dialog.py",
         "core/swarm_recovery_scope.py",
         "qt_app/generation_dialog.py", "qt_app/generation_workflow.py", "tools/generation_visual.py"),
        ("generation",),
    ),
    ImpactRule(
        ("core/model_workspace.py", "core/model_scan_review.py", "core/model_import_*.py", "qt_app/model_import_plan_dialog.py", "core/model_artwork.py", "qt_app/model_artwork_dialog.py", "core/model_training_reference.py", "tools/model_training_reference_visual.py",
         "core/model_detection.py", "core/model_relocation.py", "core/model_file_retirement.py",
         "qt_app/model_relocation_dialog.py", "qt_app/model_cache_dialog.py", "core/model_storage_policy.py", "core/model_cache.py", "core/model_cache_reference.py",
         "core/model_execution_cache.py", "core/model_usage.py", "core/model_storage_recommendations.py",
         "core/model_cache_capacity.py", "qt_app/model_capacity_review.py", "tools/model_capacity_visual.py",
         "qt_app/model_usage_dialog.py", "tools/model_placement_visual.py",
         "core/model_companion_readiness.py", "qt_app/model_companion_readiness_dialog.py", "qt_app/model_companion_workflow.py",
         "core/swarm_model_import.py", "qt_app/swarm_model_import_dialog.py", "core/swarm_model_artwork.py",
         "core/swarm_onboarding.py", "core/swarm_onboarding_receipts.py",
         "qt_app/swarm_onboarding_dialog.py", "qt_app/swarm_onboarding_workflow.py",
         "core/model_use_leases.py", "core/resource_browser_view.py",
         "core/model_folder_discovery.py", "tools/model_workflow_visual.py",
         "core/model_provider_evidence.py", "core/model_workflow_guidance.py",
         "core/model_selection.py", "core/managed_model_identity.py", "qt_app/model_selector_dialog.py", "qt_app/model_resource_browser.py",
         "qt_app/model_workspace_dialog.py", "qt_app/model_workflow_guidance_dialog.py", "data/model_workflow_baseline.json"),
        ("model-workflows",),
    ),
    ImpactRule(
        ("data/defaults/*.json", "tools/tag-semantic-audits/*.json", "tools/tag-identity-reviews/*.json", "tools/km05-consumer-applicability-matrix.json"),
        ("knowledge-tags", "knowledge-characters", "review-safety", "release"),
        full_at_checkpoint=True,
    ),
    ImpactRule(
        ("core/dataset*.py", "core/workflow_state_cache.py", "core/media_inventory.py", "qt_app/media_library_dialog.py", "tools/mixed_media_visual.py"),
        ("ingest-dataset", "storage-recovery"),
    ),
    ImpactRule(
        ("core/model_resource_registry.py","qt_app/model_resource_dialog.py"),
        ("ingest-dataset", "storage-recovery", "shell-ui"), ("--training-candidate-plan",),
    ),
    ImpactRule(
        ("qt_app/onboarding_dialog.py","core/onboarding.py"),
        ("setup-ai", "settings-startup"), ("--onboarding-ai",),
    ),
    ImpactRule(
        ("qt_app/pages.py",),
        ("setup-ai", "shell-ui"), ("--ai-tabs",),
    ),
    ImpactRule(
        ("qt_app/dependency*.py","core/dependenc*.py","core/ai_*.py"),
        ("setup-ai",),
    ),
    ImpactRule(
        ("core/trainer_*.py", "core/setup_operation_queue.py", "tools/trainer_runtime_health_worker.py",
         "tools/trainer_runtime_*.json", "tools/training_distribution_assets.json", "tools/trainer-sources/**", "tools/trainer-tokenizers/**"),
        ("setup-ai", "storage-recovery", "tooling"),
    ),
    ImpactRule(
        ("qt_app/training_setup_dialog.py", "qt_app/training_execution_setup_dialog.py", "qt_app/training_run_dialog.py", "qt_app/training_workflow_controller.py", "qt_app/training_history_dialog.py", "qt_app/training_restore_dialog.py"),
        ("setup-ai", "ingest-dataset", "shell-ui"), ("--training-candidate-plan",),
    ),
    ImpactRule(
        ("core/training_experiments.py", "core/training_retained_history.py", "core/training_evaluation.py", "core/training_checkpoint_export.py",
         "core/training_metrics.py", "core/training_trial.py", "core/training_anima*.py",
         "core/training_presets.py", "core/training_resource_guidance.py",
         "core/collection_training_drafts.py",
         "core/training_validation*.py", "qt_app/training_experiments_dialog.py",
         "qt_app/training_evaluation_dialog.py", "qt_app/training_checkpoint_export_dialog.py",
         "qt_app/training_metrics_widget.py", "qt_app/training_anima_dialog.py",
         "qt_app/training_validation_dialog.py", "tools/qualify_training_anima.py",
         "tools/qualify_training_validation.py", "tools/training_evaluation_visual.py",
         "tools/training_anima_qualification.json", "tools/training_anima_native_evidence.json", "tools/training_validation_profile.json",
         "tools/training_validation_evidence.json"),
        ("training-experiments",),
    ),
    ImpactRule(
        ("core/settings.py",),
        ("settings-startup", "setup-ai"), full_at_checkpoint=True,
    ),
    ImpactRule(
        ("qt_app/ingest*.py","qt_app/dataset*.py","core/image_*.py",
         "core/*preparation*.py","core/*training*.py","tests/training_adapter_fixtures.py","tests/training_process_fixture.py","tests/training_appcontainer_fixture.py","tests/training_wfp_fixture.py","tests/training_wfp_owner_fixture.py","tests/training_lpac_fixture.py","tests/training_startup_fixture.py","tests/training_capability_fixture.py","tests/training_component_fixtures.py"),
        ("ingest-dataset", "review-safety"),
    ),
    ImpactRule(
        ("qt_app/training_candidate*.py","qt_app/training_duplicate*.py","qt_app/training_adapter*.py","qt_app/training_package_dialog.py",
         "qt_app/training_caption_review_view.py"),
        ("ingest-dataset", "shell-ui"), ("--training-candidate-plan",),
    ),
    ImpactRule(
        ("qt_app/preparation_impact_dialog.py","qt_app/preparation_controls.py"),
        ("ingest-dataset", "shell-ui"), ("--preparation-impact",),
    ),
    ImpactRule(
        ("qt_app/automation*.py","core/automation*.py",
         "core/description_exclusions.py","qt_app/description_exclusions_dialog.py",
         "core/external_evidence*.py","core/source_correction*.py"),
        ("automation",),
    ),
    ImpactRule(
        ("core/character*.py","qt_app/character*.py"),
        ("knowledge-characters", "knowledge-tags"),
    ),
    ImpactRule(
        ("core/tag*.py","core/*relationship*.py","core/knowledge*.py",
         "core/record_layout.py","core/workspace_record_storage.py","core/pattern_registry.py",
         "qt_app/*knowledge*.py","qt_app/*relationship*.py"),
        ("knowledge-tags",),
    ),
    ImpactRule(
        ("core/library*.py","core/metadata*.py","core/recycle*.py",
         "core/*transfer*.py","core/*duplicate*.py"),
        ("storage-recovery",),
    ),
    ImpactRule(
        ("qt_app/*.py",),
        ("shell-ui",), ("smoke",),
    ),
    ImpactRule(
        ("qt_visual_test.py",),
        ("shell-ui",), ("smoke",),
    ),
    ImpactRule(
        ("tools/qualify_training_runtime.py", "tools/qualify_training_reference.py", "tools/training_execution_worker.py", "tools/training_execution_readiness_worker.py", "tools/training_reference_worker.py", "tools/qualify_wfp_controls.py", "tools/training_runtime_probe.py", "tools/training_runtime_sources.json", "tools/training_dataset_package.json", "tools/training_dataset_profile_worker.py", "tools/training_verification_worker.py"),
        ("ingest-dataset", "tooling"),
    ),
    ImpactRule(
        ("tools/training_execution_*.json", "tools/training_sdxl_layout.json"),
        ("ingest-dataset", "tooling", "release"),
    ),
    ImpactRule(
        ("Start_CollectiGen.bat", "tools/Setup-Development.ps1"),
        ("settings-startup", "release"),
    ),
    ImpactRule(
        ("tools/ui_journey_contract.py", "docs/UI_JOURNEYS.json", ".agents/skills/collectigen-ui-review/SKILL.md"),
        ("tooling",),
    ),
    ImpactRule(
        ("tools/*.py","tools/*.ps1"),
        ("tooling",),
    ),
    ImpactRule(
        (".gitattributes","version.py","tools/public-source-manifest.json",
         "tools/public-components.json",".github/**"),
        ("release",), full_at_checkpoint=True,
    ),
    ImpactRule(
        ("docs/media/**",),
        ("release",),
    ),
    ImpactRule(
        ("conftest.py","pytest.ini"),
        ("tooling",), full_at_checkpoint=True,
    ),
    ImpactRule(
        ("core/__init__.py","qt_main.py"),
        ("settings-startup", "shell-ui"), full_at_checkpoint=True,
    ),
    ImpactRule(
        ("core/*.py",),
        ("core-smoke",), full_at_checkpoint=True,
    ),
)


DOCUMENTATION_PATTERNS = (
    "tools/knowledge-change-reviews*.json","*.md","docs/*.md","docs/**/*.md","LICENSE*","NOTICE*","SUPPORT.md",
    "PRIVACY.md","SECURITY.md","CONTRIBUTING.md","FUNDING.md",
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def canonical_test_modules(root: Path = ROOT) -> tuple[str, ...]:
    paths = [*root.glob("test_*.py"), *(root / "tests").glob("test_*.py"),
             *(root / "suite_core" / "tests").glob("test_*.py")]
    return tuple(sorted(path.relative_to(root).as_posix() for path in paths))


def is_public_source_tree(root: Path = ROOT) -> bool:
    """Return whether *root* has a well-formed one-way public-source inventory."""
    manifest_path = root / "public-source.json"
    if (
        not manifest_path.is_file()
        or (root / "tools" / "Review-KnowledgePromotion.py").exists()
    ):
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    files = manifest.get("files")
    digest = str(manifest.get("reviewed_defaults_digest") or "")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("fresh_history_required") is not True
        or manifest.get("manifest_excludes_self") is not True
        or not isinstance(manifest.get("allowlisted_source_file_count"), int)
        or manifest["allowlisted_source_file_count"] <= 0
        or not isinstance(files, dict)
        or "reviewed-defaults.diff.json" not in files
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest.casefold())
    ):
        return False
    for relative, record in files.items():
        path = Path(str(relative))
        checksum = str(record.get("sha256") or "") if isinstance(record, dict) else ""
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in str(relative)
            or not isinstance(record, dict)
            or not isinstance(record.get("size_bytes"), int)
            or record["size_bytes"] < 0
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum.casefold())
            or not (root / path).is_file()
            or (root / path).is_symlink()
        ):
            return False
    return True


def suite_test_modules(name: str, root: Path = ROOT) -> tuple[str, ...]:
    if name == "full":
        return canonical_test_modules(root)
    try:
        suite = SUITES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown test suite: {name}") from exc
    return tuple(
        path
        for path in canonical_test_modules(root)
        if _matches(path, suite.test_patterns)
    )


def validate_suite_manifest(root: Path = ROOT) -> tuple[str, ...]:
    errors = []
    modules = set(canonical_test_modules(root))
    require_every_pattern = not is_public_source_tree(root)
    covered = set()
    for name, suite in SUITES.items():
        matched = set(suite_test_modules(name, root))
        if not matched and require_every_pattern:
            errors.append(f"Suite {name!r} matches no canonical test modules.")
        covered.update(matched)
        for pattern in suite.test_patterns:
            if require_every_pattern and not any(
                fnmatchcase(path, pattern) for path in modules
            ):
                errors.append(
                    f"Suite {name!r} contains stale pattern {pattern!r}."
                )
    for module in sorted(modules - covered):
        errors.append(f"Canonical test module is not assigned to a domain suite: {module}")
    for rule in IMPACT_RULES:
        for name in rule.suites:
            if name not in SUITES:
                errors.append(f"Impact rule names unknown suite {name!r}.")
    return tuple(errors)


def changed_tracked_paths(root: Path = ROOT) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--relative", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(sorted({
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines() if line.strip()
    }))


def normalize_requested_paths(
    paths: Iterable[str], root: Path = ROOT
) -> tuple[str, ...]:
    normalized = []
    for raw in paths:
        value = Path(raw)
        if value.is_absolute():
            try:
                value = value.resolve(strict=False).relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(f"Path is outside the repository: {raw}") from exc
        if ".." in value.parts:
            raise ValueError(f"Path is outside the repository: {raw}")
        text = value.as_posix().removeprefix("./")
        if text:
            normalized.append(text)
    return tuple(sorted(set(normalized)))


def plan_for_paths(
    paths: Iterable[str], root: Path = ROOT
) -> dict[str, object]:
    requested = normalize_requested_paths(paths, root)
    suites = set()
    direct_tests = set()
    visuals = set()
    documentation = []
    unknown = []
    full_at_checkpoint = False
    for path in requested:
        if (path.startswith(("tests/test_", "suite_core/tests/test_")) or ("/" not in path and path.startswith("test_"))) and path.endswith(".py"):
            direct_tests.add(path)
            continue
        if _matches(path, DOCUMENTATION_PATTERNS):
            documentation.append(path)
            continue
        matched = False
        for rule in IMPACT_RULES:
            if not _matches(path,rule.path_patterns):
                continue
            matched = True
            suites.update(rule.suites)
            visuals.update(rule.visual)
            full_at_checkpoint = full_at_checkpoint or rule.full_at_checkpoint
        if not matched:
            unknown.append(path)
    test_modules = set(direct_tests)
    for suite in suites:
        test_modules.update(suite_test_modules(suite, root))
    if len(visuals) > 1:
        visuals.discard("smoke")
    return {
        "schema_version": 1,
        "paths": list(requested),
        "suites": sorted(suites),
        "direct_tests": sorted(direct_tests),
        "test_modules": sorted(test_modules),
        "test_module_count": len(test_modules),
        "visual_scopes": sorted(visuals),
        "documentation_paths": sorted(documentation),
        "unknown_paths": sorted(unknown),
        "full_at_checkpoint": bool(full_at_checkpoint or unknown),
    }


def pytest_command(test_modules: Iterable[str]) -> list[str]:
    modules = list(test_modules)
    command = [str(Path(sys.executable)), "-m", "pytest", *modules, "-q"]
    if modules and all(path.startswith("suite_core/tests/") for path in modules):
        command.extend(("--confcutdir=suite_core", "-p", "no:cacheprovider"))
    return command


def _display_command(command: Iterable[str]) -> str:
    return subprocess.list2cmdline(list(command))


def _print_plan(plan: dict[str, object]) -> None:
    print("Proportional test plan")
    print("Changed paths:", len(plan["paths"]))
    print("Suites:", ", ".join(plan["suites"]) or "none")
    print("Pytest modules:", plan["test_module_count"])
    if plan["test_modules"]:
        print("Command:")
        print(_display_command(pytest_command(plan["test_modules"])))
    if plan["visual_scopes"]:
        print("Visual checks:")
        for scope in plan["visual_scopes"]:
            command = [str(Path(sys.executable)), "qt_visual_test.py"]
            if scope != "smoke":
                command.append(scope)
            print(_display_command(command))
    if plan["documentation_paths"]:
        print(
            "Documentation-only paths use their named deterministic validators "
            "plus git diff --check; they add no pytest modules by themselves."
        )
    if plan["unknown_paths"]:
        print("Unmapped executable or data paths:")
        for path in plan["unknown_paths"]:
            print(" -", path)
        print("Add an impact rule or use the full suite before relying on this plan.")
    if plan["full_at_checkpoint"]:
        print(
            "Checkpoint gate: retain the complete pytest suite before a release or "
            "approved checkpoint; it is not part of this immediate focused plan."
        )


def _run_modules(modules: Iterable[str]) -> int:
    command = pytest_command(modules)
    print(_display_command(command), flush=True)
    return subprocess.run(command, cwd=ROOT).returncode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or run CollectiGen's named proportional pytest suites."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List named domain suites and module counts.")
    subparsers.add_parser(
        "validate",
        help="Verify every canonical test module belongs to a domain suite.",
    )
    plan = subparsers.add_parser(
        "plan",
        help="Plan focused verification for paths; defaults to tracked changes.",
    )
    plan.add_argument("paths", nargs="*")
    plan.add_argument("--json", action="store_true", dest="as_json")
    plan.add_argument(
        "--run",
        action="store_true",
        help="Run the planned pytest modules when every path is mapped.",
    )
    run = subparsers.add_parser(
        "run",
        help="Run named suites; use 'full' explicitly for the complete gate.",
    )
    run.add_argument("suites", nargs="+", choices=[*SUITES, "full"])
    checkpoint = subparsers.add_parser("checkpoint", help="Run and record affected checks; never approve a release.")
    checkpoint.add_argument("paths", nargs="*", help="Additional source/test paths, or the complete owned scope with --owned-only.")
    checkpoint.add_argument("--owned-only", action="store_true", help="Check only explicitly named task-owned paths; record omitted tracked source changes.")
    checkpoint.add_argument("--base", default="HEAD", help="Compare against this commit (default HEAD).")
    checkpoint.add_argument("--fresh", action="store_true", help="Run checks even when unchanged passing evidence exists.")
    checkpoint.add_argument("--knowledge-only", action="store_true", help="Prepare change-focused knowledge review before running tests.")
    checkpoint.add_argument("--knowledge-reviews", help="Supplemental reviewed-change receipt JSON within the source boundary.")
    checkpoint.add_argument("--timeout", type=float, default=300, help="Per-process timeout in seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv)
    if options.command == "checkpoint":
        from tools.test_checkpoint import run
        if options.timeout <= 0:
            raise ValueError("Timeout must be positive.")
        if options.owned_only and not options.paths:
            raise ValueError("--owned-only requires explicit task-owned paths.")
        code, report = run(options.paths, base=options.base, timeout=options.timeout, fresh=options.fresh, knowledge_only=options.knowledge_only, knowledge_reviews=options.knowledge_reviews, owned_only=options.owned_only)
        print(f"Checkpoint result: {json.loads(report.read_text(encoding='utf-8'))['status']}")
        print(f"Checkpoint evidence: {report}")
        print("Affected checks only; release qualification is separate. New source/test files must be named explicitly.")
        return code
    if options.command == "list":
        for name, suite in SUITES.items():
            print(
                f"{name:20} {len(suite_test_modules(name)):3} modules  "
                f"{suite.description}"
            )
        print(
            f"{'full':20} {len(canonical_test_modules()):3} modules  "
            "Complete release/checkpoint gate."
        )
        return 0
    if options.command == "validate":
        errors = validate_suite_manifest()
        if errors:
            print("Test-suite manifest validation failed:")
            for error in errors:
                print(" -", error)
            return 1
        print(
            f"Test-suite manifest passed: {len(canonical_test_modules())} "
            f"canonical modules across {len(SUITES)} named suites."
        )
        return 0
    if options.command == "plan":
        paths = options.paths or changed_tracked_paths()
        plan = plan_for_paths(paths)
        if options.as_json:
            print(json.dumps(plan, indent=2))
        else:
            _print_plan(plan)
        if options.run:
            if plan["unknown_paths"]:
                print("Refusing focused execution while changed paths are unmapped.")
                return 2
            if not plan["test_modules"]:
                print("No pytest modules are required by this plan.")
                return 0
            return _run_modules(plan["test_modules"])
        return 0
    modules = set()
    for name in options.suites:
        modules.update(suite_test_modules(name))
    return _run_modules(sorted(modules))


if __name__ == "__main__":
    raise SystemExit(main())
