# Oracle compatibility

This document is the canonical compatibility and integration-test contract for
plsqlwks. Oracle Database 19c and Oracle AI Database 26ai are mandatory
release-gate targets. A target is described as continuously tested only after
both protected CI systems record ten consecutive qualifying green runs spanning
at least 30 days; until then, the narrower release-gate-target wording applies.

## Compatibility matrix

Both releases use python-oracledb Thin mode with password-file authentication.
`Easy Connect` means a `host:port/service` DSN; `full descriptor` means a
complete `(DESCRIPTION=...)` connect descriptor for the same service.

| Profile | Easy Connect coverage | Full-descriptor coverage |
| --- | --- | --- |
| Developer | Safety preflight and the complete live Oracle suite | Connection, identity, version, endpoint-fingerprint, and driver-mode smoke test |
| DML-only | Qualified SELECT/INSERT/UPDATE/DELETE against the developer-owned fixture, followed by cleanup and rollback; DDL must be denied | Connection, identity, version, endpoint-fingerprint, and driver-mode smoke test |
| Read-only | Qualified SELECT succeeds; DML and DDL must be denied by Oracle | Connection, identity, version, endpoint-fingerprint, and driver-mode smoke test |

The complete six-cell matrix runs for each release-gate target:

| Target | Accepted server version |
| --- | --- |
| Oracle Database 19c | Reported numeric major version is `19` |
| Oracle AI Database 26ai | Reported numeric release is `23.26` or later in the `23.x` family |

The developer suite creates, discovers, loads, and removes representative
tables, views, procedures, functions, packages, triggers, sequences, indexes,
and private synonyms, covering every object type advertised by the schema
browser. It also covers two contrasting session NLS profiles, quoted Unicode
identifiers, DATE and timestamp types, exact-boundary and approximately 1 MiB
CLOB/BLOB values, compiler warnings, cancellation of an active long-running
query, and bounded connection-failure recovery. These mutation-heavy scenarios
run in the developer/Easy Connect cell; the other five cells retain their
connection, identity, fingerprint, and privilege-boundary purposes.

A qualifying protected pipeline must select both Oracle target jobs after its
required upstream gates. GitHub requires repository hygiene, Python 3.10/3.14
tests, and build smoke; GitLab additionally requires its quality job. Every
selected Oracle test must pass without a skip, allowed failure, cancellation,
or pending gate. A failed or incomplete qualifying pipeline restarts the
consecutive-run count. Ad-hoc local runs and experimental connection jobs never
count toward the public claim.

Other Oracle releases may be used for ad-hoc development, but are not part of
the compatibility claim. Thick mode, TNS aliases, wallets, TCPS-specific
configuration, external authentication, proxy authentication, and driver or
database failover configurations are not release-gating combinations.

### Time-zone timestamp behavior

Oracle `DATE` has no time-zone component. Plain `DATE` and `TIMESTAMP(6)` values
are materialized as NLS-independent Python datetimes and ISO display strings.
The current pinned python-oracledb Thin driver does not preserve the offset or
zone of `TIMESTAMP WITH TIME ZONE` and `TIMESTAMP WITH LOCAL TIME ZONE` values,
and it truncates precision beyond Python microseconds. Returning the resulting
naive datetime would falsely imply a lossless value.

For that reason, non-null zoned timestamp cells are read-only and use these
fail-safe display and export values:

- `<TIMESTAMP WITH TIME ZONE: lossless Thin-mode fetch unavailable; use TO_CHAR>`
- `<TIMESTAMP WITH LOCAL TIME ZONE: lossless Thin-mode fetch unavailable; use TO_CHAR>`

The result also carries a warning. Select a lossless companion string explicitly
when exact data is required, for example:

```sql
to_char(zoned_timestamp, 'YYYY-MM-DD"T"HH24:MI:SS.FF9 TZH:TZM')
```

For a local-time-zone column, first cast it at the required precision to
`TIMESTAMP(9) WITH TIME ZONE`; its rendered value follows `SESSIONTIMEZONE`.
Named-zone timestamp fetches remain an explicit Thin-mode limitation.

### Experimental connection matrices

No executable experimental connection job or runtime switch is part of the
current release gate. The following names reserve separate future contracts so
their results cannot be mistaken for coverage of the mandatory Thin matrix:

