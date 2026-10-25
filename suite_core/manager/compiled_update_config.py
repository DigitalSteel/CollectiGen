"""Build-bound public Manager update profile; no inferred runtime trust.

The canonical unconfigured source deliberately holds. Reviewed build inputs may
replace this one staged constant with a CompiledManagerUpdateProfile. Root and
child share public trust/channel/transport pins; only the stable root embeds the
retained prior release. Keeping that descriptor out of the Manager binary avoids
a recursive self-hash when constructing the signed initial Manager package.
Environment and pipe payloads cannot enroll either profile.
"""
PROFILE = None
