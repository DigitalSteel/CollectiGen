SOFTWARE_VERSION = "0.1.0-alpha.8"
from core.component_release_identity import ComponentReleaseIdentity
NUMERIC_VERSION = "0.1.8"
MATURITY = "alpha"
DISPLAY_VERSION = ComponentReleaseIdentity(NUMERIC_VERSION, MATURITY).display_version
# Maximum reader support; encode chooses the minimum lossless wire version.
SNAPSHOT_INTERFACE_VERSION = 2
LEGACY_SNAPSHOT_INTERFACE_VERSION = 1
DIAGNOSTIC_SNAPSHOT_INTERFACE_VERSION = 2
RELEASE_DATE = "2026-10-08"
RELEASE_TIMEZONE = "America/New_York"