| Reserved matrix | Required isolated infrastructure |
| --- | --- |
| `oracle-experimental-thick` | A separate process and protected runner image with reviewed Oracle Instant Client libraries |
| `oracle-experimental-wallet-tcps` | A dedicated TCPS endpoint and disposable protected wallet/configuration files |
| `oracle-experimental-tns-alias` | A disposable `tnsnames.ora`/`TNS_ADMIN` configuration and a dedicated alias |

Each future experiment must retain the identity, version, endpoint-fingerprint,
privilege, guard-token, and database-lock checks used by the base matrix. It
starts as a separate non-gating job with separate secrets and no artifacts or
caches. It can be added to the compatibility claim only after becoming
compulsory on protected pipelines and independently meeting the same ten-run,
30-day evidence rule.

The schema browser and metadata completion deliberately enumerate the current
login schema. Qualified cross-schema SQL is covered by the DML-only and
read-only profiles, but it does not add cross-schema objects to the browser.
ROWID-backed grid editing and insertion also remain limited to one base table
in the current schema. Exact quoted and Unicode table/column names are covered;
qualified cross-schema results are viewable but not grid-editable.

## Fail-closed safety gate

Matrix endpoints must be isolated, disposable databases containing no
production data. Before any selected Oracle test executes, the session-level
preflight performs all of these checks:

- every required value is present and every password/token path is a nonempty
  regular file;
- the three usernames are distinct conventional unquoted identifiers;
- every profile connects through both DSN forms as its expected session user
  and current schema, without proxy authentication, and each account is local
  and not Oracle-maintained, inherited, implicit, or shard-wide;
- server version, `DB_UNIQUE_NAME`, container name, and service name match the
  protected expected values, and the database role is `PRIMARY`;
- python-oracledb is using Thin mode with password authentication, and the
  guard token matches the protected digest in the developer schema;
- effective/direct system privileges, direct/effective object grants, column
  grants, outgoing grants, and empty roles exactly match the contracts below;
  restricted schemas own no objects, every grant option is `NO`, and DBA,
  user-administration, `ANY`, unlimited-tablespace, and public mutation paths
  are rejected;
- on 26ai, both effective and directly received schema privileges are empty.
- the developer's effective `CONNECT_TIME` and `IDLE_TIME` profile limits are
  both `UNLIMITED`, so they cannot terminate and roll back the guard session.

Any mismatch is an infrastructure failure, not a skipped test. Preflight errors
identify the failed setting but do not print usernames, DSNs, password paths,
passwords, or the guard token. The token is bound into this comparison and is
never stored or logged as plaintext:

```sql
lower(rawtohex(standard_hash(:token, 'SHA256')))
```

Only after every non-mutating check succeeds, a dedicated developer connection
acquires the verified `PLSQLWKS_ORACLE_MATRIX` guard row with
`SELECT ... FOR UPDATE NOWAIT` (the zero-wait equivalent of `WAIT 0`) and holds
that row lock for the entire pytest session. If another
GitHub, GitLab, or local run already holds it, the newcomer fails before test
mutation instead of waiting or overlapping. Teardown rolls back and closes the
dedicated connection; Oracle also releases the lock automatically if the test
process or connection dies. This database-level mutex is the cross-platform
serialization boundary.
Before and after every selected Oracle test, the harness performs another
token-bound `SELECT ... FOR UPDATE NOWAIT` on the existing lock connection. It
first requires the driver's transaction-in-progress flag and never reconnects
that guard session. A lost connection, rolled-back lock, or competing holder
therefore fails closed before the next test can mutate.

DML probes use a new 32-character UUID hex key, delete their own probe row, and
roll back. DDL-denial probes run only after rollback because Oracle DDL can
implicitly commit pending work. If a denial probe unexpectedly creates an
object, the test removes that object in `finally` and fails. Read-only denial
probes disable the plsqlwks client guard so the Oracle privilege boundary is
what rejects DML and DDL.

CI contains application credentials only. It must never contain or use a DBA,
`SYS`, `SYSTEM`, user-management, grant-management, or schema-bootstrap
credential. Account creation and grants are a separate, deliberate DBA action.
The live harness proves that its ordinary sessions are not DBA sessions and
rejects every unexpected normal-session privilege. It intentionally cannot
query the password file and does not make repeated administrative-login
attempts, which could trigger authentication controls. Before storing any test
credential, a DBA must therefore verify that none of the three accounts has a
password-file administrative authorization:

```sql
select username, sysdba, sysoper, sysasm, sysbackup, sysdg, syskm
from v$pwfile_users
where username in (upper('<DEV_USER>'),
                   upper('<DML_USER>'),
                   upper('<READ_ONLY_USER>'));
```

