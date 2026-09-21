# Hermes Patch Series

Patches are applied lexically by filename during vendor sync. Each patch must use a zero-padded
sequence prefix, document the upstream commit or issue it adapts, state its reason, and state the
condition under which it can be removed. The generated vendor manifest records each patch name and
SHA-256 digest. This initial snapshot has no local patches.
