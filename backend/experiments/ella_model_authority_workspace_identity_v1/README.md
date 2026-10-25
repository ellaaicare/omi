# Workspace identity during model-authority reads

The `.py.source` files preserve exact deployed/source-reviewed bytes outside the repository Python formatter; the portable test loads them using the standard source loader.

Source candidate: the authority read previously compared workspace directory timestamps and size between two samples. Ordinary file creation or atomic replacement changes directory metadata and can reject an otherwise unchanged account/model authority. This candidate compares the workspace device, inode, mode and owner, matching the pre-existing registry-parent identity rule. It retains full checks for the profile/root directories, authority file bytes, no-follow traversal, source/version, captured content and attempt/account/consent guards.

Nine tests use the actual runtime implementation with disposable fictional files. The baseline reproduces timestamp-only rejection. The candidate accepts timestamp/content-write metadata changes while rejecting replacement, unsafe/safe permission changes, symlinks, and changes to each authority file. Portable tests differ from the local executed suite only in baseline and temporary fixture paths. The portable fixture resolves the temporary root before no-follow traversal, because macOS may return a `/var` symlink path. Its first uncanonicalized run rejected that path; the corrected portable nine cases passed without changing product source.

Run with Python and the existing PyYAML dependency:

```sh
python3 -B -m unittest discover -s backend/experiments/ella_model_authority_workspace_identity_v1/tests -v
```

A controlled saved-record attempt ended before generation with `semantic_configured_model_authority_rejected`. The latest host fixed-metadata observation reports workspace mtime/ctime-only change. That host log has no request ID or timestamp; it supports this diagnosis without proving an exact historical request join or identifying the writer. This is a demonstrated source defect, not proof of an OAuth/model error. Quarantine, credentials and saved data are not modified by these fixtures. The one-file host-only rollout completed at 2026-10-25 13:42:54 UTC with health passing, unchanged public model policy, zero database/model/config/journal writes, and installed service-interpreter import checks reporting zero forbidden I/O. The original attempt remains quarantined; no inference or live memory success is implied by the rollout.