This query must return no rows. The private runner host must not be a database
host and its service accounts must not belong to Oracle operating-system
administration groups.

## Pre-provisioning contract

Provision three different conventional unquoted users on each disposable
target: `<DEV_USER>`, `<DML_USER>`, and `<READ_ONLY_USER>`. Use the site's secret
management process to create password-authenticated accounts. Give only the
developer exactly one positive finite quota, no greater than 64 MiB, on a
dedicated test tablespace; the other accounts must have no tablespace quota.

The effective rows from `SESSION_PRIVS` must be exactly:

- developer: `CREATE SESSION`, `ALTER SESSION`, `CREATE TABLE`, `CREATE VIEW`,
  `CREATE PROCEDURE`, `CREATE TRIGGER`, `CREATE SEQUENCE`, and `CREATE SYNONYM`;
- DML-only and read-only: `CREATE SESSION`.

`SESSION_ROLES` must be empty for all three profiles, and all required
privileges must be direct grants. In particular, do not grant `CONNECT`,
`RESOURCE`, `DBA`, `UNLIMITED TABLESPACE`, catalog roles, or a privilege
containing `ANY`. This avoids trusting object privileges inherited through a
role that `SESSION_PRIVS` cannot expose.

The DML-only and read-only schemas must have zero rows in `USER_OBJECTS` and no
column-level grants. On 26ai, `SESSION_SCHEMA_PRIVS` and `USER_SCHEMA_PRIVS`
must also be empty for every profile. The developer's outgoing object grants
must be exactly the five fixture grants shown below; it must have no outgoing
column grants, no grant on the guard table, and no grant to `PUBLIC`. The
preflight also rejects any effective public table/column mutation privilege
visible to a profile.

Assign the developer a dedicated profile whose `CONNECT_TIME` and `IDLE_TIME`
are both `UNLIMITED`; the preflight verifies the effective values through
`USER_RESOURCE_LIMITS`. A DBA must also ensure that no Database Resource Manager
`MAX_IDLE_BLOCKER_TIME` or priority-transaction policy targets this isolated
test account, because such a policy could roll back the guard-row transaction.

After creating the accounts, a DBA applies the following template. Angle
brackets are deliberate non-executable placeholders and must be replaced with
the chosen safe identifiers.

```sql
grant create session, alter session, create table, create view,
      create procedure, create trigger, create sequence, create synonym
  to <DEV_USER>;
alter user <DEV_USER> quota 64M on <TEST_TABLESPACE>;

grant create session to <DML_USER>;
grant create session to <READ_ONLY_USER>;

grant execute on sys.dbms_output to <DEV_USER>;
grant execute on sys.dbms_metadata to <DEV_USER>;
grant execute on sys.dbms_xplan to <DEV_USER>;
grant execute on sys.dbms_session to <DEV_USER>;
grant execute on sys.dbms_utility to <DEV_USER>;
grant execute on sys.dbms_lob to <DEV_USER>;

grant execute on sys.dbms_output to <DML_USER>;
grant execute on sys.dbms_output to <READ_ONLY_USER>;

grant select on sys.v_$sql to <DEV_USER>;
grant select on sys.v_$sql_plan to <DEV_USER>;
grant select on sys.v_$sql_plan_statistics_all to <DEV_USER>;
grant select on sys.v_$session to <DEV_USER>;
```

These are exact allowlists, not minimums. An additional direct system or object
grant, an admin/grant option, or any received role makes preflight fail.

Generate a high-entropy token outside Oracle, keep its plaintext only in a
protected local file or CI secret, and calculate its lowercase SHA-256 hex
digest.
Then connect as `<DEV_USER>` and create the two permanent fixtures. Replace
`<LOWERCASE_TOKEN_SHA256>` with the digest, never the plaintext token.

For example, this standard-library command creates a new token file
exclusively, sets mode `0600`, and prints only the digest to copy into the SQL:

```bash
python3 - /protected/guard-token <<'PY'
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
import sys

path = Path(sys.argv[1])
token = token_hex(32)
with path.open("x", encoding="ascii") as stream:
    stream.write(token)
path.chmod(0o600)
print(sha256(token.encode("ascii")).hexdigest())
PY
```

