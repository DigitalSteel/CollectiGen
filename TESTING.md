# Focused source checks

Use Python 3.13 and the admitted Pillow/PySide image-runtime dependencies. From
this source root run pytest on test_organizer_runtime_guidance.py,
test_organizer_runtime_exiftool.py, test_organizer_package_probe.py,
test_organizer_startup_profile.py and test_setup_dependency_factory.py under tests/.
These are synthetic/component checks. Actual host native observations were made
on the separately pinned package with matching application code; they are not a
full user-journey or hardware/model test. No dependency downloads are required by
the checks. Native probe execution remains explicit and requires generated paths.
