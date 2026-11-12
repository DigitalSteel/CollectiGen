# Organizer new Training plan entry

BUG-338: the shared ribbon deliberately removes legacy goal tabs and delegates Training to its workspace. Organizer instead resumes Dataset-owned saved drafts. That left its direct current-image training planner without a visible launcher.

Add the existing training_plan_button through Organizer workspace_extra_commands in Dataset Plans. Retain its live readiness guard, selected/matching scope chooser, shared model guidance, read-only review and saved store. Existing Saved Dataset Drafts remains the resume route. The all-in-one command policy and execution restrictions are unchanged.

Acceptance: ordinary packaged Dataset entry reaches Plan Training Dataset, reviewed guidance, named Save Draft, then Saved Dataset Drafts and exact reopened scope/configuration. New plans do not execute models/trainers or modify source images.
