MODULE_ID = "collectigen.suite-manager"
VERSION = MODULE_VERSION = "0.3.2-alpha.1"
from core.component_release_identity import ComponentReleaseIdentity
NUMERIC_VERSION = "0.3.2"
MATURITY = "alpha"
DISPLAY_VERSION = ComponentReleaseIdentity(NUMERIC_VERSION, MATURITY).display_version
DATE = "2026-10-11"
MANAGER_INTERFACE_VERSION = 1
