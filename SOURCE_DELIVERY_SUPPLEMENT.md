# Manager release-label source candidate

This source-only BUG-313 patch adds presentation labels for the released Reader
0.3.1-alpha.1 and Organizer 0.4.23-alpha.1 to the actual Manager consumers.
Manager 0.5.1 and prior mappings, embedded identities, trust and update behavior
are unchanged. Focused checks exercise pure aliases and generated Manager cards
and inventory in two themes with synthetic inputs. They do not qualify normal
native startup, installation, saved state, recovery or a packaged update.

No version, binary, signed envelope, release or self-update activation is made.
Original Manager source/producer/envelope/actor/recovery gates remain open.
