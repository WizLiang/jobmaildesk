# JobMailDesk Agent Instructions

Before product, data, parser, build, or release work, read:

- `PROJECT_RULES.md` for current data and release invariants.
- `docs/PROJECT_MEMORY.md` for historical failures, root causes, and the required verification workflow.

Non-negotiable rules:

1. Preserve stable application/task IDs and backward-compatible migrations.
2. Never merge applications by company alone; role/project/job code/attempt matter.
3. Manual `ENDED`, ignored, deleted, and tombstoned states outrank mail replay.
4. IMAP is read-only and uses `BODY.PEEK`; never persist mail bodies or credentials.
5. Every parser fix adds or updates a golden vector.
6. Every lifecycle/data-integrity fix adds a service or DAO regression test.
7. Android uses the pinned dependency matrix in `libs.versions.toml`.
8. AGP 9.3 builds run on JDK 21; Java/Kotlin bytecode remains target 17.
9. Android SDK 37 paths use `android-37.0`, not `android-37`.
10. Do not suppress lint globally or treat a sandbox Gradle startup failure as a source failure.
11. Android completion requires tests, lint, APK assembly, signature verification, checksum, and device/API-29 acceptance.
12. Do not call a build release-ready before all gates pass.
13. Do not create repeated intermediate release packages; batch fixes, verify once, deliver once.
