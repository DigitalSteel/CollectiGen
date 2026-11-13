# Manager local appearance

Development source feature; not included in the published Manager 0.7.4 package.

## Choose and remember your appearance

Open **Settings**, choose a color theme, color-blind-friendly status colors or
contextual help tooltips, then choose **Save appearance**. A successful save
applies the choices immediately and restores them at the next Manager launch.
Other modules keep their own preferences. **Reload saved choices** discards
unsaved edits and applies the latest saved values.

If saving fails, Manager keeps its current appearance and restores the controls
to the last loaded choices. Resolve settings access, then reload before retrying.
Invalid settings are retained for recovery rather than replaced with defaults.
Controls owned by shared settings remain disabled here; this panel does not
activate shared-settings synchronization or override a managed setting.

## Source ownership and checks

`core/manager_appearance_settings.py::ManagerAppearanceSettings` reads only the
explicit Manager AppSettings JSON document. It patches selected display keys
through the existing settings writer, stale check, history and rollback.
The admitted INI launcher/restart document is unchanged.
`suite_core/manager/appearance_ui.py::AppearancePanel` presents one save authority;
`restore_appearance` is shared by normal startup and the synthetic reopen check.
`suite_core/manager/window.py::create_window` places the panel in Settings;
`suite_core/manager/__main__.py::main` binds the existing admitted sidecar.

Focused checks live in `tests/test_manager_appearance_settings.py` and
`tests/test_manager_appearance_ui.py`; existing Manager workflow-navigation tests
cover adjacent navigation and retained Generate/Training routes. Synthetic
source evidence does not establish signed-package delivery or native rollback.
Runtime/source inventories include both new production files. Delivery belongs
to `collectigen.suite-manager` under SUITE-10B1 / SUITE-10B, using the existing
component builder and updater. No component version is invented for this source
checkpoint. Full suite parity and the retained failed-start recovery work remain
open.
