# Releasing plsqlwks

## Prepare the release

1. Start from a clean checkout of the default branch and activate a disposable
   virtual environment. Confirm the checkout has no generated packaging artifacts:

   ```bash
   python3 tools/dev.py hygiene
   ```

   The hygiene command must pass before building; it rejects generated package metadata,
   build directories, distributions, and egg-info directories in the checkout. The
   complete verification command installs exact development versions from
   `constraints/ci.txt` into the active environment.

2. Change `plsqlwks.__version__` in `plsqlwks/__init__.py`. Move the relevant notes from
   `Unreleased` in `CHANGELOG.md` into a dated release section, then leave an empty
   `Unreleased` section for future work. The first dated heading must be
   `## X.Y.Z YYYYMMDD`, and its body becomes the GitLab Release notes. If
   `QUICKSTART.md` changed, refresh the
   packaged offline copy with:

   ```bash
   pandoc QUICKSTART.md --standalone --toc --metadata title='PLSQLWKS quick start' -o QUICKSTART.html
   ```
3. Rebuild the three separate local SQLite indexes after source, test, or
   documentation changes:

   ```bash
   python3 tools/build_vdb.py
   ```

## Verify

Run repository hygiene, constrained installation, quality checks, non-Oracle and
plugin coverage, and build smoke verification with the same command surface used
by CI:

```bash
python3 tools/dev.py ci
```

The command removes only the packaging artifacts it created, so the later release
build starts clean; it retains machine-readable reports under `coverage-reports/`.

When Oracle credentials are available, also run:

```bash
PLSQLWKS_TEST_ORACLE_TARGET=19c python3 tools/dev.py test oracle
PLSQLWKS_TEST_ORACLE_TARGET=26ai python3 tools/dev.py test oracle
```

Each local command reads `ORACLE_USER`, `ORACLE_DSN`, and `ORACLE_PASSWORD_FILE`; point
those generic variables at the matching server before running it. The target selector
also verifies the server version so nominal 19c and 26ai runs cannot accidentally use
the same database.

Before a release, also run the complete developer, DML-only, and read-only matrix for
both DSN forms. The canonical [Oracle compatibility matrix](COMPATIBILITY.md) defines
the exact pre-provisioning SQL, generic local variables, protected CI variables and
secrets, safety checks, and command. An incomplete matrix or failed endpoint guard is an
infrastructure failure rather than a skipped compatibility check.

## Build and smoke-test

Build the source archive and wheel, inspect both distributions, and install the wheel
with and without the optional XLSX extra in isolated environments:

```bash
python3 tools/dev.py build --smoke
```

The packaging privacy test builds a source archive from a checkout seeded with synthetic
private files, builds the wheel from that archive as PyPI does, and requires exact file
allowlists. It permits only the explicitly public `Author: unu2000` identity and Ko-fi
project URL; all author email, maintainer metadata, email and home/build-path leaks,
private-key/token patterns, local workspaces, credentials, agent/vector metadata, and
symlinks remain rejected. Do not publish if this test fails. Inspect the source archive
and wheel to confirm that both contain `license.txt`, that their metadata links to the
GitLab repository and `https://ko-fi.com/unu2000`, and that neither metadata file refers
to the old GitHub preview URL.

Confirm that the self-hosted GitLab pipeline and GitHub Actions workflow are green. The
19c and 26ai Oracle matrix jobs must pass on both platforms before tagging. Verify each
Oracle job selected the expected private runner, kept test output in its log, uploaded
no artifacts or caches, and consumed no hosted-runner minutes. The non-Oracle Python
GitLab's 3.10 and 3.14 jobs retain machine-readable test and coverage reports
for seven days; GitHub runs both test suites directly without retaining reports.
A pipeline with a warning, allowed failure, failed cleanup, pending gate, or skipped
required job is not release-ready. A green pre-tag pipeline is preparation evidence;
only the protected release-tag pipeline can build or publish official artifacts.

