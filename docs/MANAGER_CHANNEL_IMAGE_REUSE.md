# Original control-channel image reuse

SUITE-10B3 fixes repeated full executable reads during component member checkpoints.
The existing WindowsControlChannel now retains a read handle for each of its two
original process images. It computes the digest once under that handle, which
allows read and rename/delete sharing but denies concurrent writes. Each use
still queries the retained process handle for its PID, birth and image path,
rejects path/reparse/file identity changes, and requires the original API,
process handle and channel epoch. No serialized cache or peer-supplied digest
creates this authority. Normal channel Close closes both retained image streams
before its original pipe and inherited peer handles.

This does not skip package signatures, inventory checks, transaction scopes,
cancellation, owner leases or recovery. Named-file replacement refuses rather
than reusing a digest from a different file. The existing independent full
_native_witness remains unchanged for other native admission paths.

Stock API evidence: [CreateFileW sharing modes](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).
Regression coverage: tests/test_manager_channel_image_reuse.py; component-stage
and native admission suites remain direct consumers. Final package timing and
signed delivery evidence are retained separately; source tests are not delivery.