```sql
create table plsqlwks_compat_guard (
  guard_name varchar2(30 char) primary key,
  token_sha256 varchar2(64 char) not null,
  constraint plsqlwks_guard_token_sha_ck check (
    regexp_like(token_sha256, '^[0-9a-f]{64}$', 'c')
  )
);

insert into plsqlwks_compat_guard (guard_name, token_sha256)
values ('PLSQLWKS_ORACLE_MATRIX', '<LOWERCASE_TOKEN_SHA256>');

create table plsqlwks_compat_fixture (
  probe_id varchar2(32 char) primary key,
  probe_value varchar2(200 char) not null
);

insert into plsqlwks_compat_fixture (probe_id, probe_value)
values ('READ_ONLY_BASELINE', 'compatibility fixture');

grant select, insert, update, delete on plsqlwks_compat_fixture to <DML_USER>;
grant select on plsqlwks_compat_fixture to <READ_ONLY_USER>;
commit;
```

Do not grant either limited user access to `PLSQLWKS_COMPAT_GUARD`. Verify each
profile in a new session with `SESSION_PRIVS`, `SESSION_ROLES`,
`USER_SYS_PRIVS`, `USER_ROLE_PRIVS`, `USER_TAB_PRIVS_RECD`,
`USER_COL_PRIVS_RECD`, `USER_TAB_PRIVS_MADE`, `USER_COL_PRIVS_MADE`, and
`USER_OBJECTS`, `USER_TS_QUOTAS`, and `USER_RESOURCE_LIMITS`. On 26ai also inspect `SESSION_SCHEMA_PRIVS` and
`USER_SCHEMA_PRIVS`. Obtain the fingerprint values used by CI with:

```sql
select sys_context('USERENV', 'DB_UNIQUE_NAME') as db_unique_name,
       sys_context('USERENV', 'CON_NAME') as con_name,
       sys_context('USERENV', 'SERVICE_NAME') as service_name
from dual;
```

## Running the matrix locally

The existing developer-only live suite remains available through
`python3 tools/dev.py test oracle`. A complete matrix run uses the
`oracle-matrix` profile, chooses `19c` or `26ai`, and supplies all of the
following generic variables:

```bash
export ORACLE_USER='<developer user>'
export ORACLE_DSN='db.example.test:1521/service'
export ORACLE_PASSWORD_FILE='/protected/dev-password'
export PLSQLWKS_TEST_ORACLE_DESCRIPTOR_DSN='(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db.example.test)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME=service)))'
export PLSQLWKS_TEST_ORACLE_DML_USER='<dml user>'
export PLSQLWKS_TEST_ORACLE_DML_PASSWORD_FILE='/protected/dml-password'
export PLSQLWKS_TEST_ORACLE_READ_ONLY_USER='<read-only user>'
export PLSQLWKS_TEST_ORACLE_READ_ONLY_PASSWORD_FILE='/protected/read-only-password'
export PLSQLWKS_TEST_ORACLE_EXPECTED_DB_UNIQUE_NAME='<DB_UNIQUE_NAME>'
export PLSQLWKS_TEST_ORACLE_EXPECTED_CON_NAME='<container name>'
export PLSQLWKS_TEST_ORACLE_EXPECTED_SERVICE_NAME='<service name>'
export PLSQLWKS_TEST_ORACLE_GUARD_TOKEN_FILE='/protected/guard-token'

PLSQLWKS_TEST_ORACLE_TARGET=19c \
python3 tools/dev.py test oracle-matrix
```

Password and token files must be nonempty regular files; protect them with
mode `0600` on POSIX systems. The developer DSN must be Easy Connect and the
descriptor variable must be a full descriptor for the same endpoint. Omitting
the matrix flag preserves the existing ad-hoc developer-suite behavior, but it
does not acquire the guard-row mutex. Never point an unguarded ad-hoc run at a
shared release-gating endpoint; use a separate disposable database or enable
the complete matrix.

## Protected CI release gates

Both CI systems define the same required 19c and 26ai matrix, use only private
self-hosted runners, and have no schedule or nightly trigger. GitLab explicitly
rejects scheduled pipelines and fork merge-request pipelines. Missing
configuration fails the selected job. Test output stays in the job log; the CI
definitions do not upload artifacts or use vendor-hosted remote caches from the
live Oracle jobs. GitHub runs the non-Oracle and plugin suites directly on
Python 3.10 and 3.14 without retaining reports; GitLab retains JUnit, Cobertura
XML, and coverage JSON for seven days. Build and installed-wheel smoke
verification stay in one job so no distribution artifact transfer is needed.
The shared GitLab Oracle template explicitly sets both live-test opt-in flags,
declares blocking `needs` edges to repository hygiene, quality, both parallel
Python test instances, and build smoke, disables dependency artifact downloads,
and is non-interruptible. The GitHub jobs use the equivalent explicit flags,
depend on repository hygiene, both Python test instances, and build smoke, and
use non-cancelling concurrency groups.