Oracle 19c and 26ai remain described as mandatory release-gate targets until
each CI platform has recorded ten consecutive qualifying protected pipelines
spanning at least 30 days. Each qualifying pipeline must select and pass both
Oracle jobs after its required upstream gates: repository hygiene, both Python
test instances, and build smoke on GitHub, plus quality on GitLab. No Oracle
skip, allowed failure, cancellation, or pending gate is permitted. A failed or
incomplete qualifying pipeline restarts the count. Ad-hoc
developer runs and the reserved Thick, wallet/TCPS, and TNS-alias experiments
do not contribute evidence or broaden the public compatibility claim. Restore
the “continuously tested” wording only in a later reviewed documentation change
after the complete evidence window exists in both CI histories.

## Tag

Create the release tag from the verified commit and push it:

```bash
git tag -a plsqlwks-X.Y.Z -m 'plsqlwks X.Y.Z'
git push origin plsqlwks-X.Y.Z
```

Do not create another tag spelling. The release pipeline rejects a tag unless its exact
version matches `plsqlwks.__version__`, the `pyproject.toml` distribution name, the
GitLab project name, and the latest dated changelog heading. It also rejects non-empty
`Unreleased` notes, an invalid date, empty release notes, or an unprotected tag.

## Automated GitLab Release

The protected tag starts the separate release path in `.gitlab/ci/release.yml`. Its
identity job runs before any build. Quality, Python 3.10 and 3.14 tests and coverage,
build smoke, and both Oracle matrix jobs are blocking dependencies. There are no
allowed-failure release gates, and Oracle cleanup failure is configured to fail the
job.

After every gate passes, the official build job uses a fresh clone with no CI cache. It
installs only the constrained build and metadata tools, rejects generated checkout
artifacts, builds the wheel and source archive, checks archive identity and required
metadata, runs `twine check --strict`, installs the built wheel in a new virtual
environment with and without the XLSX extra, and writes `dist/SHA256SUMS`.

This project is not source-only. The publish job uploads the wheel, source archive, and
`SHA256SUMS` to the GitLab Generic Package Registry under package `plsqlwks` and the tag
version, then links those durable package files from an automatically created GitLab
Release. The release description is the non-empty latest changelog entry. The official
build artifact is retained without expiry, and supported GitLab runners add SLSA
provenance metadata; the release links back to that build evidence. Distribution files
must not be uploaded manually or rebuilt after tagging.

The publish command uses `--no-update`. If a retry finds a partial registry upload,
confirm that no Release was created, delete only the incomplete Generic Package
Registry version for that tag, and retry the failed protected pipeline after correcting
the cause. Never replace files belonging to an already published Release.

## Maintainer settings checklist

These controls live in GitLab and cannot be established by a commit. A Maintainer must
verify them before enabling automatic releases:

- Protect the wildcard tag `plsqlwks-*`. Permit tag creation only for the intended
  Maintainer or release role, and do not allow Developers to create matching tags.
- Keep the `plsqlwks` and `docker` runner tags assigned to locked, project-scoped,
  disposable Docker runners. Disable untagged jobs and shared-runner fallback. Grant the
  runner only the network and Oracle secret-file access needed by its job, and use a
  GitLab Runner version that emits artifact provenance for
  `RUNNER_GENERATE_ARTIFACTS_METADATA=true`.
- Ensure the runner can pull the pinned GitLab CLI image and that Generic Package
  Registry and Releases are enabled for the project. Add a Generic package protection
  rule for `plsqlwks` with Maintainer-level push and delete access. Ask the group Owner
  to turn off **Allow duplicates** for Generic packages, with no exception matching
  `plsqlwks`, so an existing version cannot be silently overwritten.
- Prefer the built-in `CI_JOB_TOKEN`. Confirm the instance permits that token to create
  project Releases and upload project Generic Packages; the job enables GitLab CLI CI
  auto-login and does not print the token.
- If instance policy does not grant those `CI_JOB_TOKEN` permissions, create a dedicated
  project access token with Maintainer role and `api` scope. Store it only as the
  protected, masked, hidden `RELEASE_ACCESS_TOKEN` variable, give it a short expiry,
  record an owner and rotation date, and never expose it to unprotected refs or fork
  pipelines. Remove this fallback after job-token permissions become sufficient.
- Before the first release, run a protected dry run through the identity and build
  stages, inspect the runner-generated provenance, and verify that a failed or warning
  gate leaves both the Release and registry version absent.

After publication, verify the Release title, tag, commit SHA, non-empty notes, all three
package links, both SHA-256 checks, and the build-evidence link before announcing it.
