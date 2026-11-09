# Live demo: adding MemMachine to a customer's existing setup

The demo plays out what a customer actually goes through. They already run
LiteLLM, Keycloak and Open WebUI, and they already use the **old** memory hook
(the `main` branch's), which talks to MemMachine core directly. We add the
platform and upgrade the hook, changing as little of their setup as possible.

| Phase | Script | Where | What |
|---|---|---|---|
| 0 | `0-baseline/server.sh` | VM-1 | The customer's server: Keycloak, MemMachine core, LiteLLM with virtual keys and the old hook |
| 0 | `0-baseline/client.sh` | VM-2 | The customer's Open WebUI (Corp SSO) and an employee's Claude Code, both stock |
| 1 | `1-integrate/server.sh` | VM-1 | Install the platform; Keycloak additions; upgrade the hook (preflight, backup, swap, automatic rollback) |
| 1 | `1-integrate/client.sh` | VM-2 | Open WebUI: 4 env lines + restart; Claude Code: headers in its settings |
| 1 | `1-integrate/verify.sh` | VM-1 | The repo's end-to-end checks against this layout |
| 2 | `2-standalone/memmachine.sh` | VM-2 | MemMachine core installed alone, step by step, timed |

```
   your laptop ── ssh (only if you need to look) ──┐      your laptop
                                                   │        │ ssh -L 8087, 8180, 8100
                                                   ▼        ▼  (the one tunnel, to VM-2)
 ┌─ VM-1  "customer server", VPC only ──────┐      ┌─ VM-2  "employee side" ─────────┐
 │ Keycloak :8180 ◄─────────────────────────┼──────┤ socat 127.0.0.1:8180            │
 │ platform :8100 (phase 1) ◄───────────────┼──────┤ socat 127.0.0.1:8100 (phase 1)  │
 │ LiteLLM  :4011 + hook ◄──────────────────┼──────┤ Open WebUI :8087, Claude Code   │
 │   phase 0: old hook ──► core :8090       │      │                                 │
 │   phase 1: new hook ──► platform ──► core :8090 │ phase 2: standalone core        │
 │ Postgres, Qdrant (127.0.0.1)             │      │                                 │
 └──────────────────────────────────────────┘      └─────────────────────────────────┘
```

**You work on VM-2, the employee's side.** Your browser reaches Open WebUI, the
portal and Keycloak through one ssh tunnel to VM-2, and Claude Code runs there.
VM-1 takes traffic only from inside the VPC. Its ssh is open to you only so you
can look around when you need to.

**Keycloak's and the portal's address.** Every component, and your browser,
uses `http://127.0.0.1:8180` (Keycloak) and `http://127.0.0.1:8100` (portal):
- On VM-1, they run locally.
- On VM-2, socat units (`ltm-forward@8180`, `ltm-forward@8100`) forward those
  ports to VM-1.
- On your laptop, the ssh tunnel to VM-2 forwards them.

A real company would use DNS names instead. The forward stands in for
that, and isn't part of what we deliver. There are two reasons for doing it this
way:
- Keycloak's login cookies are `Secure`, and a browser accepts those over http
  only from `127.0.0.1`.
- Open WebUI and the platform have to reach Keycloak by the same URL the browser
  uses, or token issuers won't match.

**People.** Corp SSO users have an `employee_id` attribute: admin E10000, alice
E12345, bob E67890, carol E24680. After phase 1, memory is organized by
employee ID. Memories stored by the old hook were organized by email, so they
stay where they are and the new setup doesn't see them. We don't migrate them.

## 1. Launch two VMs

Launch both in the same VPC and subnet, with Ubuntu 26.04 LTS on x86_64.

| | VM-1 (server) | VM-2 (frontends) |
|---|---|---|
| Type | `t3.xlarge` (16 GB), 40 GB gp3 | `t3.large` (8 GB), 30 GB gp3 |
| Public IP | yes (ssh, and outbound installs) | yes |
| Inbound SG | 22 from your IP; 4011, 8180, 8100 from **VM-2's SG** | 22 from your IP; 8087 from VM-1's SG |

VM-1's services answer only inside the VPC, and only to VM-2:
- 4011 is LiteLLM, for Open WebUI and Claude Code.
- 8180 is Keycloak.
- 8100 is the platform. It serves the portal, and its memory API answers only
  to a registered service credential.

VM-2's 8087 is open to VM-1 only for `verify.sh`, which signs in to Open WebUI
from there. Without that check, VM-2 needs nothing but 22.

Both VMs need outbound internet for installs (GitHub, PyPI,
Keycloak releases, claude.ai's installer). VM-1 also needs it for DeepInfra and
OpenAI.

You need two provider keys: `DEEPINFRA_API_KEY` (chat, GLM-5.2) and
`OPENAI_API_KEY` (`text-embedding-3-small`). Both go into VM-1's LiteLLM only.

## 2. Copy the repo to both

From your checkout. Copying instead of cloning means the VMs need no GitHub
access, and an unpushed branch works too:

```bash
V1=ubuntu@<VM-1 public IP>; V2=ubuntu@<VM-2 public IP>
RS=(rsync -az --exclude .venv --exclude __pycache__ --exclude '.*_cache')
for V in $V1 $V2; do
  ssh $V 'sudo mkdir -p /opt/ltm-platform-env && sudo chown ubuntu: /opt/ltm-platform-env'
  "${RS[@]}" ./ $V:/opt/ltm-platform-env/repo/
done
```

The old hook comes from the repo's own git history (`main`), so keep `.git` in
the copy.

Each script can be rerun. Finished steps are skipped (markers in
`/opt/ltm-platform-env/.state/`), and each step logs how long it took.

## 3. Phase 0: the customer's setup as it is today

VM-1 (`ssh $V1`):

```bash
cd /opt/ltm-platform-env/repo
read -rs DEEPINFRA_API_KEY; read -rs OPENAI_API_KEY; export DEEPINFRA_API_KEY OPENAI_API_KEY
CLIENT_IP=<VM-2 private IP> DEMO_PASSWORD='<easy to type in front of people>' \
  bash deploy/aws/0-baseline/server.sh 2>&1 | tee ~/phase0.log
```

To add people beyond admin/alice/bob/carol, pass
`EXTRA_USERS='yu.xu@memverge.com:<password>:E13579'` (space-separated,
`email:password:employee_id`). Each gets a Corp SSO login and a personal
LiteLLM key owned by their employee ID (in `keys.env` as `YUXU_VIRTUAL_KEY`).
Their first chat enrols them with the platform. You can rerun the script any
time to add more.

Hand over what VM-2 needs (OAuth client secret, Open WebUI's LiteLLM key,
Alice's key, the admin password). Run this from your laptop:

```bash
H=/opt/ltm-platform-env/handoff
ssh $V1 cat $H/client-baseline.env | ssh $V2 "mkdir -p $H && umask 077 && cat > $H/client-baseline.env"
```

VM-2 (`ssh $V2`):

```bash
bash /opt/ltm-platform-env/repo/deploy/aws/0-baseline/client.sh 2>&1 | tee ~/phase0.log
```

## 4. Connect from your laptop

```bash
ssh -N -L 8087:127.0.0.1:8087 -L 8180:127.0.0.1:8180 -L 8100:127.0.0.1:8100 $V2
```

| What | URL |
|---|---|
| Open WebUI | http://127.0.0.1:8087 (Corp SSO) |
| Platform portal (after phase 1) | http://127.0.0.1:8100/portal/ |
| Keycloak admin | http://127.0.0.1:8180/admin/ (`keycloak.env` on VM-1) |

All three are on VM-2's 127.0.0.1. Open WebUI runs there, and the other two
are its forwards to VM-1. The portal answers only after phase 1.

Use `127.0.0.1`, never `localhost`. Passwords are in
`/opt/ltm-platform-env/accounts.txt` on VM-1.

Claude Code runs on VM-2, as any person with a key. `show-keys.sh` on VM-1
prints the keys; `claude-as.sh` on VM-2 starts Claude Code as one person, in a
new empty folder. To hand every key over at once, from your laptop:

```bash
R=/opt/ltm-platform-env/repo
for who in alice bob carol; do
  ssh $V1 bash $R/deploy/aws/show-keys.sh $who | ssh $V2 bash $R/deploy/aws/claude-as.sh $who --setup
done
ssh $V2 bash $R/deploy/aws/claude-as.sh bob    # then: Claude Code as Bob
```

Each person's settings are rebuilt from `~/.claude/settings.json` on every
start, so phase 1's headers reach them too.

## 5. Phase 1: the integration, live

VM-1:

```bash
bash /opt/ltm-platform-env/repo/deploy/aws/1-integrate/server.sh 2>&1 | tee ~/phase1.log
```

What it does, in order:

1. **Installs the platform** (a venv and a systemd unit) next to core.
2. **Keycloak: additions only.** It adds client `memmachine-platform` (portal
   sign-in, plus a service account that can read users) and the `employee_id`
   claim on the existing `open-webui` client. That client's secret, redirect
   URIs and settings aren't touched (`KEYCLOAK_PHASE=integrate` in
   `tools/keycloak_setup.py`).
3. **Registers the gateway with the platform:**
   - this LiteLLM's service credential (`mm-svc-…`);
   - Open WebUI's app key, with its signing secret;
   - admin, alice and bob. Carol is left out on purpose: she shows self-enrolment.
   - It also creates a read-only LiteLLM key, so the platform can find people's
     own keys by itself.
4. **Upgrades the hook:**
   - **Preflight:** the platform answers; it accepts the credential; the new
     hook loads with the new settings in LiteLLM's own Python. If any of these
     fails, nothing has been changed yet.
   - **Backup:** `litellm/backup-<time>/` gets the hook, `litellm.env` and
     `config.yaml`.
   - **Swap:** the new `memmachine_hook.py`, plus `memmachine_adapters/`.
     `MEMMACHINE_BASE_URL` moves from core (:8090) to the platform (:8100), and
     `MEMMACHINE_API_KEY` becomes the service credential. `config.yaml` doesn't
     change: same callback, same `memmachine-` aliases.
   - **Restart:** if LiteLLM isn't healthy again, the script restores the
     backup on its own and stops.
   - **Check:** a chat with Alice's key, after which the platform must have
     linked her key to her.

Hand over Open WebUI's signing secret, from your laptop:

```bash
ssh $V1 cat $H/client-integrate.env | ssh $V2 "umask 077 && cat > $H/client-integrate.env"
```

VM-2:

```bash
bash /opt/ltm-platform-env/repo/deploy/aws/1-integrate/client.sh
```

What it changes:
- **`openwebui.env`:** forward a signed user token to LiteLLM, and name each
  user by their employee ID. It prints the diff and restarts Open WebUI.
  Open WebUI 0.11.3 reads these settings from the environment on every start
  (unlike most of its settings, which live in its database), so env + restart
  is enough.
- **`~/.claude/settings.json`:** Claude Code's hint headers, project and
  client headers.
- **Demo only:** it starts the forward for the portal (`ltm-forward@8100`),
  so your tunnel reaches it.

Both files are backed up as `*.before-memmachine`.

Optionally, in Open WebUI: Admin Settings > Connections > the LiteLLM
connection > Headers: `{"x-memmachine-client": "openwebui"}`. That setting
lives in Open WebUI's database, so it's a UI step.

Then, on VM-1: `bash /opt/ltm-platform-env/repo/deploy/aws/1-integrate/verify.sh`.
Those checks were written for SETUP.md's one-host stack, so a failure here may
come from the two-VM layout rather than a real fault.

## 6. Demo script

Use a separate browser profile per person. SSO cookies are per browser.

**Act 1: before (phase 0 running)**
1. Open WebUI as **Alice**, model `memmachine-glm-5.2`: *"Our Q1 launch is
   codenamed Bluefin."* Wait about 30 s.
2. Open WebUI as **Bob**: *"What is our Q1 codename?"* **Bob knows it.** The
   old hook names people by their key's email, and everyone in Open WebUI
   shares one app key, so everyone shares one memory.
3. Claude Code on VM-2 (Alice's own key), in an empty folder: *"Remember: my
   badge PIN hint is 'heron'."* Its memory is organized by her email, apart
   from Open WebUI's shared one.
4. Any key can read anyone's memory. On VM-1, with Bob's key, ask for Alice's:
   ```bash
   . /opt/ltm-platform-env/keys.env
   curl -s http://127.0.0.1:4011/v1/chat/completions -H "Authorization: Bearer $BOB_VIRTUAL_KEY" \
     -H 'Content-Type: application/json' -H 'x-memmachine-org: alice_corp_example' \
     -d '{"model":"memmachine-glm-5.2","messages":[{"role":"user","content":"What do you know about me?"}]}'
   ```
   The old hook trusts the `x-memmachine-org` header.

**Act 2: the integration (phase 1, live).** Run the two scripts. Point out
what each one changes, as described in section 5.

**Act 3: after**
5. Alice again (sign out and back in: her name is now E12345):
   *"Our Q1 launch is codenamed Bluefin."* Then Bob asks; **he doesn't know.**
6. Claude Code on VM-2, which uses Alice's own key, in an empty folder:
   *"What is our Q1 codename?"* It knows: same person, different frontend.
7. The curl from step 4 again: the header no longer matters. The platform
   decides the namespace from the verified person.
8. Portal as Alice (Memories tab): what was stored, and sharing a project with Bob.
9. Carol's first chat enrols her by herself; show it in the Admin tab.
10. Admin tab: turn Alice's memory off, and her next chat gets none. Show the
   audit log, then turn her memory back on.

**Act 4 (optional): what MemMachine alone takes.** Run phase 2 (below) and
show the timings table.

## 7. Phase 2: MemMachine core alone, on VM-2

Core needs a chat model and an embedding model. Give it a key on VM-1's
LiteLLM, limited to those two models:

```bash
# VM-1
. /opt/ltm-platform-env/.state/secrets.env
curl -s http://127.0.0.1:4011/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"key_alias":"standalone-memmachine","models":["glm-5.2","text-embedding-3-small"]}'
```

```bash
# VM-2
LITELLM_URL=http://<VM-1 private IP>:4011 LITELLM_KEY=<the key above> \
  bash /opt/ltm-platform-env/repo/deploy/aws/2-standalone/memmachine.sh
```

It installs Postgres + pgvector, Qdrant and core (git clone + `uv sync`),
writes `cfg.yml`, and starts two units. It then stores a fact and searches for
it, directly against core. At the end it prints how long each step took.

## 8. What changed on the customer's systems

| System | Change | Undo |
|---|---|---|
| Keycloak | **Added** client `memmachine-platform`, and the `employee_id` claim on `open-webui` | Delete both |
| LiteLLM | Hook file replaced, `memmachine_adapters/` added, 4 lines in its env (platform URL, service credential, timeout, Open WebUI claim); one new read-only LiteLLM user and key for the platform | `litellm/backup-*`, restart |
| Open WebUI | 4 env lines, one restart; optionally one connection header in the UI | `openwebui.env.before-memmachine` |
| Claude Code | Headers in `settings.json` | `settings.json.before-memmachine` |
| New | The platform (venv, sqlite registry, one unit), next to core, listening for VM-2 on :8100 | Stop and remove the unit |

Requests to models not named `memmachine-…` don't go through memory at all.

## 9. Before the day

- [ ] Do a full rehearsal on throwaway VMs. Done once on 2026-11-09: phase 0,
      phase 1 and verify (48/48) passed. Phase 2 has not run on a VM yet.
- [ ] Rebuild the demo VMs the day before, up to the end of phase 0. Phase 1
      is the live part. Don't run it ahead of time: its markers make it skip
      on the day.
- [ ] Check quota on DeepInfra and OpenAI.
- [ ] Keep the ssh commands, IPs and passwords in a note you can paste from.
- [ ] Keep a screen recording of a good run as a fallback.

## Caveats

- With LiteLLM and core on one host, core on 127.0.0.1 is no boundary:
  LiteLLM could still reach it. On separate hosts, core would listen only for
  the platform.
- Terminate both VMs afterwards, and rotate the provider keys if they were
  demo-only.
