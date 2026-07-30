# Niki API 端点清单（自动生成）

> 来源：`/openapi.json` · 总数：**138 个端点**

## 分布

| 切片 | 端点数 |
|---|---|
| auth | 15 |
| memory | 15 |
| session | 13 |
| dashboard | 12 |
| push | 10 |
| content-policy | 8 |
| resilience | 8 |
| emotion | 7 |
| treehole | 7 |
| tools | 6 |
| media | 5 |
| routing | 5 |
| chat | 3 |
| encyclopedia | 3 |
| health | 3 |
| safety | 3 |
| tutoring | 3 |
| audio | 2 |
| children | 2 |
| pairing | 2 |
| alerts | 1 |
| auth_audit | 1 |
| config | 1 |
| deletion | 1 |
| data-portability | 1 |
| status | 1 |

## 覆盖率（BE-API-006）

按 tag 统计 `summary` 是否齐全。 **目标：所有 `Missing` 为 0。**

| Tag | Endpoints | With summary | Missing summary |
|---|---|---|---|
| auth | 15 | 15 | 0 |
| memory | 15 | 15 | 0 |
| session | 13 | 13 | 0 |
| dashboard | 12 | 12 | 0 |
| push | 10 | 10 | 0 |
| content-policy | 8 | 8 | 0 |
| resilience | 8 | 8 | 0 |
| emotion | 7 | 7 | 0 |
| treehole | 7 | 7 | 0 |
| tools | 6 | 6 | 0 |
| media | 5 | 5 | 0 |
| routing | 5 | 5 | 0 |
| chat | 3 | 3 | 0 |
| encyclopedia | 3 | 3 | 0 |
| health | 3 | 3 | 0 |
| safety | 3 | 3 | 0 |
| tutoring | 3 | 3 | 0 |
| audio | 2 | 2 | 0 |
| children | 2 | 2 | 0 |
| pairing | 2 | 2 | 0 |
| alerts | 1 | 1 | 0 |
| auth_audit | 1 | 1 | 0 |
| config | 1 | 1 | 0 |
| data-portability | 1 | 1 | 0 |
| deletion | 1 | 1 | 0 |
| status | 1 | 1 | 0 |

✅ 100% coverage.

## 完整清单