The authoritative CI definitions are `.github/workflows/ci.yml` and
`.gitlab-ci.yml`; a duplicate root `ci.yml` is intentionally not maintained.
Both definitions call the versioned `tools/dev.py` commands for installation,
test profiles, hygiene, and build smoke checks; GitLab also calls the lint
command. Installs and nested wheel smoke environments use the exact
Linux/Python 3.10/3.14 dependency set in `constraints/ci.txt`; dependency updates
are deliberate reviewed changes rather than upgrades performed by each job.

Every GitLab job runs a standard-library preflight before installation or test
work. It verifies the requested Python version, `curses`, usable PTYs, the selected
Python image, a disposable Docker environment, both required runner tags, and
required checkout inputs. Oracle jobs additionally validate every mapped matrix
variable, the job-specific workspace prerequisites, and all four private secret
files. Failures name only the unmet condition or variable label: the preflight
never prints a username, DSN, secret-file path or contents, raw operating-system
error, or configuration representation.

Every live Oracle job creates a new mode-`0700` workspace with a job-unique,
validated path and exports it as `PLSQLWKS_WORKSPACE`. The always-run cleanup
deletes it only after verifying a job-created ownership marker; GitLab binds
that marker to the job token with an HMAC. A pre-existing path fails the job
and is not deleted. This prevents stale `config.ini` settings on a persistent
private runner from changing transaction or read-only behavior.

### GitLab variables and jobs

For each prefix `ORACLE_19C` and `ORACLE_26AI`, configure these protected CI/CD
variables:

- `<PREFIX>_USER`, `<PREFIX>_DSN`, and file-type
  `<PREFIX>_PASSWORD_FILE` for the developer profile;
- `<PREFIX>_DESCRIPTOR_DSN`;
- `<PREFIX>_DML_USER` and file-type `<PREFIX>_DML_PASSWORD_FILE`;
- `<PREFIX>_READ_ONLY_USER` and file-type
  `<PREFIX>_READ_ONLY_PASSWORD_FILE`;
- `<PREFIX>_EXPECTED_DB_UNIQUE_NAME`, `<PREFIX>_EXPECTED_CON_NAME`, and
  `<PREFIX>_EXPECTED_SERVICE_NAME`;
- file-type `<PREFIX>_GUARD_TOKEN_FILE`.

Mark values protected and mask values where GitLab supports masking. The two
explicit jobs, `oracle-integration-19c` and `oracle-integration-26ai`, run only
on protected refs, fail on an incomplete matrix, and use per-release resource
groups so two GitLab pipelines cannot mutate one endpoint at the same time.
They are release-gating jobs, not scheduled or nightly jobs. The guard-row
mutex also prevents overlap with GitHub or local runs.

All GitLab jobs require both private runner tags, `plsqlwks` and `docker`. Project
maintainers must disable GitLab instance runners and every non-compliant group or
project runner, protect the selected project runner, and disable **Run untagged
jobs**. Without a runner carrying both tags, jobs intentionally remain pending,
so hosted-runner minutes are never consumed.

### GitHub secrets and jobs

For each prefix `ORACLE_19C` and `ORACLE_26AI`, configure these protected
GitHub Actions secrets:

- `<PREFIX>_USER`, `<PREFIX>_DSN`, `<PREFIX>_PASSWORD`, and
  `<PREFIX>_DESCRIPTOR_DSN`;
- `<PREFIX>_DML_USER` and `<PREFIX>_DML_PASSWORD`;
- `<PREFIX>_READ_ONLY_USER` and `<PREFIX>_READ_ONLY_PASSWORD`;
- `<PREFIX>_EXPECTED_DB_UNIQUE_NAME`, `<PREFIX>_EXPECTED_CON_NAME`, and
  `<PREFIX>_EXPECTED_SERVICE_NAME`;
- `<PREFIX>_GUARD_TOKEN`.

