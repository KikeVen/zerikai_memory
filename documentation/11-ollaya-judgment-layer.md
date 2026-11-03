# 11 — Ollaya Local Judgment Layer

> Ollaya/`winnow:e4b` is a **fully local, zero-cost alternative** to the Jev judgment layer.
> It is **off by default** and **mutually exclusive with Jev** — enabling both is a startup error.
> With `ENABLE_OLLAYA=false`, zerikai_memory behaves exactly as it did before this layer existed.

---

## 1. What Ollaya is

Ollaya is a local inference server (analogous to Ollama) that runs `winnow:e4b` — a compact
model purpose-built for **structured evidence judgment**. It exposes a TypeSafe-compatible
`/v1/decisions` endpoint for inference, so it accepts exactly the same question types (Noul,
Choice, Score) and returns the same probability shapes as Jev, without any network call
outside your machine. It also exposes `/api/decide`, used by `ollaya/warm.py` for model
warm-up and VRAM eviction requests.

**Get Ollaya and the model:**

- Download and install Ollaya: **<https://ollaya.dev>**
- Pull the model: `ollaya pull winnow:e4b` — model page: **<https://ollaya.dev/library/winnow>**

**Key characteristics:**

| Property | Value |
| --- | --- |
| Server | `ollaya.exe serve` on `127.0.0.1:11435` |
| Model | `winnow:e4b` (8.0 GB on disk, 6.2 GB VRAM) |
| Judgment latency | 2.35–3.31 s / 23 questions / 5 passages (measured, `results_020.json`) |
| Verdict agreement with Jev | 39/53 passages = 73.6% (same labelled set) |
| API cost | $0.00 — runs on your GPU |
| Requires internet | No |

---

## 2. What it does in zerikai_memory

Ollaya plugs into the **same post-retrieval judgment slot** as Jev (`assess_evidence` →
`compose_evidence`). When `ENABLE_OLLAYA=true`, after ChromaDB retrieval the pipeline sends
the candidate passages to the local Ollaya server instead of to TypeSafe's cloud API. The
rest of the pipeline — synthesis, the `Sources:` block, the `Assessment / Evidence / Guidance`
report — is identical.

### Question set

Ollaya uses a **reframed question set** derived from `probe_v2b.py` (not the cloud Jev set).
Two per-passage questions differ from the Jev wording:

| Question | Why it was reframed |
| --- | --- |
| `contradicts` (Score) | Uses query-directed wording — *"Does this passage contradict a claim in the user's **query**?"* — with level 0 = most conflicting. This is what made the false-premise hard case (q09) work. |
| `injection` (Choice) | Explicitly names the passage being judged via a `compare` reference. Without it, `winnow:e4b` assigned the same score to every passage. |

The remaining questions (`relevant`, `evidence`, `answerable`, `coverage`, `conflict`) carry
the same semantic meaning as Jev's but with string-form instructions, because Ollaya 0.12
rejects object-form instruction dicts.

### Normalization

Ollaya returns `contradicts` as a Score (not a Noul) and `injection` as a Choice. The
`ollaya/normalize.py` module maps these to the `noul`-shaped fields that `compose_evidence`
reads:

```
contradicts_noul  :=  P(level 0)                  — probability of "states the opposite"
injection_noul    :=  P("instruction_override")   — probability of prompt-injection intent
```

All other answers pass through unchanged.

### VRAM handoff

`winnow:e4b` (6.2 GB) and the Ollama synthesis model `ornith:9b` (5.6 GB) together exceed
8 GB and cannot both be resident. The pipeline manages this automatically based on the
synthesis backend (determined by `MEMORY_MODE` and query routing):

#### Cloud mode (`MEMORY_MODE=cloud` or escalated queries)

Synthesis uses DeepSeek API (no local Ollama needed):

1. **Before judging:** `ollaya/warm.py` pins `winnow:e4b` with `keep_alive=-1` so it stays
   loaded across queries without reloading (~6.7 s reload time saved per query).