| 方法 | 路径 | 标签 | 摘要 |
|---|---|---|---|
| GET | `/api/v1/alerts/stream/{child_id}` | alerts | SSE stream of high-risk alerts for the calling parent |
| POST | `/api/v1/audio/synthesize` | audio | Synthesise speech from text and return base64-encoded audio |
| POST | `/api/v1/audio/transcribe` | audio | Run ASR on an uploaded audio blob and return recognised text |
| GET | `/api/v1/auth/audit/recent` | auth_audit |              Return the most recent audit events the caller is allowed to see              |
| GET | `/api/v1/auth/captcha/challenge` | auth | Mint a captcha challenge |
| POST | `/api/v1/auth/captcha/verify` | auth | Verify a captcha answer |
| GET | `/api/v1/auth/consent/document/{consent_type}` | auth | Get consent document text |
| POST | `/api/v1/auth/deletion/complete` | auth | Complete account deletion |
| POST | `/api/v1/auth/deletion/request` | auth | Request account deletion |
| POST | `/api/v1/auth/logout` | auth | Logout (revoke access JWT) |
| GET | `/api/v1/auth/me/{parent_id}` | auth | Get parent account |
| POST | `/api/v1/auth/refresh` | auth | Refresh access + refresh token |
| POST | `/api/v1/auth/signup/consent` | auth | Step 3: Sign consent document |
| POST | `/api/v1/auth/signup/face` | auth | Step 2: Face verification |
| POST | `/api/v1/auth/signup/real-name` | auth | Step 1: Real-name verification |
| POST | `/api/v1/auth/signup/start` | auth | Begin parent signup |
| POST | `/api/v1/auth/sms/login` | auth | SMS-code login |
| POST | `/api/v1/auth/sms/send` | auth | Send a 6-digit SMS code |
| GET | `/api/v1/auth/whoami` | auth | Inspect current JWT |
| POST | `/api/v1/chat/full` | chat | Run the full chat pipeline end-to-end |
| GET | `/api/v1/chat/stages` | chat | List the stages of the chat pipeline (for debugging) |
| POST | `/api/v1/chat/stream` | chat | Stream a chat reply via Server-Sent Events |
| GET | `/api/v1/children/list/{parent_id}` | children | List child_ids owned by the calling parent |
| POST | `/api/v1/children/register/{parent_id}` | children | Register a child under a parent |
| GET | `/api/v1/config/client` | config | Return the client-facing runtime config |
| POST | `/api/v1/dashboard/alerts/batch/dismiss` | dashboard | Dismiss many alerts in a single round trip (BE-API-005) |
| POST | `/api/v1/dashboard/alerts/record/high-risk` | dashboard | Record a high-risk signal (self-harm, etc.) |
| POST | `/api/v1/dashboard/alerts/record/safety-valve` | dashboard | Record a tutoring safety-valve trigger |
| POST | `/api/v1/dashboard/alerts/{alert_id}/delivered` | dashboard | Mark a high-risk alert as delivered |
| GET | `/api/v1/dashboard/alerts/{child_id}` | dashboard | Get pending alerts (PRD #27, #29) |
| POST | `/api/v1/dashboard/baseline/add` | dashboard | Add a baseline record (for testing/demo) |
| POST | `/api/v1/dashboard/events/knowledge` | dashboard | Record a knowledge-base query |
| POST | `/api/v1/dashboard/events/tutoring` | dashboard | Record a tutoring event (for whitepaper generation) |
| GET | `/api/v1/dashboard/interest/{child_id}` | dashboard | Get the interest graph (PRD #25) |
| GET | `/api/v1/dashboard/learning/{child_id}` | dashboard | Get the learning whitepaper (PRD #26) |
| GET | `/api/v1/dashboard/weekly-report/{child_id}` | dashboard | Get the weekly emotion report (PRD #24) |
| GET | `/api/v1/dashboard/weekly-report/{child_id}/pdf` | dashboard |              Get the weekly emotion report as a downloadable PDF (BE-DASH-001)              |
| GET | `/api/v1/deletion/{request_id}/receipt` | deletion | Download a parent-facing PDF certificate of deletion |
| GET | `/api/v1/emotion/baseline` | emotion | Get baseline statistics |
| POST | `/api/v1/emotion/baseline/add` | emotion | Add a record to the baseline (for testing/demo) |
| POST | `/api/v1/emotion/events/knowledge` | emotion | Record a knowledge-base query |
| POST | `/api/v1/emotion/events/tutoring` | emotion | Record a tutoring event (for whitepaper generation) |
| POST | `/api/v1/emotion/express` | emotion | Child shares an emotional expression |
| GET | `/api/v1/emotion/good-night` | emotion | Short good-night greeting (PRD #16) |
| POST | `/api/v1/emotion/proactive-care` | emotion | Run the 8pm proactive-care check (PRD #12) |
| POST | `/api/v1/encyclopedia/ask` | encyclopedia | Ask an encyclopedia question |
| GET | `/api/v1/encyclopedia/knowledge` | encyclopedia | List L1 knowledge entries (debugging endpoint) |
| POST | `/api/v1/encyclopedia/search` | encyclopedia | Search the KB (debugging endpoint) |
| GET | `/api/v1/health` | health | Check the health of all infrastructure components |
| GET | `/api/v1/health/live` | health |              Kubernetes-style liveness probe — just checks the process is alive              |
| GET | `/api/v1/health/ready` | health |              Kubernetes-style readiness probe — checks if the app can serve traffic              |
| POST | `/api/v1/media/avatar` | media | Upload a child-profile avatar (BE-MEDIA-004) |
| POST | `/api/v1/media/ocr` | media | Run OCR on an uploaded image and return extracted text |
| POST | `/api/v1/media/upload` | media |              Receive a file upload and store it via the active MediaStorage              |
| GET | `/api/v1/media/{file_id}` | media | Read back a previously uploaded media file |
| GET | `/api/v1/media/{file_id}/thumb` | media | Return a server-resized thumbnail (BE-MEDIA-001-rollout) |
| POST | `/api/v1/memory/graph/apply-decay` | memory | Apply PRD #50 time decay to all relations |
| GET | `/api/v1/memory/graph/entities` | memory | List entities in the cognitive graph (BE-API-003: paginated) |
| POST | `/api/v1/memory/graph/entities` | memory | Add an entity (with automatic alignment/dedup) |
| POST | `/api/v1/memory/graph/entities/batch` | memory | Add many entities in a single round trip (BE-API-005) |
| POST | `/api/v1/memory/graph/relations` | memory | Add a relation between two entities |
| POST | `/api/v1/memory/graph/relations/batch` | memory | Add many relations in a single round trip (BE-API-005) |
| POST | `/api/v1/memory/hook/process` | memory | Run the daytime memory hook on a child message |
| POST | `/api/v1/memory/long-term/add` | memory | Add a memory record to long-term storage |
| POST | `/api/v1/memory/long-term/search` | memory | Semantic search over long-term memory |
| GET | `/api/v1/memory/pipeline/dead-letter` | memory | Get the dead-letter queue contents (PRD #47) |
| GET | `/api/v1/memory/pipeline/health` | memory | Get pipeline health status (PRD #47) |
| GET | `/api/v1/memory/pipeline/run` | memory | Trigger a pipeline run (for testing/admin) |
| GET | `/api/v1/memory/short-term/{session_id}` | memory | Get recent conversation turns for a session |
| POST | `/api/v1/memory/short-term/{session_id}` | memory | Append a turn to the session's recent memory |
| DELETE | `/api/v1/memory/short-term/{session_id}` | memory | Clear a session's recent memory |
| POST | `/api/v1/pairing/code` | pairing | Mint a 6-digit pairing code for one of the caller's children |
| POST | `/api/v1/pairing/consume` | pairing |              Redeem a 6-digit code (called by the kid's H5 — unauthenticated)              |
| GET | `/api/v1/parents/{parent_id}/data-export` | data-portability | Download a ZIP of all of a parent's data (PIPL Art |
| POST | `/api/v1/policy/age/grade` | content-policy | Grade a concept for a child's age |
| POST | `/api/v1/policy/age/grade-text` | content-policy | Grade all concepts mentioned in text |
| GET | `/api/v1/policy/age/{child_id}` | content-policy | Get the age band for a child |
| GET | `/api/v1/policy/blacklist/{family_id}` | content-policy | List all blacklisted topics for the family |
| POST | `/api/v1/policy/blacklist/{family_id}/add` | content-policy | Add a topic to the family blacklist |
| POST | `/api/v1/policy/blacklist/{family_id}/evaluate` | content-policy | Evaluate text for blacklist matches |
| POST | `/api/v1/policy/blacklist/{family_id}/remove` | content-policy | Remove a topic from the family blacklist |
| POST | `/api/v1/policy/downgrade/evaluate` | content-policy | Apply all downgrade rules (blacklist + age) to text |
| GET | `/api/v1/push/active` | push | List only the *enabled* devices for the calling parent |
| GET | `/api/v1/push/devices` | push | List every device registered for the calling parent |
| GET | `/api/v1/push/devices-full` | push | List every push device registered to the calling parent |
| DELETE | `/api/v1/push/devices-full/{device_id}` | push | Unregister a single device (logout-from-this-device flow) |
| POST | `/api/v1/push/enable/{device_id}` | push | Re-enable a previously kicked device |
| POST | `/api/v1/push/kick/{device_id}` | push | Disable a single device (BE-DEV-003: "踢下线" flow) |
| GET | `/api/v1/push/prefs` | push | Return notification preferences (defaults if unset) |
| PATCH | `/api/v1/push/prefs` | push | Partial update of notification preferences |
| POST | `/api/v1/push/register` | push | Register (or re-register) the calling parent's device |
| POST | `/api/v1/push/unregister` | push | Remove a registered device |
| POST | `/api/v1/resilience/breaker/force-open` | resilience | Manually trip the breaker (for testing/admin) |
| POST | `/api/v1/resilience/breaker/reset` | resilience | Manually reset the breaker to CLOSED |
| GET | `/api/v1/resilience/breaker/stats` | resilience | Get current circuit-breaker statistics |
| POST | `/api/v1/resilience/chat` | resilience | Example call via the circuit-breaker-aware client |
| GET | `/api/v1/resilience/degradation` | resilience | Get current degradation state (for the parent dashboard) |
| POST | `/api/v1/resilience/degradation/recover` | resilience | Mark degradation as recovered (e.g |
| POST | `/api/v1/resilience/simulate-outage` | resilience |              Simulate a DeepSeek-V4 outage by switching to the failing client              |
| POST | `/api/v1/resilience/simulate-recovery` | resilience | Switch back to a working DeepSeek-V4 client |
| POST | `/api/v1/routing/classify` | routing | Classify input text into intent categories |
| POST | `/api/v1/routing/fuse` | routing | Demonstrate fusion of mocked agent outputs |
| POST | `/api/v1/routing/persona/inject` | routing | Inject the persona fragment into a base system prompt |
| GET | `/api/v1/routing/personas` | routing | List all available personas with their TTS parameters |
| POST | `/api/v1/routing/route` | routing | Classify and route to the appropriate agent(s) |
| POST | `/api/v1/safety/chat` | safety | Same as /chat but routed under /safety/chat for debugging |
| POST | `/api/v1/safety/check-input` | safety | Run the input safety gateway against arbitrary text |
| POST | `/api/v1/safety/check-output` | safety | Run the output safety gateway against arbitrary text |
| POST | `/api/v1/session/activity` | session | Record activity (resets idle timer) |
| POST | `/api/v1/session/end` | session |              End a session naturally (child says bye, or hard limit reached)              |
| GET | `/api/v1/session/is-exhausted/{child_id}` | session | Check if the hard daily limit is reached |
| GET | `/api/v1/session/is-idle/{child_id}` | session | Check if the idle timeout is reached (PRD #17) |
| POST | `/api/v1/session/parent/clear-pause` | session | Clear the pause state (when parent resumes) |
| POST | `/api/v1/session/parent/consume-pause` | session | Consume a pending pause (returns natural exit message) |
| POST | `/api/v1/session/parent/end-session` | session | End a session as a parent-pause (with natural exit message) |
| GET | `/api/v1/session/parent/has-pause/{child_id}` | session | Check if a parent pause is pending for the child |
| POST | `/api/v1/session/parent/pause` | session | Record a pause request from the parent's mini-program |
| GET | `/api/v1/session/parent/paused/{child_id}` | session | True if the child has been paused (any reason) |
| GET | `/api/v1/session/should-warn/{child_id}` | session |              Check if the soft warning should be issued (5 min before hard limit)              |
| POST | `/api/v1/session/start` | session | Start a new session for a child |
| GET | `/api/v1/session/stats/{child_id}` | session | Get current session stats |
| GET | `/api/v1/status` | status | Coarse-grained health for the public status page |
| GET | `/api/v1/tools/allowed/{agent}` | tools | List all tools an agent can use |
| GET | `/api/v1/tools/audit` | tools | Get the access-check audit log (PRD: 降级不降安全) |
| POST | `/api/v1/tools/check` | tools | Check whether an agent can use a tool |
| POST | `/api/v1/tools/grant` | tools | Grant a tool to one or more agents (admin) |
| GET | `/api/v1/tools/matrix` | tools | Return the full tool permission matrix |
| POST | `/api/v1/tools/revoke` | tools | Revoke all access to a tool (admin) |
| POST | `/api/v1/treehole/aggregate` | treehole |              Compute the aggregated baseline contribution from treehole data              |
| POST | `/api/v1/treehole/engage` | treehole | Engage treehole mode for a child message |
| POST | `/api/v1/treehole/purge-all` | treehole | Force-purge treehole records |
| POST | `/api/v1/treehole/purge-expired` | treehole | Run the 7-day purge (normally called by cron job) |
| POST | `/api/v1/treehole/should-activate` | treehole | Check if a given text would activate treehole mode |
| GET | `/api/v1/treehole/store-stats` | treehole | Get the current treehole repository statistics |
| GET | `/api/v1/treehole/summaries/{child_id}` | treehole |              List stored treehole summaries (parent dashboard, BE-API-003 paginated)              |
| POST | `/api/v1/tutoring/ask` | tutoring | Ask for help with a stuck problem |
| GET | `/api/v1/tutoring/difficulty` | tutoring | Show the per-subtype difficulty discount matrix |
| GET | `/api/v1/tutoring/strategies` | tutoring | List the 4-round guided-discovery strategy templates |