Store these secrets in the protected `oracle-integration` environment and
restrict that environment to trusted branches and reviewers. The workflow
writes each password and token value to a temporary mode-`0600` file, maps that
path to the generic test variable, and removes the file during job cleanup.
Live Oracle jobs run only for repository-owned protected refs or a trusted
manual dispatch; they have no schedule, do not run for pull requests from
forks, and never expose these secrets to fork code. All other workflow jobs
also skip fork pull requests before selecting the private runner. Each job uses
`runs-on: [self-hosted, linux, x64, plsqlwks]`; no job names a GitHub-hosted
runner label. The two live jobs have separate per-release concurrency groups
and do not cancel an in-progress database run. Disable any hosted-runner
fallback allowed by the repository or organization policy. With no matching
private runner, a job must remain queued instead of consuming hosted-runner
minutes. The guard-row mutex additionally prevents overlap with GitLab or local
runs that use the same endpoint. Every referenced GitHub action is pinned to a
reviewed full commit SHA; update those pins only as a deliberate dependency
change.

### Private runner setup and first run

Use a dedicated Linux x64 runner host that contains no production credentials
or data and can reach only the required disposable Oracle endpoints and package
sources. Do not reuse a credentialed runner for untrusted fork code.

1. Install Python 3.10 and 3.14, Python build tooling, a compiler/toolchain, and
   the platform prerequisites needed by this repository. On the GitHub runner,
   expose the interpreters as executable `/usr/local/bin/python3.10` and
   `/usr/local/bin/python3.14` paths for the runner service account. Each GitHub
   job creates a workspace `.venv` directly from the selected installed
   interpreter instead of relying on GitHub-specific setup state. For
   GitLab, use the Docker executor because the Python 3.10/3.14 matrix selects
   `python:<version>-slim` images and rejects other executor environments.
2. In the GitHub repository or organization runner UI, follow the official
   [self-hosted runner setup](https://docs.github.com/en/actions/reference/runners/self-hosted-runners),
   create a runner, and run the displayed registration command and short-lived
   token on the host. Keep the default `self-hosted`, `linux`, and `x64` labels
   and add `plsqlwks`; never store the registration token in the repository.
3. In the GitLab project or group runner UI, follow the official
   [runner setup](https://docs.gitlab.com/ci/runners/), create a protected project
   runner with tags `plsqlwks` and `docker`, disable **Run untagged jobs**, and run
   the displayed registration command and token on the same host or a separately
   isolated host. Select the Docker executor and `python:3.14-slim` as its default
   image. Do not retain the token outside the runner's protected configuration.
4. Install both runner agents as system services under dedicated unprivileged
   operating-system accounts. Do not store Oracle passwords or guard tokens in
   the runner checkout or service environment. From the configured GitHub
   runner directory, `sudo ./svc.sh install`, `sudo ./svc.sh start`, and
   `sudo ./svc.sh status` install, start, and inspect that service. For a
   package-installed GitLab runner, use `sudo gitlab-runner start`,
   `sudo gitlab-runner status`, and `sudo gitlab-runner verify`.
5. Disable GitLab instance runners, non-compliant group/project runners, and
   GitHub-hosted fallback in project, organization, and billing/policy settings.
   Disable vendor-hosted remote-cache use for these workflows. Confirm a
   deliberately unmatched label leaves a harmless test job pending/queued rather
   than selecting a hosted runner, then cancel that job.
6. Add the protected GitLab variables and GitHub secrets listed above. Protect
   the GitLab runner and variables, and configure the GitHub
   `oracle-integration` environment for trusted branches or tags with the
   desired approval rules.
7. Start the services. In GitHub open **Actions > CI > Run workflow** and choose
   a protected branch. In GitLab open **Build > Pipelines > New pipeline** and
   choose a protected ref. Verify in both job UIs that the selected runner has
   the expected private identity and the required `plsqlwks` and `docker` tags
   before allowing the Oracle commands to run.
8. Confirm both Oracle version jobs pass, leave only secret-free test output in
   the logs, remove temporary credential files, and upload no Oracle artifacts
   or caches.
   Stop and investigate immediately if either platform selects an unexpected
   runner.

Self-hosted runner hardware or cloud instances, network traffic, electricity,
maintenance, and Oracle licensing remain the operator's responsibility. The
private-runner guarantee is specifically that these definitions consume no
GitHub/GitLab hosted-runner minutes and upload no Oracle artifacts or remote
caches; only non-Oracle machine-readable test and coverage reports are retained
for seven days. A job waits when the private runner is unavailable. Confirm the
account-side boundary in the current [GitHub Actions billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
and [GitLab compute-minute documentation](https://docs.gitlab.com/ci/pipelines/instance_runner_compute_minutes/)
before the first run.
