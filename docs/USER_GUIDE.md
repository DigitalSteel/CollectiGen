# Media Organizer a.0.4.5 quick guide

## Choose the scope

Start with copied images and a separate profile as described in the [README](../README.md). In Setup, choose the incoming folder and library destination, review their sharing boundaries, then save the storage settings. A profile holds application state; images stay in your chosen folders.

## Review and save an image

Open Ingest and select an image using the visible controls. Use the existing image editor to review Tags and Descriptions, rating and required image facts. Save only the changes you intend. Inspect the saved result and its JSON/TXT sidecars; optional Metadata review has its own authority and should not be confused with saving Image Knowledge.

Required knowledge and a valid destination determine whether an image is ready to move. Advisory guidance does not authorize guessing facts. Keep unknown facts unknown and correct an image manually when necessary. An alpha completeness indicator is not a guarantee that every fact or output is suitable for every later use.

## Move and inspect

Choose a named library destination, review the sharing boundary and preview the proposed move. Cancel if the destination or readiness is wrong. Move only copied test images during evaluation. After completion, verify the image and sidecars at the destination. Open in Dataset to inspect the same saved record; allow the gallery to finish loading before judging its count or thumbnail.

## Close and recover conservatively

Use ordinary Close. Pending work may retain the window while it cancels or drains. Keep a held operation and its evidence intact; do not delete state or caches or force-close it to obtain a success message. If startup or storage fails, stop and report the diagnostic. Keep an independent backup of the profile, images and sidecars before trying another build.

This candidate supports the manual image workflow described here. Manager launch, automatic updates, Video/audio, models, training and generation are not qualified by this guide. See [limits and startup](../README.md), [testing](../TESTING.md), [privacy](../PRIVACY.md) and [support](../SUPPORT.md).

## First-run configuration

Guided Setup starts with Library locations, then optional AI setup, Preferences and defaults, and Review and finish. Save and Continue Later keeps the current step; Skip for Now preserves your choices without completing setup. Model-use history is optional and takes effect only at Finish. Finish does not open or scan a library or start media processing. Shared Manager configuration requires a verified connection; this standalone candidate uses only its own saved profile settings.
