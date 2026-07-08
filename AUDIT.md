# OpenEMR Fork Audit

**Version:** OpenEMR 8.2.0-dev (`version.php`: `$v_major=8`, `$v_minor=2`, `$v_patch=0`, `$v_tag=-dev`)
**Database schema:** v541 (`version.php`: `$v_database = 541`, must match the version comment in `sql/database.sql`)
**ACL schema:** v13 (`version.php`: `$v_acl = 13`)

---

## Executive Summary

Five passes (Architecture, Security, Data Quality, Performance, Compliance) over this OpenEMR 8.2.0-dev fork surfaced five findings that most directly shape how an AI agent can safely read patient data:

1. **Auth is sound but bypassable.** The two-layer model — OAuth2 scopes (`AuthorizationListener`) plus legacy ACL (`AclMain`) — is a real, enforced Policy Enforcement Point for standard API calls. But "local API" / `skipAuthorization` code paths exist for in-process (non-HTTP) callers and skip both checks entirely. **The agent must always go through the full authenticated REST/FHIR path, never a local/in-process shortcut**, or its access is unaudited and unscoped.
2. **There is no "everything" endpoint, and no caching layer anywhere in the platform.** Assembling one patient's demographics + encounters + meds + problems + labs costs an estimated 10–40+ sequential queries (plus a per-service metadata-query tax with no memoization), and timeline queries lack the composite indexes needed to sort efficiently. A naive agent implementation will not hit a "seconds" bar for patients with long histories.
3. **The schema has zero foreign-key constraints, and demo/clinical data is inconsistently populated** — nullable fields, mixed date/unit formats, coded-vs-free-text drift, and orphanable rows are all possible. **The agent must treat missing or malformed fields as expected, not exceptional**, and never assume referential integrity a query result implies.
4. **The audit-logging system is real but is itself an unprotected PHI store.** `EventAuditLogger` writes tamper-evident, categorized logs — but the default config (`api_log_option = 2`) persists full plaintext API response bodies (i.e., PHI) into `api_log`, with no encryption and no retention/purge mechanism anywhere in the codebase. This is a pattern to avoid replicating, not a safeguard the agent can lean on.
5. **No BAA-covered LLM integration exists today.** Any PHI reaching an external model provider is a HIPAA disclosure requiring an executed BAA with that specific provider/tier beforehand — this is an organizational prerequisite, not something satisfied by code.

**How this changed the integration plan:** the original assumption was a thin agent that calls the API on demand and lets OpenEMR's own auth and audit handle safety. The audit changes that to a thicker agent: it must run its **own** correlation-ID audit trail (points 1, 4), enforce **minimum-necessary PHI** at prompt-construction time rather than trusting a single API pull, and own a **short-TTL, encrypted, access-controlled cache** for latency (point 2) — since caching PHI outside OpenEMR creates a new store that inherits none of OpenEMR's existing protections and must supply its own.

---

## Architecture Audit

### Monolith Structure

This is a large PHP monolith mixing legacy procedural code with a modern PSR-4 layer under one Apache/PHP-FPM webroot. Main directories:

| Directory | Purpose |
|---|---|
| `/src/` | Modern code, `OpenEMR\` PSR-4 namespace. `Services/` (business logic), `RestControllers/` (API + FHIR controllers), `Common/` (Auth, Acl, Csrf, Http, Logging, Session, Uuid...), `FHIR/` (FHIR resource mappers), `Events/` (Symfony event classes), `Core/` (kernel, globals bag). New code belongs here per `CLAUDE.md`. |
| `/library/` | Legacy procedural PHP (`auth.inc.php`, `patient.inc.php`, `forms.inc.php`, etc.), still used heavily by the classic UI. |
| `/interface/` | Classic web UI controllers + Smarty/Twig templates, organized by module (`interface/patient_file/`, `interface/forms/`, `interface/main/`, `interface/modules/` for pluggable add-ons). |
| `/templates/` | Twig (modern) and Smarty (legacy) view templates. |
| `/apis/` | REST/FHIR API bootstrap: `apis/dispatch.php` (single entry point) and `apis/routes/*.inc.php` (route tables). |
| `/oauth2/` | Thin OAuth2 endpoint entry point (`oauth2/authorize.php`); the bulk of OAuth2/OIDC logic lives in `src/RestControllers/AuthorizationController.php` and `src/Common/Auth/OpenIDConnect/`. |
| `/portal/` | Patient portal front end. |
| `/sql/` | `database.sql` (full schema) plus versioned upgrade scripts (`sql_upgrade.php`, `sql_patch.php`). |
| `/public/` | Webpack-built static assets (JS/CSS), served directly. |
| `/docker/` | Docker/Compose configs (dev stack in `docker/development-easy/`, flexible dev image in `docker/flex/`, production images in `docker/release/` and `docker/binary/`). |
| `/tests/` | PHPUnit (`Unit`, `Isolated`, `Services`, `Api`, `E2e`, `Fhir`) and Jest tests. |
| `/ccdaservice/` | Node.js microservice for CCDA/CQM document generation, invoked from PHP. |
| `/gacl/` | Bundled phpGACL library backing the legacy ACL system. |

Entry points: `index.php` (classic app), `apis/dispatch.php` (REST/FHIR API), `oauth2/authorize.php` (OAuth2), `portal/index.php` (patient portal).

### Where Patient Data Lives

Schema defined in `sql/database.sql`. Key clinical tables and their line numbers in that file:

| Data | Table(s) | `sql/database.sql` line |
|---|---|---|
| Patient demographics | `patient_data` | 8334 |
| Encounters/visits | `form_encounter` | 2022 |
| Medications (active meds) | `prescriptions` | 8698 |
| Medication list linkage | `lists_medication` | 7717 |
| Problem list (diagnoses, allergies, etc. — generic list table differentiated by `type` column) | `lists` | 7671 |
| Problem/med renewal tracking | `lists_touch` | 7744 |
| Immunizations | `immunizations` | 3235 |
| Labs/imaging orders | `procedure_order` | 10369 |
| Lab/imaging order line items | `procedure_order_code` | 10423 |
| Lab/imaging order relationships | `procedure_order_relationships` | 10562 |
| Lab/imaging results (report header) | `procedure_report` | 10467 |
| Lab/imaging results (discrete values) | `procedure_result` | 10493 |
| Lab/imaging order & result taxonomy | `procedure_type` | 10314 |
| Lab/imaging provider/facility registry | `procedure_providers` | 10280 |
| Historical/social/family history | `history_data` | 2916 |
| Patient history detail | `patient_history` | 8478 |
| Scanned documents/images | `documents` | 1391 |
| Legal document tracking | `documents_legal_detail`, `documents_legal_master`, `documents_legal_categories` | 1441, 1469, 1496 |

Note: `procedure_order` is the shared table for **both** labs and imaging orders — modality is distinguished by `procedure_type` / `procedure_order_code`, not a separate imaging table.

New code reading/writing these tables should go through `src/Services/*Service.php` classes (e.g. `EncounterService`, `PrescriptionService`, `ConditionService`) built on `QueryUtils`/`BaseService`, per `CLAUDE.md` — raw SQL against these tables should be flagged in the Security/Data Quality passes if found.

### REST/FHIR API Entry Points and Route Tables

- Single entry point: `apis/dispatch.php` — builds an `HttpRestRequest` and hands off to `src/RestControllers/ApiApplication.php`, which wires a Symfony `EventDispatcher`/`OEHttpKernel` pipeline (`CORSListener`, `OAuth2AuthorizationListener`, `AuthorizationListener`, `RoutesExtensionListener`, `ViewRendererListener`, etc. — see `src/RestControllers/ApiApplication.php:76-103`).
- Route tables, wired together in `_rest_routes.inc.php` (repo root):
  - `apis/routes/_rest_routes_standard.inc.php` — classic `/api/*` REST endpoints, backed by `src/RestControllers/*RestController.php` (e.g. `PatientRestController.php`, `EncounterRestController.php`, `PrescriptionRestController.php`, `ProcedureRestController.php`).
  - `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php` — `/fhir/*` FHIR R4 US Core endpoints, backed by `src/RestControllers/FHIR/Fhir*RestController.php`.
  - `apis/routes/_rest_routes_portal.inc.php` — patient-portal-scoped API.
- OAuth2/OIDC authorization server: `src/RestControllers/AuthorizationController.php` (wraps `league/oauth2-server`), used both for `/oauth2/*` endpoints and SMART-on-FHIR flows. Thin entry point at `oauth2/authorize.php`.

### Two-Layer Authorization Model

Every API request must pass **two** distinct Policy Enforcement Points (PEPs), as documented directly in the docblock of `src/RestControllers/Subscriber/AuthorizationListener.php:4-9`:

1. **PEP #1 — OAuth2 bearer token + scope validation** (`onKernelRequest`, `AuthorizationListener.php:112-127`): runs during `KernelEvents::REQUEST`, before routing. A chain of `IAuthorizationStrategy` implementations is tried in order (`src/RestControllers/Subscriber/AuthorizationListener.php:86-110`):
   - `LocalApiAuthorizationController` — trusted internal/local API calls.
   - `SkipAuthorizationStrategy` — explicit allowlist of unauthenticated routes (`/fhir/metadata`, `/fhir/.well-known/smart-configuration`, `/fhir/OperationDefinition`, `/api/version`, `/api/product`).
   - `BearerTokenAuthorizationStrategy` — validates the OAuth2 bearer token signature/expiry against the server's public key.
   Related: `src/RestControllers/Subscriber/OAuth2AuthorizationListener.php` handles the OAuth2 protocol endpoints themselves (`/oauth2/token`, `/authorize`, `/introspect`, `/jwk`, `/.well-known/openid-configuration`, `/registration`, `/logout`, device code flow, SMART endpoints).

2. **PEP #2 — Scope-to-resource + legacy ACL check** (`onRestApiSecurityCheck`, `AuthorizationListener.php:134-196`): runs per-route via a `RestApiSecurityCheckEvent`. Verifies the request's role (`user`/`patient`/`system`) matches the route type (standard API vs. FHIR vs. portal), constructs a scope string (e.g. `user/Patient.read`), and checks it against the token's granted `ScopeEntity` list (`AuthorizationListener.php:182-193`). **Separately**, individual route closures in the route tables call `RestConfig::request_authorization_check($request, $section, $value)` (e.g. `"patients", "demo"` or `"admin", "super"` — see `apis/routes/_rest_routes_standard.inc.php:52,57,63,70,77,85`), which delegates to the legacy phpGACL-backed ACL system: `src/Common/Acl/AclMain::aclCheckCore()`. The full permission taxonomy (sections `admin`, `acct`, `patients`, `encounters`, etc.) is documented in the class docblock at `src/Common/Acl/AclMain.php:11-60`.

So a request must hold both a valid OAuth2 scope for the resource/action **and** pass the legacy ACL permission check tied to the authenticated user's role — neither is sufficient alone.

Key files for this section:
- `apis/dispatch.php`
- `src/RestControllers/ApiApplication.php`
- `src/RestControllers/Subscriber/OAuth2AuthorizationListener.php`
- `src/RestControllers/Subscriber/AuthorizationListener.php`
- `src/RestControllers/Config/RestConfig.php` (`request_authorization_check`, `scope_check`)
- `src/Common/Acl/AclMain.php`
- `src/RestControllers/AuthorizationController.php`

---

## Security Audit

### Authentication

**Strengths:**
- Password hashing is modern and configurable (`gbl_auth_hash_algo` global): bcrypt (PHP `PASSWORD_DEFAULT`/`PASSWORD_BCRYPT`), Argon2i/Argon2id, or SHA-512 `crypt()` — all implemented in `src/Common/Auth/AuthHash.php`. No MD5/SHA1 in the core auth path; a legacy pre-5.0.0 bcrypt salt-length workaround exists (`AuthHash.php:206-223`) but rehashes to the modern algorithm on next successful login (`src/Common/Auth/AuthUtils.php:449-457`).
- Dual-layer brute-force protection: per-account lockout (`users_secure.login_fail_counter`, default 20 attempts / 1hr reset) and per-IP lockout (`ip_tracking.ip_login_fail_counter`, default 100/1hr), both in `AuthUtils.php:1171-1214`, thresholds in `library/globals.inc.php:2202-2227`. Dummy-hash verification against unknown usernames mitigates timing-based user enumeration (`AuthUtils.php:89-113`).
- Session fixation is mitigated: session ID is regenerated on successful login via `$session->migrate(true)` (`interface/main/main_screen.php:402-403`).
- Password policy is enforced by default: complexity (`secure_password`, on), minimum length 9 / maximum 72, 5-entry password history, 180-day expiration with a 30-day grace period (`AuthUtils.php:1017-1103`, defaults in `library/globals.inc.php:2131-2227`).
- MFA (TOTP and U2F, `src/Common/Auth/MfaUtils.php:22-24`) is available for both the classic UI login and the OAuth2 password grant (`src/RestControllers/AuthorizationController.php:872-908`).

**Risks / things an AI agent integration must be careful about:**
- **MFA is opt-in per user, not enforced globally** — there is no "require MFA for all users" setting (`MfaUtils::isMfaRequired()` only returns true if the user has already registered a factor). A compromised password alone is often sufficient.
- **Core session cookie is `httponly=false` by design** (to support OpenEMR's multi-tab-per-patient UX — `src/Common/Session/SessionConfigurationBuilder.php:20-28`, documented in `SessionUtil.php:7-17`), which is a real trade-off: any XSS in the classic UI can steal the session cookie via JavaScript. Portal, OAuth2, and API sessions do set `httponly=true`.
- **Default `admin`/`pass` credentials** are applied on first automated install unless `OE_USER`/`OE_PASS` are overridden (`docker/release/auto_configure.php:54-56`, `docker/release/openemr.sh:66-68`, `docker/development-easy/docker-compose.yml:66-67`). This is dev/first-boot only (the manual `setup.php` installer enforces a 12+ character username/password), but any automation that provisions OpenEMR instances must not assume these were rotated.
- LDAP-authenticated and Google-sign-in users **skip lockout, password-strength, and expiration checks** (`AuthUtils.php:13-17, 647-669, 1088-1090`) — federated auth paths inherit whatever weaker policy the IdP has.

### Authorization / Access-Control Gaps

**Strengths — the two-layer model (see Architecture Audit):**
- OAuth2 scopes are granular and context-aware: `patient/*`, `user/*`, and `system/*` prefixes distinguish "this one patient," "every patient this user's ACL allows," and "the entire database," respectively (`src/Common/Auth/OpenIDConnect/Entities/ServerScopeListEntity.php:428-432`, `Documentation/api/AUTHORIZATION.md:120-176`).
- For patient-role tokens, the patient UUID is bound server-side from the authenticated account, not trusted from the URL (`src/RestControllers/Authorization/BearerTokenAuthorizationStrategy.php:224-231`). Compartment-style FHIR resources (e.g. Encounter) that implement `IPatientCompartmentResourceService` get this UUID injected as a mandatory search filter in `src/Services/FHIR/Traits/ResourceServiceSearchTrait.php:89-97`, and `GET /fhir/Patient/:uuid` additionally hard-rejects a URL UUID that doesn't match the token's own patient (`apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:610-622`).

**Risks:**
- **`admin`/`super` ACL bypasses every check.** `src/Common/Acl/AclMain.php:173-176` grants any user holding the `admin/super` ACO access to everything, regardless of the section/value being checked. This is a legitimate "superuser" design, but it means any AI-agent service account must never be assigned this ACL role.
- **Cross-patient IDOR gap in `QuestionnaireResponse`.** `FhirQuestionnaireResponseService` does not implement `IPatientCompartmentResourceService`, so the patient-UUID filter is never applied, and the ACL check on that route is commented out (`apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php:797-801`; controller at `src/RestControllers/FHIR/FhirQuestionnaireResponseRestController.php:118,184`). A patient-scoped token can plausibly fetch another patient's questionnaire response by UUID.
- **`checkUserHasAccessToPatient()` is a stub that always returns `true`** (`BearerTokenAuthorizationStrategy.php:479-484`). SMART launch tokens bind to a patient UUID taken from launch context, but this function — meant to verify the authenticated *user* actually has ACL access to that *patient* — performs no real check.
- Several other FHIR routes have no ACL check and inconsistent/no patient-UUID binding for patient-role requests: `Location`, `Media/:uuid`, `Person/:uuid` (non-self), `Organization`, `Practitioner*` (the last two appear to be by-design open reads). See route table lines noted by the audit around `403-521` and `632-675` of `apis/routes/_rest_routes_fhir_r4_us_core_3_1_0.inc.php`.
- **`system/*` scopes are effectively unrestricted, database-wide access** (`system/Patient.rs`, `system/*.$export`, `system/*.$bulkdata-status` — `src/Common/Auth/OpenIDConnect/Entities/ServerScopeListEntity.php:62-67`), gated only by the `rest_system_scopes_api` global. Note this flag **defaults to `1` (enabled) in the dev docker-compose** (`docker/development-easy/docker-compose.yml:85`) — verify it is deliberately configured off (or tightly scoped to trusted client_credentials clients) in any non-dev environment.
- Two "fail-open" defaults exist deeper in `AclMain.php`: an empty ACO spec or an ACL-unmapped issue type both return `true` (allow) rather than denying (`AclMain.php:337-338, 369-370`) — a misconfigured/incomplete ACL registry entry silently grants access rather than blocking it.

### Data-Exposure Vectors

- **Twig autoescaping is explicitly disabled** (`src/Common/Twig/TwigContainer.php:67-70`, `'autoescape' => false`). Every template variable must be manually passed through `|text`, `|attr`, `|xlt`, etc. (helpers defined in `library/htmlspecialchars.inc.php`). Legacy Smarty templates have the same opt-in model (no global `escape_html` default modifier) — escaping relies entirely on developer discipline at every call site.
- `|raw` (Twig's escape-bypass filter) is used in ~13 templates, concentrated in the calendar UI (`templates/calendar/default/views/{day,week,month}/ajax_template.html.twig` — event HTML, month selectors) and a few others (`templates/core/about.html.twig`, `templates/patient/card/manage_care_team.html.twig`). If the underlying HTML strings these render ever include unsanitized user/DB input, this is a direct stored-XSS vector.
- **Content-Security-Policy is minimal**: only `frame-ancestors 'none'` is set, and only on the login and portal entry pages (`interface/login/login.php:32`, `portal/index.php:21`). There is no `script-src`/`default-src` restriction anywhere, so CSP provides no defense-in-depth against XSS beyond clickjacking protection.

### PHI-Handling Issues

- Field-level encryption (`src/Common/Crypto/CryptoGen.php`, AES-256-CBC + HMAC-SHA384) is applied to admin-marked "encrypted" globals, documents/CouchDB attachments (when `drive_encryption` is on), payment-gateway keys, payment audit records, and OAuth2 client secrets — **but not to general patient demographic fields**. Notably, `patient_data.ss` (Social Security Number, `sql/database.sql:8350`) is a plain `varchar(255)` with no application-level encryption; it relies entirely on database-level protections (e.g. disk/TDE encryption) if any are configured.
- `database_encryption` and `drive_encryption` globals default to enabled (`library/globals.inc.php:1028-1039`), but either can be toggled off, in which case `encryptForDatabase()`/`encryptForFilesystem()` become pass-throughs (`ContextualEncryptionTrait.php:25-27`) with no warning to downstream code that consumed data is no longer encrypted.
- **Strength:** a dedicated audit-logging subsystem exists (`src/Common/Logging/EventAuditLogger.php`) with per-event-type toggles (patient-record, scheduling, order, lab-order, lab-results, security-administration), a "breakglass" emergency-access concept, and optional IHE ATNA syslog export for HIPAA-style access auditing (`EventAuditLogger.php:44-66`). This is the right place to point any AI-agent access at, so its PHI reads are auditable like a human user's.

### Injection Risks

**SQL injection:**
- **Strength:** the modern query stack (`sqlStatement()`/`sqlQuery()` in `library/sql.inc.php:96-165`, wrapping `src/Common/Database/QueryUtils.php`) supports `?`-placeholder parameter binding through ADODB over mysqli, and essentially all `src/Services/*` code uses it correctly (e.g. `src/Services/PatientAccessOnsiteService.php:98-131`).
- **Risk:** legacy code in `/library`, `/interface/billing`, and some controllers (e.g. `controllers/C_PatientFinder.class.php:94-139`, `library/clinical_rules.php:2724-2744`, `src/Billing/BillingReport.php:55-81`) still builds SQL via string concatenation using the `add_escape_custom()` (mysqli-escape) helper from `library/formdata.inc.php` rather than bound parameters. This is defense-in-depth-weaker than binding, and dynamic column/table names (`escape_sql_column_name()`, `escape_table_name()`) rely on allowlisting rather than binding (SQL doesn't support parameterized identifiers).
- **Tooling gap:** PHPStan has no rule that catches SQL string concatenation (the only SQL-specific rule, `tests/PHPStan/Rules/Sql/SqlReservedWordRule.php`, checks reserved-word column names, not injection). Custom Semgrep rules (`semgrep.yaml:183-206`, `openemr-sql-injection-sqlstatement`/`-sqlquery`) do flag concatenated arguments to `sqlStatement()`/`sqlQuery()` in CI — this is the actual safety net, not PHPStan.

**XSS:** see Data-Exposure Vectors above (`text()`/`attr()`/`xlt()` helpers exist and are used in most places, but Twig/Smarty autoescape is off by default, so correctness depends on consistent manual escaping).

**CSRF:**
- **Strength:** CSRF protection (`src/Common/Csrf/CsrfUtils.php`) uses a per-session random 32-byte secret key (`random_bytes(32)`) to derive HMAC-SHA256 tokens per "subject" (`api` vs `default`), verified with a timing-safe `hash_equals()` comparison (`CsrfUtils.php:35-97`). This is a solid, standard construction.
- **Caveat (not independently verified in this pass):** protection is only as good as its coverage — every state-changing legacy endpoint must explicitly call `checkCsrfInput()`. This audit did not exhaustively verify that every POST handler across `/interface` does so; treat consistent CSRF coverage on legacy admin/billing screens as an open question for a follow-up pass.

### Secrets & Configuration Handling

- **Database credentials are stored in plaintext** in `sites/<site_id>/sqlconf.php` (`$pass` / `$sqlconf["pass"]`), and `sites/default/sqlconf.php` with dev-default credentials (`openemr`/`openemr`) **is committed to this repo**. Docker post-install hardening does `chmod 400` on this file (`docker/release/openemr.sh:875-878`), but the file format itself has no encryption-at-rest.
- **OAuth2 client secrets are encrypted (reversibly), not hashed** — `src/Common/Auth/OpenIDConnect/Repositories/ClientRepository.php:90-94,199-207` uses `CryptoGen::encryptForDatabase()`/decrypt + `hash_equals()` for comparison, generated via `random_bytes(64)`. This is necessary for the OAuth2 protocol (the server must be able to recover the secret to validate `client_secret_post`/`basic` auth) but means compromise of the app's crypto keys exposes all client secrets, unlike a proper one-way hash.
- The OAuth2/OIDC RSA-2048 signing keypair (`src/Common/Auth/OAuth2KeyConfig.php`) is written to `sites/<site>/documents/certificates/{oaprivate,oapublic}.key`; the private key is passphrase-encrypted PEM (`chmod 0640`) and the passphrase/encryption key are themselves stored in the `keys` DB table via `CryptoGen`.
- **A real-looking secret is committed in multiple docker-compose files**: `GITHUB_COMPOSER_TOKEN` and encoded variants in `docker/development-easy/docker-compose.yml:75-77` (and mirrored in `development-easy-light`, `development-easy-redis`, `development-insane` variants). This, plus hardcoded dev passwords (`root`, `openemr`, `pass`, `password`) throughout the dev compose files, should be treated as **dev-only and rotated/revoked if this token is still live** — flag for the user to confirm.
- `.env` is correctly gitignored (`.gitignore:4-5`) with only a minimal `.env.example`, so the Dotenv-based dev config loading (`interface/globals.php:69-71`) is not itself a leak vector.

---

### Implications for the AI Agent

Given the above, an AI agent that reads (or writes) patient data through this API must:

1. **Always authenticate through the OAuth2/OIDC layer** (`/oauth2/token`), never via direct database access or by reusing another component's session cookie. Treat the two PEPs (`AuthorizationListener::onKernelRequest` bearer-token check, then `onRestApiSecurityCheck` scope check) as mandatory, not optional — don't build a bypass path that calls service classes directly with `RestConfig::setLocalCall()`/`setNotRestCall()` "local API" shortcuts unless it is truly a trusted, in-process, non-user-facing caller.
2. **Request the narrowest scope that does the job.** For per-patient tasks, request `patient/*.read`-style scopes bound to that one patient rather than `user/*` (which spans every patient the human user can see) or, especially, `system/*` (unrestricted, whole-database access gated only by the `rest_system_scopes_api` global). The agent's own client registration should never be granted `system/*.$export`/`$bulkdata-status` unless it is explicitly meant to run bulk exports.
3. **Never assume server-side patient-compartment filtering is complete.** Most FHIR resources correctly scope patient-role tokens to the bound patient UUID, but `QuestionnaireResponse`, `Location`, `Media`, `Person`, and `Organization` currently have gaps (missing ACL and/or missing `IPatientCompartmentResourceService` binding). If the agent's own logic ever accepts a resource UUID from an untrusted source (e.g. echoed back from a prior tool call in a multi-tenant context) and passes it straight to these endpoints, add its own patient-ownership check rather than trusting the API to reject cross-patient access.
4. **Never provision or use an `admin`/`super` ACL role for the agent's service account.** That ACO bypasses every other permission check in the system (`AclMain.php:173-176`); the agent should hold the least-privileged, resource-specific ACL grants it actually needs, the same as a human user account would.
5. **Treat access tokens as short-lived (1 hour) but refresh tokens as long-lived (3 months)** (`AuthorizationController.php` TTL constants). A leaked refresh token is a standing risk; store it with the same care as a password, and prefer the `client_credentials` grant with a tightly scoped system client over long-lived user-delegated refresh tokens where possible.
6. **Never log or echo PHI in plaintext** in agent transcripts, tool outputs, or error messages by default — since general patient fields (e.g. SSN) are not field-encrypted at the application layer, whatever exposure controls exist stop at the database boundary, and it is the caller's (the agent's) responsibility not to further propagate that data unnecessarily.
7. **Route all agent reads/writes through the audited API paths so they land in `EventAuditLogger`.** Don't use "local API" or direct-service-class shortcuts that mark a request `skipAuthorization`/`isLocalApi` for user-facing data access, since those requests bypass the second PEP and may not generate the same audit trail a human-driven request would.
8. **Never commit or transmit real site credentials.** `sqlconf.php`, OAuth client secrets, and the `GITHUB_COMPOSER_TOKEN`-style values seen in the dev docker-compose files are examples of exactly the kind of secret an agent must be careful never to read out, paste into a commit, or otherwise exfiltrate from the environment it's operating in.

---

## Performance Audit

### The Service-Layer Access Path

Every modern data access should go through a `src/Services/*Service.php` class extending `src/Services/BaseService.php`, which wraps `src/Common/Database/QueryUtils.php` (see Architecture Audit). This is the right access pattern architecturally, but it has a fixed per-instantiation tax worth flagging up front:

**Every `new XService()` call pays two metadata-query round-trips before it runs any real business query.** `BaseService::__construct()` calls `QueryUtils::listTableFields($table)` (`src/Services/BaseService.php:65-73`), which itself issues `SHOW COLUMNS FROM <table>` (`src/Common/Database/QueryUtils.php:31-41`) — but to safely quote the table name, `listTableFields()` first calls `escapeTableName()`, which runs a full `SHOW TABLES` against the schema and whitelists against every table returned (`QueryUtils.php:43-60`). **Neither result is cached or memoized anywhere in `QueryUtils` or `BaseService`** — confirmed by the caching-infrastructure investigation below. So assembling a "full patient picture" by instantiating `PatientService`, `EncounterService`, `PrescriptionService`, `ConditionService`, and `ProcedureService` pays **5 × 2 = 10 metadata round-trips** before a single row of clinical data is fetched, on every request, with no way to skip it short of reusing already-constructed service instances within a request.

### Query Cost to Assemble a Full Patient Picture

The core list-fetching services are generally well-written — most use one joined query per resource type rather than looping — but there is **no single orchestrator** (no FHIR `Patient/$everything`, no "patient summary" endpoint) that assembles demographics + encounters + medications + problems + labs in one call. A caller must issue one search per resource type and stitch results together itself:

| Resource | Service / method | Query pattern | Approx. queries |
|---|---|---|---|
| Demographics | `PatientService::getOne()`/`search()` (`src/Services/PatientService.php:392-523,632-647`) | UUID-existence check, then count + UUID-page + joined data query (joins `patient_history`, `users`, `contact`/`contact_address`/`addresses`) | **4** |
| Encounters | `EncounterService::search()` (`src/Services/EncounterService.php:152-350`) | One joined query (`form_encounter` + `openemr_postcalendar_categories` + `list_options` + `facility`/`uuid_mapping` + `patient_data` + `users`) | **1** |
| Medications | `PrescriptionService::getAll()`/`getBaseSql()` (`src/Services/PrescriptionService.php:56-337`) | One `UNION`+joined query (`prescriptions` + `lists` + `drugs` + `lists_medication` + `issue_encounter` + `list_options` + `patient_data`/`form_encounter`/`users`) | **1** (+ per-row RxNorm code lookups via `BaseService::addCoding()`, `BaseService.php:551-572`) |
| Problem list | `ConditionService::search()` (`src/Services/ConditionService.php:42-94`) or `FhirConditionProblemListItemService` (`src/Services/FHIR/Condition/FhirConditionProblemListItemService.php:115-205`) | One joined query on `lists` | **1** (+ 1 query per diagnosis code via `lookup_code_descriptions()`, `BaseService.php:559-564`) |
| Labs/imaging | `ProcedureService::search()` (`src/Services/ProcedureService.php:76-321`) — the FHIR-facing path | One LEFT-JOINed query across `procedure_order` → `procedure_order_code` → `procedure_report` → `procedure_result` (+ `list_options`, `procedure_type`, `form_encounter`, `procedure_providers`, `users`, `facility`, `documents`) | **1**, plus **~2 more per report that has specimens** (`ProcedureService.php:475-505` issues a per-report order-id lookup and specimen fetch in a `foreach`) |
| Insurance/coverage | `InsuranceService::search()` (`src/Services/InsuranceService.php:77-113`) | One joined query (`insurance_companies`, `patient_data`) — **not included** in `PatientService`'s own query, must be fetched separately | **1** |

**Realistic total for a lean "full patient picture" build: ~10-15 distinct SQL round-trips** for a patient with modest history; **20-40+** once diagnosis-code lookups, RxNorm lookups, and lab specimens are counted for a patient with a non-trivial number of problems/labs. On top of that, add the **10 metadata queries** from service instantiation above (one-time per request if the agent reuses service instances, but repeated per request otherwise).

**Two legacy/N+1 landmines to avoid:**
- `ProcedureService::getAll()`/`getOne()` (the older REST path, `ProcedureService.php:681-754,868-918`) is classic N+1: one query per order for `procedure_order_code`, then one query per report for `procedure_result`, inside nested `while` loops — i.e. cost scales with `1 + orders + reports + results`, not O(1). **The FHIR-facing `search()` method does not have this problem** and should be preferred whenever an agent has a choice of code path.
- CCDA/CCD generation (`src/Services/CDADocumentService.php:113-138` → `CcdaServiceRequestModelGenerator::create_data()`) is the **heaviest** full-chart aggregation in the codebase — encounters cost `1 + 2×encounter_count` queries, problems cost `1 + (problems × codes_per_problem)` for code-description lookups, medications add a per-row provenance/code lookup. A moderate chart can hit **40-150+ queries**; a chart with many encounters/problems can exceed **500**. If an agent needs a CCDA-equivalent full summary rather than composing FHIR searches itself, this is the most expensive path available and should be avoided for latency-sensitive use.

### Indexing

Index coverage on the five key tables (all in `sql/database.sql`) is adequate for simple `WHERE pid = ?` lookups but **not** for the patient-timeline access pattern (`WHERE patient_col = ? ORDER BY date_col`) that a chart summarizer actually needs:

| Table | Indexes | Timeline fitness |
|---|---|---|
| `patient_data` (8334-8472) | `UNIQUE(pid)`, `UNIQUE(uuid)`, `(lname,fname)`, `(DOB)`, `(id)` | N/A — single-row lookup, fine as-is |
| `form_encounter` (2022-2062) | `PRIMARY(id)`, `UNIQUE(uuid)`, `(pid,encounter)`, `(date)` | **No `(pid, date)` composite** — the planner filters on `pid` then has to filesort by `date` separately, or use the `date` index and filter `pid` after |
| `prescriptions` (8698-8751) | `PRIMARY(id)`, `(patient_id)`, `UNIQUE(uuid)` | **No date index at all** — `ORDER BY date_added`/`datetime` is an unindexed filesort |
| `lists` (7671-7712) | `PRIMARY(id)`, `(pid)`, `(type)`, `UNIQUE(uuid)` | **No date index** — `ORDER BY begdate` (used for the problem-list timeline, e.g. `interface/patient_file/summary/stats_full.php:263`) is an unindexed filesort, and `pid`+`type` are two separate single-column indexes rather than one composite |
| `procedure_order` (10369-10414) | `PRIMARY`, `UNIQUE(uuid)`, `(date_ordered, patient_id)`, `(patient_id)`, plus several single-column indexes | The one composite index (`datepid`) is **date-first, not patient-first** — suboptimal for "all orders for this patient" lookups, which is the actual dominant access pattern |
| `procedure_report` / `procedure_result` (10467-10513) | FK-style single-column indexes only (`procedure_order_id`, `procedure_report_id`) | No patient or date index; always accessed via the parent order/report id, which is fine as long as the query always joins down from `procedure_order` |

**Net assessment:** every table can efficiently answer "give me this one patient's rows," but none can do so **pre-sorted** by the date column a timeline view needs — every patient-history query pays a filesort on top of the index lookup. This gets measurably worse as encounter/prescription/problem counts grow for long-tenured patients, exactly the patients whose full picture is most expensive to assemble.

### Caching Infrastructure

**There is effectively no query-result caching layer in this codebase.** This was directly investigated and confirmed:

- `src/Common/Database/QueryUtils.php` and `src/Services/BaseService.php` are pure pass-throughs to the database — no memoization, no static result cache, nothing.
- Redis is available but **only for session storage**, and only in the opt-in `docker/development-easy-redis/docker-compose.yml` stack (the default `docker/development-easy/docker-compose.yml` has no Redis at all). No code path uses Redis, APCu, or Memcached for caching query results or computed objects — `symfony/cache` is wired up only as a Doctrine ORM **metadata** cache (`config/database.php:102-110`, using an in-process `ArrayAdapter`, not query-result caching).
- The one real per-request cache is `FhirPatientService`'s in-memory memoization of `list_options` lookups (`src/Services/FHIR/FhirPatientService.php:535-549`) and a similar static translation-string cache (`src/Common/Translation/TranslationCache.php:16-24`) — both request-scoped, neither shared across requests.
- **OPcache (PHP bytecode caching, not data caching) is disabled by default in the dev stack** because Xdebug is on (`docker/flex/openemr.sh:1187-1193`, triggered by `XDEBUG_ON: 1` in `docker/development-easy/docker-compose.yml`). It is enabled with JIT in production-mode flex images. This affects overall PHP execution speed for every request (including the agent's), not just DB-bound work — dev-stack latency numbers will look meaningfully worse than a production deployment's for this reason alone.
- Search is entirely relational (MariaDB B-tree indexes); there is no Elasticsearch/Solr or other dedicated search tier for clinical data, and MySQL `FULLTEXT` indexes exist only for terminology/code tables, not patient records.

### Implications for "Seconds, Not Minutes" Latency

Given the above, a naive implementation — the agent calling the REST/FHIR API fresh for every patient-summary request — is likely to land in the **1-3 second range on a warm database with a moderate chart**, and meaningfully worse (potentially several seconds) for patients with long histories, because:
1. Every fresh set of service instantiations pays ~10 metadata round-trips with zero caching.
2. Timeline queries (encounters, medications, problems) all pay an unindexed filesort on top of their index lookup, scaling with history length.
3. There is no server-side "everything" endpoint, so the agent must make ~10-40 sequential or parallelized round-trips itself, each with its own network + auth-check overhead (recall from the Architecture Audit that **every** REST/FHIR request pays two Policy Enforcement Point checks — bearer-token validation and scope/ACL checks — on top of the query itself).
4. If the agent (or anything else in the request path) falls onto the legacy `ProcedureService::getAll()`/`getOne()` N+1 code path, or triggers CCDA generation for a "full summary," latency can jump into the multi-second-to-tens-of-seconds range for chart-heavy patients.

**Recommendation: pre-computing/caching a patient summary is warranted, not optional, if the "seconds" bar must hold across all patients, not just newly-registered ones.** Concretely:
- **Batch the agent's own calls**: reuse one instance of each `Service` class per request/session rather than constructing fresh ones, to avoid re-paying the `SHOW TABLES`/`SHOW COLUMNS` tax on every call; prefer the FHIR `search()` methods (which are single-query) over the legacy `getAll()`/`getOne()` N+1 paths, and never route through CCDA generation for a live "summarize this patient" request.
- **Add a caching layer in front of the agent's data access**, since none exists in the platform itself: cache the assembled patient-picture response (demographics + encounters + meds + problems + labs) for a short TTL keyed by patient UUID + a data-freshness signal (e.g. max `last_update`/`modifydate` timestamp across the relevant tables), invalidating on write. This is genuinely additive — it wouldn't be duplicating an existing cache, since this audit found none.
- **Consider a pre-computed summary table/materialized view** (refreshed on write or on a short interval) if the agent's query volume is high enough to matter, given the schema has no covering `(patient_id, date)` indexes to make the live timeline queries cheap — that's a server-side schema change (new index or summary table) rather than something the agent can work around purely from the client side, and would need to go through the standard `sql/*_upgrade.sql` migration process (see Architecture Audit) if pursued.
- **Treat the first request for a given patient as the expensive case** and budget for it accordingly (e.g. show a "gathering chart data" state) — a cache/pre-compute strategy can make *repeat* summaries fast, but cannot make the platform's underlying query cost disappear on a cold cache.

---

## Data Quality Audit

### Provenance and Completeness of the "Demo" Data

This repo does **not** ship patient-level sample data itself — `sql/database.sql` is schema-only (`DROP TABLE` / `CREATE TABLE` statements, no `INSERT`s for clinical tables). Demo data is optional and comes from an external, decade-old source:

- The dev container only loads demo data if `DEMO_MODE=standard` is explicitly set (`docker/flex/openemr.sh:321-324`) — the default `docker/development-easy/docker-compose.yml` stack does **not** set this, so a stock `openemr-cmd worktree up` gives an essentially empty patient database (just installer-created scaffolding: one admin user, default facility, default lists).
- When demo data *is* requested, `demoData()` (`docker/flex/utilities/devtoolsLibrary.source:230-237`) downloads `demo_5_0_0_5.sql` — **a SQL dump from OpenEMR 5.0.0** (pinned by commit SHA in `docker/flex/Dockerfile:272-276`, fetched from the `openemr-devops` repo) — imports it, then runs `upgradeOpenEMR 5.0.0` (i.e. replays every `sql/*_upgrade.sql` script from 5.0.0 up to the current 541) to bring the *schema* current.

**Agent failure mode:** any AI summarizer trained or tested against this demo set is looking at ~10-year-old clinical records whose *schema* was mechanically upgraded but whose *content* was never backfilled. Every column added after 2016 — `class_code`, `encounter_type_code`/`encounter_type_description`, `usage_category`/`request_intent` on prescriptions, `procedure_order.order_intent`/`scheduled_date`/`performer_type`, all `uuid` columns, `procedure_order_relationships`, `procedure_specimen`, etc. — will be systematically `NULL` or default-valued for **every** demo record, not just some. An agent that infers "this field is usually empty, so it's optional/unimportant" from the demo set will be wrong once it hits real-world data entered by a modern OpenEMR install, and conversely an agent QA'd only against demo data will never exercise its handling of these modern fields at all.

### Nullable / Commonly-Empty Clinical Fields

| Table | Field(s) | Nullability | Agent failure mode |
|---|---|---|---|
| `form_encounter` (`sql/database.sql:2022-2062`) | `pid`, `encounter`, `date` | all nullable (no `NOT NULL`, no default) | A summarizer joining on `pid`/`encounter` to build a patient timeline can silently drop or mis-bucket encounter rows with `NULL` values instead of erroring, understating visit history. |
| `form_encounter` | `sensitivity` (`varchar(30)`), `class_code` (`varchar(10)` default `'AMB'`) | free text / defaulted | `sensitivity` (e.g. "normal"/restricted-style flags) is unconstrained text — an agent must not assume a fixed value set, and a defaulted `class_code='AMB'` may reflect "never set" rather than "confirmed ambulatory." |
| `prescriptions` (`sql/database.sql:8698-8751`) | `patient_id`, `provider_id`, `encounter`, `start_date`, `end_date`, `filled_date` | all nullable | A medication-list summarizer computing "current meds as of encounter X" has no reliable date anchor if `start_date`/`end_date` are absent — it can only fall back to `active` (an unconstrained `int`, not boolean) or `txDate` (the only `NOT NULL` date field, of unclear semantic vs. `date_added`). |
| `prescriptions` | `rxnorm_drugcode` (nullable `varchar(25)`) vs. `drug` (nullable free-text `varchar(150)`) | coded field optional, free-text field also optional | A drug-interaction or allergy-cross-check agent that only reads the *coded* field will silently skip any prescription entered with a free-text drug name and no RxNorm code — a real gap in legacy/manually-entered records. |
| `lists` (`sql/database.sql:7671-7712`) | `pid`, `begdate`, `enddate`, `title`, `diagnosis` | all nullable | The problem list shares one table for problems, allergies, and medical history (`type` column, itself a nullable free-text `varchar(255)`, not an enum). A problem with no `begdate` can't be placed on a timeline; a `NULL` `enddate` is the *normal* signal for "still active" — an agent must not treat missing `enddate` as missing data. |
| `lists` | `severity_al` (`varchar(50)`, free text) | nullable, unconstrained | An allergy-severity classifier cannot rely on a fixed vocabulary here (e.g. "severe", "Severe", "life-threatening", blank all plausible) despite `verification` nearby pointing at a real coded list (`list_options.list_id=allergyintolerance-verification`). |
| `procedure_report` (`sql/database.sql:10467-10484`) | `procedure_order_id`, `date_collected`, `date_report` | all nullable | A lab-results summarizer keying results to the originating order can encounter reports with no `procedure_order_id` (report exists with no linkable order) and no collection/report date to sequence them by. |
| `procedure_result` (`sql/database.sql:10493-10513`) | `date`, `date_end` | nullable; everything else (`result`, `units`, `range`, `abnormal`, `facility`) is `NOT NULL DEFAULT ''` | "Not null" here is misleading for completeness purposes — an empty string is indistinguishable from "not entered" in `result`, `units`, `range`, and `abnormal`. An agent must treat `''` as missing data, not as a valid empty result. |
| `history_data` (`sql/database.sql:2916-3005+`) | `date` nullable; ~15 `last_*_exam`/`last_*vax` fields are `varchar(255)` | free text, nullable | Fields that are semantically dates (`last_mammogram`, `last_ecg`, `last_psa`, etc.) are stored as unstructured strings — see formatting section below. Dozens of generic `usertextNN`/`userdateNN`/`userareaNN` extension columns (`sql/database.sql:2980-3005+`) carry no fixed meaning at all; their content is whatever a given practice's custom layout maps to them, so an agent cannot interpret them without also reading that practice's `layout_options` configuration. |

### Formatting Inconsistencies (Dates, Units, Coded vs. Free-Text)

1. **Dates stored as free text, not `DATE`/`DATETIME`.** In `history_data`, ~15 "last exam/procedure" fields (`last_breast_exam`, `last_mammogram`, `last_gynocological_exam`, `last_rectal_exam`, `last_prostate_exam`, `last_physical_exam`, `last_sigmoidoscopy_colonoscopy`, `last_ecg`, `last_cardiac_echo`, `last_retinal`, `last_fluvax`, `last_pneuvax`, `last_ldl`, `last_hemoglobin`, `last_psa`, `last_exam_results` — `sql/database.sql:2928-2943`) are `varchar(255)`, while a handful of conceptually similar fields a few lines later (`cataract_surgery`, `tonsillectomy`, `cholecystestomy`, `heart_surgery`, `hysterectomy`, `hernia_repair`, `hip_replacement`, `knee_replacement`, `appendectomy` — lines 2963-2971) *are* proper `datetime` columns. **Agent failure mode:** a date-parsing routine that works on the `datetime` columns will choke or silently misparse the `varchar` ones, which can contain anything a user typed ("2019", "last year", "unknown", a full date, or blank).
2. **Multiple candidate "date of record" fields per row with no defined precedence.** `prescriptions` has `date_added`, `date_modified`, `datetime`, `start_date`, `txDate` (the only `NOT NULL` one), and `filled_date` (`sql/database.sql:8704-8739`) — none documented as canonical. **Agent failure mode:** two different summarization passes (or two different agent runs) can pick different "when was this prescribed" fields and produce inconsistent timelines for the same record.
3. **Units and reference ranges are unconstrained free text, not coded (e.g. UCUM).** `procedure_result.units` and `procedure_result.range` are both `varchar` with no format constraint (`sql/database.sql:10502,10504`) — the same lab test could plausibly appear across records as `"mg/dL"`, `"mg/dl"`, `"MG/DL"`, or with the range written `"3.5-5.5"`, `"3.5 - 5.5"`, or `"<5.5"`. **Agent failure mode:** a summarizer computing "is this result in range" by string-comparing `result` against `range` will fail on any of these formatting variants; naive numeric extraction from `result` will also break because `result` itself is a free-text `varchar(255)` that can hold `"Negative"`, `"See note"`, or `"5.4 (H)"` as easily as a bare number, distinguished only by the informational `result_data_type` flag (`N`/`S`/`F`/`E`/`L`) that nothing in the schema enforces.
4. **The `abnormal` flag is free text, not an enum**, despite having a fixed intended vocabulary. `procedure_result.abnormal` is `varchar(31) NOT NULL DEFAULT ''` with a comment saying `'no,yes,high,low'` (`sql/database.sql:10505`) but no `ENUM`/`CHECK` constraint backs that — any string can be stored. **Agent failure mode:** an agent flagging abnormal labs by exact string match on `"yes"`/`"high"`/`"low"` will miss variant capitalization or any value outside the intended set, and won't be warned that it's happening.
5. **Diagnosis/procedure codes are embedded in free-text fields using an ad hoc "system:code" convention rather than separate coded columns.** `lists.diagnosis` (`varchar(255)`, `sql/database.sql:7687`) and `procedure_order_code.diagnoses` (`text`, comment: `"diagnoses and maybe other coding (e.g. ICD9:111.11)"`, `sql/database.sql:10429`) both store coding-system-prefixed codes as unstructured strings, sometimes with multiple codes concatenated. **Agent failure mode:** extracting a structured ICD-9/ICD-10/SNOMED code requires string-splitting on an undocumented, non-enforced delimiter convention; a summarizer that assumes one code per field or one consistent prefix format will mis-extract or drop codes silently.
6. **Coded value + human-readable title pairs can diverge.** `prescriptions`/`lists_medication` both store `usage_category`/`usage_category_title` and `request_intent`/`request_intent_title` as **separate** columns, where the *_id (option_id) is nullable but the *_title is `NOT NULL` (`sql/database.sql:8740-8743`, `7721-7724`). **Agent failure mode:** if the underlying `list_options` row for that code is later renamed or removed, the stored `_title` text becomes stale and disagrees with what the code would resolve to today — an agent reading only the title text (easier to summarize in prose) can present outdated terminology as current.
7. **Free-text fields can contradict structured fields with no cross-field validation.** As a concrete illustration (from the test fixtures rather than demo data, but demonstrating the same schema-level gap): `tests/Tests/Fixtures/patients.php:17-33` defines a fixture patient with `title => 'Mrs.'` and `sex => 'Male'` in the same record — nothing in `patient_data`'s schema or the service layer prevents this kind of internal inconsistency. **Agent failure mode:** an agent that infers demographic facts from one field (e.g. `title`) without cross-checking others (`sex`) can produce confidently wrong summaries, and there is no database-level signal that would have caught the contradiction.

### Duplicate / Orphaned-Record Risks

- **There are zero `FOREIGN KEY` constraints anywhere in `sql/database.sql`** (repo-wide search for `FOREIGN KEY` in that file returns no matches, despite ~100 `ENGINE=` table declarations). Every `pid`/`encounter`/`patient_id`/`procedure_order_id` reference documented in this audit's Architecture section is enforced only by application code (`src/Services/*Service.php`), never by the database itself.
- **Concretely, this means:** deleting a patient does not cascade to `form_encounter`, `prescriptions`, `lists`, or `procedure_order` rows referencing that `pid`/`patient_id` unless every code path that performs patient deletion remembers to clean up each table by hand. Any table with a nullable FK-like column (`form_encounter.pid`, `prescriptions.patient_id`, `lists.pid`, `procedure_report.procedure_order_id` — all cited above) can independently end up `NULL` or pointing at a since-deleted row, producing orphaned records that are structurally indistinguishable from "not yet linked."
- **Agent failure mode:** an AI agent building a patient summary by querying encounters/prescriptions/problems/labs for a given `pid` will silently under-count if some records for that patient were orphaned by an incomplete deletion or a data-migration bug elsewhere in the system — there is no database constraint that would have prevented or flagged the orphan, and no `NOT NULL`/`CHECK` that would make the gap visible. Conversely, if the agent ever queries *without* filtering on `pid` (e.g. a buggy report), nothing at the schema level stops it from accidentally aggregating orphaned rows (`pid IS NULL`) into another patient's totals if a downstream join mishandles the null.
- **Duplicate risk:** `lists` has no uniqueness constraint on `(pid, diagnosis)` or `(pid, type, title)` — nothing prevents the same problem/allergy from being entered twice for the same patient (once via manual entry, once via a CCDA import, for instance). An agent summarizing "does this patient have condition X" by counting rows rather than de-duplicating on the coded value risks double-counting a single real-world problem as though it were reported twice, or, worse, treating two independently-entered near-duplicates (slightly different `title` text) as unrelated when they're the same problem.

### Summary: Agent Failure Modes at a Glance

| Failure mode | Root cause | Where |
|---|---|---|
| Understated patient timeline | Nullable `pid`/date columns silently drop from time-ordered joins | `form_encounter`, `prescriptions`, `lists` |
| Wrong "as of" medication state | No canonical date field among 5+ candidates | `prescriptions` |
| Missed drug-interaction/allergy checks | Coded field (`rxnorm_drugcode`) optional even when free-text `drug` is present | `prescriptions` |
| Misinterpreting "no end date" as missing data instead of "still active" | `lists.enddate` NULL is the active-problem signal, not a data gap | `lists` |
| Failed date parsing / mixed exam-history quality | Semantically-dated fields stored as free-text `varchar` alongside true `datetime` siblings | `history_data` |
| False "in range" / "abnormal" judgments | Units, ranges, and abnormal flags are unconstrained free text, not coded/enum | `procedure_result` |
| Silently mis-extracted diagnosis/procedure codes | Codes embedded in free text via an undocumented "system:code" convention | `lists.diagnosis`, `procedure_order_code.diagnoses` |
| Stale terminology presented as current | Denormalized `_title` text can drift from the `list_options` row its code points to | `prescriptions`, `lists_medication` |
| Confidently wrong demographic summaries | No cross-field validation between related free-text/coded fields | `patient_data` (illustrated via `tests/Tests/Fixtures/patients.php:17-33`) |
| Under- or double-counting patient records | No FK constraints anywhere in the schema; no dedup constraints on `lists` | Schema-wide (`sql/database.sql`) |
| Unrepresentative test/eval results | Demo dataset is a decade-old, schema-upgraded-but-content-frozen fixture, and isn't loaded by default | `docker/flex/utilities/devtoolsLibrary.source:230-237`, `docker/flex/Dockerfile:272-276` |

---

## Compliance & Regulatory Audit

### Audit-Logging Mechanism

OpenEMR has a real, purpose-built HIPAA audit-logging subsystem, centered on `src/Common/Logging/EventAuditLogger.php` (a singleton, `SingletonTrait`, `EventAuditLogger.php:32-90`):

- **What gets logged:** table-level writes are mapped to HIPAA-style event categories via a hardcoded table registry, `EventAuditLogger::LOG_TABLES` (`EventAuditLogger.php:107-174`) — e.g. `patient_data`, `lists`, `form_encounter`, `history_data`, `insurance_data` → `patient-record`; `prescriptions` → `order`; `procedure_order`/`procedure_order_code` → `lab-order`; `procedure_report`/`procedure_result` → `lab-results`; `users`/`facility`/`gacl_*` → `security-administration`. Each category has an independent on/off toggle (`audit_events_patient-record`, `audit_events_order`, `audit_events_lab-order`, `audit_events_lab-results`, `audit_events_security-administration`, `audit_events_scheduling`, `audit_events_other` — `EventAuditLogger.php:73-80`), plus a master `enable_auditlog` switch and a separate `queryEvents`/`httpRequestEvents` toggle (`EventAuditLogger.php:69,71-72`).
- **Where it's stored:** three tables, all written from a single sink, `src/Common/Logging/Audit/LogTablesSink.php`:
  - `log` (`sql/database.sql:7758-7776`) — one row per audited event: `event`, `category`, `user`, `groupname`, `comments`, `patient_id`, `success`, and a **SHA3-512 `checksum`** for tamper-evidence (`LogTablesSink.php:63`, computed from all logged field values).
  - `log_comment_encrypt` (`sql/database.sql:12560+`) — a linked row per log entry storing an `encrypt` flag and a second checksum (`checksum_api`) for API-specific events (`LogTablesSink.php:86-94`).
  - `api_log` (`sql/database.sql:92-105`) — API-call-specific detail: `user_id`, `patient_id`, `ip_address`, `method`, `request_url`, and **`request_body`/`response` as raw `longtext`** (`LogTablesSink.php:70-84`).
- **Separate DB connection by design:** `EventAuditLogger`'s audit connection is intentionally distinct from the main application connection (`EventAuditLogger.php:40-44`, `BreakglassChecker.php:28-33`) specifically so autoincrement IDs and connection setup don't cross-talk or create a dependency cycle with the thing being audited — a correctness detail worth preserving if this system is ever touched.
- **Emergency-access ("breakglass") support:** `src/Common/Logging/BreakglassChecker.php` checks group membership in a `breakglass` GACL group (`BreakglassChecker.php:49-58`) and `AuditConfig` has a `forceBreakglass` flag (`EventAuditLogger.php:70`, backed by `gbl_force_log_breakglass`) — i.e. the platform has a documented concept of emergency access that force-enables logging, appropriate for the "break the glass to save a life" HIPAA access scenario.
- **Optional IHE ATNA export:** when `enable_atna_audit` is set, events are additionally streamed to an external syslog collector over TLS (`Audit\AtnaSink`, `Audit\Atna\TcpWriter` — `EventAuditLogger.php:50-65`), the standard mechanism health systems use to feed audit events into a centralized SIEM for HIPAA Security Rule §164.312(b) audit control requirements.

**Gap — the audit log is itself an unencrypted PHI store by default.** `ApiResponseLoggerListener::onRequestTerminated()` (`src/RestControllers/Subscriber/ApiResponseLoggerListener.php:39-104`) logs the **full JSON response body** of every non-local API call into `api_log.response`/`request_body` whenever the `api_log_option` global is `2` ("Full Logging") — and **`2` is the documented default** (`library/globals.inc.php:2893-2902`: `'2', // default`, described as `"Full includes requests and responses"`). Since FHIR/REST responses routinely *are* PHI (a `Patient`, `Observation`, `Condition` resource body), this means a default install duplicates PHI into a plaintext `longtext` column with no `CryptoGen` encryption applied at that insertion point (`LogTablesSink.php:96-98` inserts `$apiLogData` — which includes the raw `response`/`request_body` — directly, unlike the payment/document/OAuth-secret paths noted in the Security Audit that do call `encryptForDatabase()`). Anyone auditing this system for HIPAA compliance needs to know the audit trail is not exempt from the same minimum-necessary and encryption-at-rest expectations as the primary clinical tables — it may need the same protection *and* its own access controls, since it's an aggregation point across every patient the API has ever touched.

### Data Retention

**There is no built-in retention/purge mechanism for the audit-log tables** (`log`, `log_comment_encrypt`, `api_log`) or general clinical data in this codebase — no cron job, no `background_services` entry (`sql/database.sql:189-201`), no global setting analogous to "delete audit logs older than N days" was found anywhere in `library/globals.inc.php` or the codebase search for retention/purge logic. Practically, this means:
- Audit logs — including the full PHI-bearing API response bodies described above, under the default logging level — accumulate indefinitely unless an operator builds their own external archival/purge process.
- This is a meaningful operational gap for HIPAA's implicit expectation (and many state laws' explicit requirements) that PHI not be retained indefinitely without a documented retention/disposal schedule, and it means the audit table itself can become one of the largest, longest-lived stores of PHI in the system — a natural target for a breach and a growing e-discovery/subject-access-request burden over time.
- Separately, `interface/modules/custom_modules/oe-module-ehi-exporter/` implements the ONC (b)(10) "Electronic Health Information Export" certification requirement (`Readme.md:1-30`) — i.e. the *patient right-of-access / data portability* side of compliance (exporting a full designated record set on request) is implemented, even though the *retention/disposal* side is not automated.

### BAA Implications of Sending PHI to an External LLM Provider

This repo currently has **no integration with any external LLM provider** — searches for common provider domains/APIs (OpenAI, Anthropic, etc.) inside `/src` turned up no real matches. This section is therefore forward-looking guidance for the agent integration itself, not a finding about existing code:

- **Any PHI that leaves this system and reaches an external LLM API is a "disclosure" under HIPAA**, and the LLM provider becomes a **Business Associate** the moment it processes PHI on the covered entity's behalf. Sending PHI to a provider without an executed **Business Associate Agreement (BAA)** in place is itself a HIPAA violation, independent of how secure the transmission or the provider's infrastructure is.
- Most consumer-facing LLM APIs (including default/free tiers of major providers) **do not offer a BAA** and **do explicitly use submitted data for model training/improvement** by default — sending PHI to such an endpoint is not just a compliance risk on paper, it can mean patient data is retained and potentially reproduced by the provider outside of any covered-entity control.
- Providers that **do** offer HIPAA-eligible/BAA-covered API tiers typically require: (1) an explicitly executed BAA before any PHI is sent, (2) use of the specific "enterprise"/"business"/"HIPAA-eligible" API surface (not the general consumer product), and (3) confirmation that logging/retention on the provider side is disabled or configured to the BAA's terms. None of that is a code-level guarantee — it has to be verified organizationally before the first PHI-bearing request is made, and re-verified if the agent's provider, model, or API tier ever changes.
- Even with a BAA in place, the **minimum-necessary standard still applies** (see below) — a BAA licenses the relationship, it does not license sending more PHI than the task requires.

### Implications for the AI Agent

1. **Never send PHI to an LLM provider without a verified BAA covering that exact provider/product/API tier.** This must be confirmed by a human with authority over the covered entity's compliance program before the agent's first PHI-bearing call — it is not something the agent can self-certify by reading a provider's marketing page.
2. **Route every PHI read/write the agent performs through the audited API paths**, not through "local API"/`skipAuthorization` shortcuts (see the Security Audit's implications), so it lands in `EventAuditLogger` and is subject to the same `log`/`api_log` trail as a human user's actions. If the agent bypasses this, there is no record it ever touched a given patient's data at all.
3. **Add the agent's own correlation ID to every request and every log line it emits**, distinct from OpenEMR's internal `log.id`/`api_log.id` autoincrement keys — a single user-facing "summarize this patient" task can fan out into 10-40+ underlying API calls (per the Performance Audit), and without a shared correlation ID threading through them, reconstructing "what did the agent do to answer this one question" from the `log`/`api_log` tables after the fact is impractical. Emit it as a custom header or query parameter the agent controls, and record it in the agent's own logs alongside the OpenEMR `log_id`/`api_log.id` it corresponds to (visible via `LogTablesSink::record()`'s `$lastLogId`, `LogTablesSink.php:60-61`, though that value isn't currently returned to the API caller — the agent's own request/response logging is the only place this join can happen today).
4. **Record, for every agent action: who asked, what was asked, and what data sources were consulted** — the human user (or system) that triggered the agent, the natural-language request or task description, the specific OpenEMR resources/endpoints queried (patient UUID, resource types, date ranges), and which of those results were actually used in the agent's final output versus fetched-but-discarded. This is a stricter standard than OpenEMR's own audit log captures (which records that an endpoint was hit and by whom, not why, or what an automated consumer did with the result) — the agent needs its own audit layer on top, because "an AI agent read this patient's full chart" is exactly the kind of access a compliance officer or patient (via an accounting-of-disclosures request) will want explained in plain language, not reconstructed from raw API log rows.
5. **Apply minimum-necessary PHI at every step, not just at the final output.** Concretely: request only the resource types and date ranges the specific task needs (don't reflexively pull "everything" because the API makes it easy to compose); when the task doesn't require patient-identifying fields (e.g. aggregate quality-measure computation), strip or avoid fetching identifiers rather than fetching-then-discarding; when constructing the LLM prompt itself, include only the specific fields relevant to the question asked, not the full raw record dump the API returned; and never persist PHI in the agent's own logs, transcripts, or caches beyond what's operationally necessary for the correlation/audit trail in point 3 — a full chart cached "for speed" (per the Performance Audit's caching recommendation) is a second, agent-controlled copy of PHI that inherits every retention/encryption/access obligation the primary system has, and none of the primary system's existing safeguards apply to it automatically.
6. **Treat the agent's own cache/log store as PHI that needs the same protections this audit found lacking in OpenEMR's own `api_log` table** — don't repeat the "full response body in plaintext with no retention policy" pattern found in `ApiResponseLoggerListener.php`/`library/globals.inc.php:2893-2902` in whatever the agent builds on top; if the agent caches patient summaries for latency (as the Performance Audit recommends), that cache needs its own encryption-at-rest, access control, and expiry — it cannot rely on OpenEMR's protections because it lives outside OpenEMR's database.
