# Offline release inputs

`requirements.lock` and `offline/wheels/` form the Python 3.12 offline dependency boundary.
Release automation must additionally produce an SBOM, third-party notices, source commit,
wheel hashes and image digest from one Git commit. Runtime downloads are not permitted.

