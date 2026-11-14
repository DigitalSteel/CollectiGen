# Organizer transaction image-read reuse

SUITE-10B3 avoids repeated executable reads during the existing authenticated
Organizer component transaction. WindowsControlChannel defaults to its previous
full native witnesses. Only a successful Organizer stage begin enables the
cache, on Root after original stage admission and on Manager after its verified
reply. Reader, successor, maintenance and unrelated channels do not opt in.

During that stage, each original process image is hashed once under a retained
read handle denying writes. Every use still checks actual process birth, image
path, named-file identity, original API, retained process handle and channel
epoch. The image handle is not serialized or accepted from a peer.

Release closes and clears BOTH image streams before Root removes its durable
stage and permits maintenance. Manager drops its cache after validating release
or on failed exchange; an uncertain request remains held. Cache disposal closes
no original pipe, process, Job or owner lease and grants no update/recovery
permission. A cache-close failure retains the original stage. Normal channel
Close also disposes any remaining cache. Default full witnesses resume afterward.

The previous channel-lifetime candidate was not published: source review found
its root-side file leases could survive into stock apply/repair. Regression
coverage requires actual Windows os.replace to work after scope release while
the original channel remains open; rename-away alone is insufficient proof.

Stock API: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
Tests: tests/test_manager_channel_image_reuse.py and original component-stage
ownership tests. Exact signed delivery and native evidence are recorded separately.