2. **After synthesis:** winnow remains pinned (since synthesis doesn't use Ollama).

Benefit: Avoids winnow reload penalty while synthesis runs on cloud.

#### Local / Hybrid modes (Ollama synthesis)

Synthesis uses local Ollama `ornith:9b`:

1. **Before judging:** `ollaya/warm.py` pins `winnow:e4b` with `keep_alive=-1`.
2. **After synthesis:** release call passes `keep_alive=0` so winnow is evicted, freeing
   the card for ornith synthesis without both models resident.

Measured steady state with `keep_alive=-1 / 0` (local synthesis): **~1.0 s judging + ~3.3 s
synthesis reload ≈ 4.5 s per query**, with VRAM staying well under 7,900 MiB between queries.

---

## 3. How to activate it

**Manual setup (one-time prerequisites):**

- Ollaya installed: download from **<https://ollaya.dev>**
- Model pulled: `ollaya pull winnow:e4b` (model page: <https://ollaya.dev/library/winnow>)
- Server running: `ollaya serve` (listens on `127.0.0.1:11435`)

**Verify the server is ready:**

```bash
curl http://127.0.0.1:11435/           # → "Ollaya is running"
curl http://127.0.0.1:11435/api/ps     # → model list (winnow:e4b when warm)
```

### Auto-start the server

Since zerikai_memory queries the Ollaya server at startup, you need `ollaya serve` running *before* your MCP server starts. Choose the method for your platform:

#### Linux

The `install.sh` script sets up a **systemd service** automatically (if run with `sudo`):

```bash
# Already configured after installation
systemctl enable ollaya      # Start at boot
systemctl start ollaya       # Start now
systemctl status ollaya      # Verify it's running
```

If you need to set it up manually:

```bash
sudo tee /etc/systemd/system/ollaya.service > /dev/null <<EOF
[Unit]
Description=Ollaya Decision Model Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/ollaya serve
Environment="OLLAYA_HOST=127.0.0.1:11435"
Restart=on-failure
User=ollaya
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ollaya
sudo systemctl start ollaya
```

#### macOS

**Option 1: Using the Ollaya desktop app (easiest)**

1. Open the Ollaya app
2. Click the menu bar icon → **"Open at Login"** toggle

This writes a LaunchAgent plist that starts the server at each login.

**Option 2: Manual LaunchAgent setup**

Create a plist file at `~/Library/LaunchAgents/dev.ollaya.app.plist`:

```bash
mkdir -p ~/Library/LaunchAgents

cat > ~/Library/LaunchAgents/dev.ollaya.app.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.ollaya.app</string>
  <key>ProgramArguments</key><array><string>/usr/local/bin/ollaya</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
EOF

# Load it
launchctl load ~/Library/LaunchAgents/dev.ollaya.app.plist
launchctl start dev.ollaya.app
```

Verify it started:

```bash
launchctl list | grep dev.ollaya
```

#### Windows

Use **Task Scheduler** to run `ollaya serve` at logon. Run this once in an **admin PowerShell**:

```powershell
$action  = New-ScheduledTaskAction -Execute "ollaya" -Argument "serve"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Ollaya" -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited
```

**Notes:**

- `ollaya` must be on your `PATH`, or replace with the full path: `"$env:LOCALAPPDATA\Ollaya\ollaya.exe"`
- `RunLevel Limited` runs it as your user (not system) — correct for the loopback server
- `Hidden` keeps the task invisible; combined with Ollaya's `CREATE_NO_WINDOW` flag, you'll see no console window at login
- Verify after reboot: `curl http://127.0.0.1:11435/`

**Troubleshooting:** If the server doesn't start after reboot, check the Task Scheduler event log or the Ollaya log:

```powershell
Get-ScheduledTask -TaskName "Ollaya" | Get-ScheduledTaskInfo
Get-Content "$HOME\.ollaya\logs\server.log"
```

### Integration with zerikai_memory

Once the server is running, the default `.env` values work seamlessly:

```
OLLAYA_BASE_URL=http://127.0.0.1:11435
OLLAYA_API_KEY=local
```

The server trusts local callers by default at `127.0.0.1:11435`. If you set a custom `OLLAYA_API_KEY` in your `.env`, ensure the same key is set in the environment where the Ollaya server runs (or use the startup scripts above to inject it).

**In your `.env`, set:**

```
ENABLE_OLLAYA=true
```

`ENABLE_JEV` must be `false` (the default). Setting both to `true` is a startup error.

**Restart the MCP server** — `main.py` reads `.env` at startup and pins the model on first
query.

Ollaya runs in any `MEMORY_MODE` (`cloud`, `hybrid`, `local`) — it is always local and never
blocked by the local-mode guard that applies to Jev.

---

## 4. Guarantees

- **Off by default.** `ENABLE_OLLAYA=false` → byte-identical to the pre-Ollaya pipeline.
- **Mutually exclusive with Jev.** Both `ENABLE_JEV=true` and `ENABLE_OLLAYA=true` → the
  process refuses to start with a clear error message naming both flags.
- **Fail-open.** Any Ollaya error (`OllayaUnavailable`) — connection refused, timeout, HTTP
  422, missing `answers` — falls through to lexical re-ranking. The answer still comes back.
- **No internet required.** The judgment call never leaves the machine.
- **Works in local mode.** No `OLLAYA_ALLOW_IN_LOCAL` flag needed; Ollaya is itself local.
- **Injection defense.** Passages whose `injection` probability exceeds `OLLAYA_INJECTION_MAX`
  are dropped before the LLM sees them.

---

## 5. Understanding the gates

The evidence assessment keeps a passage when:

```
relevant >= OLLAYA_RELEVANCE_MIN  AND  evidence >= OLLAYA_EVIDENCE_MIN  AND  injection < OLLAYA_INJECTION_MAX
```

and flags it as conflicting when `contradicts >= OLLAYA_CONTRA_MIN`. The whole result is
`no_answer` when nothing is kept, or when `answerable < OLLAYA_ANSWERABILITY_MIN`.

**Threshold calibration:** Ollaya's probabilities sit on a different scale from Jev's. The
`JEV_*` defaults are not valid starting points. The `.env.example` ships calibrated values
derived from a 10-case / 23-question sweep against `zerikai_memory` on 2026-10-31:

```
OLLAYA_RELEVANCE_MIN=0.30   # lowest threshold where all expected passages survive
OLLAYA_EVIDENCE_MIN=0.20    # allows contradicting passages through (correct — they should be kept)
OLLAYA_INJECTION_MAX=0.50   # 1 labelled decoy; held at Jev default
OLLAYA_CONTRA_MIN=0.50      # 1 labelled decoy; held at Jev default
OLLAYA_ANSWERABILITY_MIN=0.40
```

**Re-calibrate** before deploying on a different workspace or after switching models. See
[documentation/12-ollaya-threshold-calibration.md](12-ollaya-threshold-calibration.md) for the procedure.

**One honest limitation:** `OLLAYA_MAX_PASSAGES=2` caps the judgment window at 2 passages.
With `FETCH_CAP=5` the pipeline retrieves 5 candidates but only the top 2 (by L2 distance)
are judged. Raising `OLLAYA_MAX_PASSAGES` increases judgment quality and latency linearly —
23 questions at ~1 s vs. 11 questions at ~0.9 s. The cap exists because the calibration data
uses 2 passages; changing it before recalibrating will skew the threshold values.

---

## 6. Configuration reference (`.env`)

| Key | Ship value | What it does |
| --- | --- | --- |
| `ENABLE_OLLAYA` | `false` | Master switch. `false` = byte-identical pre-Ollaya behavior. |
| `OLLAYA_BASE_URL` | `http://127.0.0.1:11435` | Ollaya server address. |
| `OLLAYA_MODEL` | `winnow:e4b` | Model tag passed to every judgment request. |
| `OLLAYA_API_KEY` | `local` | Passed as the API key; Ollaya does not enforce it but the SDK requires one. |
| `OLLAYA_TIMEOUT_SECONDS` | `120` | Request timeout. 23-question batches at 5 passages take 2–3 s; 120 s is intentionally generous. On timeout, `OllayaUnavailable` is raised and the pipeline fails open. |
| `OLLAYA_MAX_PASSAGES` | `2` | Judgment window cap. Raise only after recalibrating thresholds. |
| `OLLAYA_RELEVANCE_MIN` | `0.30` | Minimum passage relevance to keep. Calibrated 2026-10-31. |
| `OLLAYA_EVIDENCE_MIN` | `0.20` | Minimum passage evidence to keep. Calibrated 2026-10-31. |
| `OLLAYA_INJECTION_MAX` | `0.50` | At or above this score the passage is dropped. |
| `OLLAYA_CONTRA_MIN` | `0.50` | At or above this score the passage is flagged as conflicting. |
| `OLLAYA_ANSWERABILITY_MIN` | `0.40` | Minimum whole-set answerability; below → `no_answer`. |

### Tuning notes

- **Too many "no answer" results?** Lower `OLLAYA_RELEVANCE_MIN` / `OLLAYA_EVIDENCE_MIN` (toward `0`).
- **Good passages dropped?** Check `OLLAYA_EVIDENCE_MIN` first — a contradicting passage
  that correctly disproves a false query premise will score low on `evidence` by design; lower
  `OLLAYA_EVIDENCE_MIN` to keep it.
- **Ollaya gets slow on the second query?** The Ollama synthesis model is holding the card.
  Confirm `keep_alive=0` is being sent in synthesis calls (`main.py`); check with `nvidia-smi`
  after a query.
- **OllayaUnavailable every time?** Check `ollaya serve` is running, `OLLAYA_BASE_URL` is
  correct, and the firewall is not blocking `127.0.0.1:11435`.

---

## 7. VRAM requirements

> **GPU recommendation:** 16 GB VRAM is recommended. `winnow:e4b` can run on 8 GB but the card will be tight — see the table below.

| State | VRAM |
| --- | --- |
| Both unloaded | 0 MiB |
| `winnow:e4b` only | ~6,200 MiB |
| `ornith:9b` only (Ollama synthesis) | ~5,600 MiB |
| Both resident | ~7,984 MiB of 8,192 — fits, but Ollaya's eval time degrades 3–9× |

The pipeline avoids the "both resident" state via the `keep_alive=0` release on synthesis.
If VRAM sits persistently near the 8 GB ceiling and judgment latency spikes, another process
has taken GPU memory — check with `nvidia-smi`.

---

## 8. Hardware Limitations & Failover Behavior

### Context Overflow on Long-Form Prose

`winnow:e4b` is optimized for **code judgment** — it was trained on short code snippets (typically 200–500 tokens). On **8 GB VRAM**, the model leaves minimal headroom for state (query + passages combined).

When passages are **long-form prose** (markdown documents, legal text, essays, etc. — 1000+ tokens each), the combined state can exceed winnow's context window (~2048 tokens). When this happens:

1. **Ollaya returns HTTP 422** (`part of state was dropped to fit the context`)
2. **zerikai_memory logs a warning** and catches the `OllayaUnavailable` exception
3. **Automatic failover:** The pipeline skips winnow judgment and falls back to **lexical re-ranking** (keyword overlap + L2 distance)
4. **Answer still comes back** — the fail-open guarantee holds

**Example from logs:**

```
21:43:22 — winnow drops state to fit context
21:43:22 — OllayaUnavailable; proceeding fail-open
21:43:22 — lexical re-rank applied
```

### When Context Overflow Occurs

- **Code-heavy workspaces**: ✅ Works fine at `OLLAYA_MAX_PASSAGES=5`
- **Markdown-heavy workspaces** (legal docs, long-form prose): ⚠️ Context overflow at even `OLLAYA_MAX_PASSAGES=2`

### Workaround (Current 8 GB Hardware)

Reduce `OLLAYA_MAX_PASSAGES` to 1 and accept lexical re-ranking for markdown queries. The answer quality degrades gracefully — you lose structured judgment but keep keyword-based passage filtering.

Alternatively, disable Ollaya for markdown-heavy workspaces:

```
ENABLE_OLLAYA=false
```

This reverts to Jev (cloud) or pure lexical re-ranking, depending on your `MEMORY_MODE`.

### Recommended Fix: Upgrade GPU & Use winnow:12b

The permanent solution for supporting both code and prose workspaces:

1. **Upgrade GPU to 16+ GB VRAM** (RTX 4070 Super, RTX 4080, A100, H100, etc.)
2. **Pull `winnow:12b`**: `ollaya pull winnow:12b` (larger model, trained for broader judgment tasks)
3. **Update `.env`**: `OLLAYA_MODEL=winnow:12b`
4. **Re-calibrate thresholds** (see [documentation/12-ollaya-threshold-calibration.md](12-ollaya-threshold-calibration.md))
5. **Result:** winnow handles both code and markdown without context overflow

**Estimated latency (winnow:12b on 24GB GPU):**

- Markdown passages: ~2–3s per query (vs. current 0–0.5s via lexical-only failover)
- Code passages: ~1.5–2s per query (slight increase from winnow:e4b's 1–1.3s)

**Trade-off:** Slightly slower per-query, but dramatically better judgment quality across document types and no automatic degradation to lexical re-ranking.

---

## 9. References

- Ollaya — download and install: <https://ollaya.dev>
- `winnow:e4b` model page: <https://ollaya.dev/library/winnow>
- Jev judgment layer (cloud alternative): [documentation/10-jev-judgment-layer.md](10-jev-judgment-layer.md)
