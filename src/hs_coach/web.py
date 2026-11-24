"""FastAPI web application: drag-and-drop Power.log trial, SaaS auth, quotas, and habit dashboard."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from hs_coach.auth import (
    GUEST_COOKIE_NAME,
    PASSWORD_RESET_TTL_SECONDS,
    FAST_MODELS,
    PLAN_CONFIGS,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_guest_token,
    create_session_token,
    generate_password_reset_token,
    generate_upload_token,
    get_plan_allowed_models,
    hash_password,
    hash_reset_token,
    send_password_reset_email,
    verify_guest_token,
    verify_password,
    verify_session_token_details,
)
from hs_coach.billing import (
    TransientStripeError,
    create_billing_portal_session,
    create_checkout_session,
    handle_stripe_event,
    resolve_stripe_config,
    verify_stripe_signature,
)
from hs_coach.cards import CardDB, canonicalize_replay
from hs_coach.coach import coach_game, resolve_coach_config
from hs_coach.config import AppConfig, is_loopback_host, load_app_config
from hs_coach.parser import parse_power_log
from hs_coach.render import render_html_report
from hs_coach.schemas import GameReplay, validate_completed_replay
from hs_coach.security import (
    BodyTooLargeError,
    CoachJobManager,
    MaxBodySizeMiddleware,
    QueueFullError,
    RateLimiter,
    get_client_ip,
    is_secure_request,
)
from hs_coach.storage import GameStore

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB limit
_HOSTED_UNTRUSTED_COACH_MSG = (
    "Hosted coaching is disabled for unverified client-submitted replays. "
    "Trusted post-game completion evidence is required; use local mode for self-parsed Power.log coaching."
)


class CoachRequest(BaseModel):
    provider: str | None = None
    model: str | None = None


class AuthCredentials(BaseModel):
    email: str
    password: str


class PasswordResetRequest(BaseModel):
    email: str


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str


class ShareToggleRequest(BaseModel):
    enabled: bool = True


def _legal_page_html(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} — HS-Coach</title>
  <style>
    body {{
      margin: 0;
      padding: 28px 20px;
      background: #0f141f;
      color: #e6ecf5;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      line-height: 1.6;
    }}
    .wrap {{
      max-width: 820px;
      margin: 0 auto;
      background: #171f2e;
      border: 1px solid #2c3952;
      border-radius: 12px;
      padding: 24px 28px;
    }}
    h1 {{ margin-top: 0; color: #f0b949; font-size: 1.5rem; }}
    h2 {{ color: #e6ecf5; font-size: 1.08rem; margin-top: 20px; }}
    a {{ color: #38bdf8; text-decoration: none; }}
    p, li {{ color: #cbd5e1; }}
  </style>
</head>
<body>
  <div class="wrap">
    <p><a href="/">&larr; Back to HS-Coach</a></p>
    <h1>{title}</h1>
    {body_html}
  </div>
</body>
</html>
"""


_PRIVACY_HTML = _legal_page_html(
    "Privacy Policy",
    """
    <p>HS-Coach is designed as a 100% post-game Hearthstone coaching tool with strict local privacy hygiene.</p>
    <h2>1. Data Minimisation &amp; Local Anonymization</h2>
    <ul>
      <li>Raw <code>Power.log</code> files stay on your device in hosted deployments. Local CLI/Desktop tools parse logs locally and strip BattleTags (e.g. <code>Player#1234</code>) and account identifiers before anything is uploaded.</li>
      <li>Only anonymized match telemetry (class matchup, card entities, turn snapshots, deterministic pre-checks, and terminal <code>PLAYSTATE</code> outcomes labeled as <code>you</code> and <code>opponent</code>) is stored and sent to configured LLM providers for post-game coaching.</li>
    </ul>
    <h2>2. Accounts &amp; Billing Data</h2>
    <ul>
      <li>When you create an account, we store your email address, a salted PBKDF2-HMAC-SHA256 password hash, an upload Bearer token, and server-side session metadata.</li>
      <li>Paid Pro subscriptions are processed through Stripe. We do not store raw payment card details; we retain only Stripe customer/subscription identifiers needed to manage entitlement and Billing Portal access.</li>
    </ul>
    <h2>3. Cookies &amp; Security</h2>
    <p>HS-Coach uses HttpOnly signed session cookies strictly for authentication and isolated guest storage. No third-party ad trackers are embedded.</p>
    <h2>4. Deletion &amp; Contact</h2>
    <p>To request account or match history deletion, or for any privacy question, email <a href="mailto:hscoach.support@gmail.com">hscoach.support@gmail.com</a>. You can cancel an active subscription anytime via Manage Billing.</p>
    """,
)

_TERMS_HTML = _legal_page_html(
    "Terms of Service",
    """
    <p>By accessing or using HS-Coach, you agree to the following terms.</p>
    <h2>1. Post-Game Only &amp; Game Compliance</h2>
    <ul>
      <li>HS-Coach strictly analyzes completed Hearthstone matches after a terminal match outcome (<code>WON</code>, <code>LOST</code>, or <code>TIED</code>).</li>
      <li>Do not attempt to modify or connect HS-Coach to provide live mid-match advice, in-game overlays, memory inspection, or client hooks.</li>
      <li>HS-Coach is an independent tool and is not affiliated with, endorsed by, or sponsored by Blizzard Entertainment, Inc.</li>
    </ul>
    <h2>2. Plans, Quotas &amp; Subscriptions</h2>
    <ul>
      <li><strong>Free:</strong> 5 coached games per month at no cost.</li>
      <li><strong>Pro:</strong> $9.00 USD per month for 200 coached games per month, including 30 deep reviews per month with stronger models. Payments are processed by Stripe. Pro renews automatically each month until you cancel.</li>
      <li>Automated abuse, quota circumvention, or excessive traffic is subject to rate limiting or account suspension.</li>
    </ul>
    <h2>3. Cancellation &amp; Refunds</h2>
    <ul>
      <li>You can cancel anytime from <strong>Manage Billing</strong> on the dashboard. You keep Pro until the end of the period you already paid for, and you will not be charged again.</li>
      <li>We don't give partial refunds for unused time. If something went wrong (for example a duplicate charge, or you couldn't use Pro because of a problem on our side), email us within 7 days of the charge and we'll make it right, including a full refund where appropriate.</li>
    </ul>
    <h2>4. Disclaimer</h2>
    <p>AI-generated coaching reports are provided "as is" for educational analysis without warranty of accuracy or uninterrupted availability.</p>
    <h2>5. Contact</h2>
    <p>Questions, billing problems or refund requests: <a href="mailto:hscoach.support@gmail.com">hscoach.support@gmail.com</a>.</p>
    """,
)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HS-Coach — Post-Game Standard AI Coach</title>
  <style>
    :root {
      --bg: #0f141f;
      --surface: #171f2e;
      --surface-alt: #1e283b;
      --border: #2c3952;
      --text: #e6ecf5;
      --muted: #9aa7bd;
      --accent: #f0b949;
      --win: #27c07d;
      --loss: #f25f5c;
      --info: #38bdf8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 24px;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      line-height: 1.5;
    }
    .container { max-width: 1180px; margin: 0 auto; display: grid; gap: 20px; }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
    }
    .topbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      border-left: 4px solid var(--accent);
    }
    h1 { margin: 0; font-size: 1.4rem; }
    h2 { margin: 0 0 12px 0; font-size: 1.08rem; color: var(--accent); }
    .muted { color: var(--muted); font-size: 0.9rem; }
    .pill {
      display: inline-block;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      background: var(--surface-alt);
      border: 1px solid var(--border);
    }
    .pill.privacy { color: var(--win); border-color: rgba(39, 192, 125, 0.45); }
    .pill.win { color: var(--win); border-color: rgba(39, 192, 125, 0.45); }
    .pill.loss { color: var(--loss); border-color: rgba(242, 95, 92, 0.45); }
    .pill.tie { color: var(--muted); }
    .pill.plan { color: var(--accent); border-color: rgba(240, 185, 73, 0.45); }
    .auth-bar {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
    }
    .auth-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    input[type="email"], input[type="password"], input[type="text"] {
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 0.86rem;
    }
    .drop-zone {
      border: 2px dashed var(--border);
      border-radius: 12px;
      padding: 28px 20px;
      text-align: center;
      background: var(--surface-alt);
      transition: border-color 0.15s ease, background 0.15s ease;
      cursor: pointer;
    }
    .drop-zone.dragover {
      border-color: var(--accent);
      background: #233049;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-top: 12px;
    }
    select, button {
      background: var(--surface-alt);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 7px 12px;
      font-size: 0.88rem;
    }
    button { cursor: pointer; font-weight: 600; }
    button.primary {
      background: var(--accent);
      color: #111827;
      border-color: var(--accent);
    }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    .kpis {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
    }
    .kpi {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
    }
    .kpi-label { font-size: 0.75rem; text-transform: uppercase; color: var(--muted); }
    .kpi-value { font-size: 1.25rem; font-weight: 700; margin-top: 4px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; }
    a.btn-link {
      display: inline-block;
      padding: 5px 10px;
      border-radius: 7px;
      border: 1px solid var(--info);
      color: var(--info);
      text-decoration: none;
      font-size: 0.84rem;
      font-weight: 600;
    }
    a.btn-link:hover { background: rgba(56, 189, 248, 0.12); }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
    .status-msg { margin-top: 10px; font-size: 0.9rem; color: var(--info); min-height: 1.2em; }
    .status-msg.error { color: var(--loss); }
    code { background: var(--bg); padding: 2px 6px; border-radius: 5px; font-size: 0.82rem; }
  </style>
</head>
<body>
  <div class="container">
    <section class="card topbar">
      <div>
        <h1>HS-Coach · Post-Game Standard AI Coach</h1>
        <div class="muted">Review your finished Standard games: mulligans, turning points and missed lethals, after the match is over.</div>
      </div>
      <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center;">
        <span class="pill privacy">100% Post-Game · Names &amp; BattleTags Stripped On Your PC</span>
      </div>
    </section>

    <section id="uploaderCard" class="card">
      <h2>Get the Windows Uploader</h2>
      <div class="muted" style="margin-bottom: 10px;">
        After each finished game, <code>HS-Coach-Uploader.exe</code> reads your local <code>Power.log</code>, strips player names and BattleTags on your PC, and uploads only the anonymized replay. Raw logs never leave your machine.
      </div>
      <a id="uploaderDownload" class="btn-link" href="https://github.com/GreatLDane/hs-coach/releases/latest" target="_blank" rel="noopener noreferrer">Download HS-Coach-Uploader.exe (Windows)</a>
      <ol class="muted" style="margin: 10px 0 0 18px; padding: 0; font-size: 0.88rem;">
        <li>Register free below and click <strong>Copy Upload Token</strong>.</li>
        <li>Open the uploader, paste the token, click <strong>Enable Power.log</strong>, then <strong>Save Settings</strong>. Restart Hearthstone if it was open.</li>
        <li>Play a Standard game to the end. It appears here; click <strong>Coach</strong>.</li>
      </ol>
      <div class="muted" style="margin-top: 8px; font-size: 0.8rem;">
        The uploader is not code-signed yet, so Windows may show "Windows protected your PC": click More info &rarr; Run anyway. Each release lists a <code>.sha256</code> checksum you can verify first.
      </div>
    </section>

    <section id="onboardingCard" class="card">
      <h2>How HS-Coach Works · Free vs Pro</h2>
      <div class="grid-2" style="grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));">
        <div><strong>1. Strip Names Locally</strong><div class="muted">Play Standard Constructed. When the game ends, the Windows uploader removes names and BattleTags before anything leaves your PC.</div></div>
        <div><strong>2. Deterministic Math</strong><div class="muted">Exact Python checks find missed lethals, floated mana, burned cards and unused attacks first, so the AI review is grounded in real numbers.</div></div>
        <div><strong>3. Grounded AI Review</strong><div class="muted"><strong>Free:</strong> 5 coached games/mo (Flash) + upload token · <strong>Pro:</strong> $9/month for 200 games/mo, including 30 deep reviews with stronger models (Gemini 3.1 Pro, Claude Sonnet). Cancel anytime.</div></div>
      </div>
    </section>

    <section class="card">
      <div id="accountBar" class="auth-bar">
        <div id="accountInfo" class="muted">Guest Trial Mode (local storage) — Sign in or register for personal quota &amp; watcher token.</div>
        <div id="authControls" class="auth-form">
          <input id="authEmail" type="email" placeholder="email@example.com" />
          <input id="authPassword" type="password" placeholder="Password (min 6 chars)" />
          <button id="loginBtn" type="button">Sign In</button>
          <button id="registerBtn" class="primary" type="button">Register (Free)</button>
          <button id="forgotBtn" type="button">Forgot Password?</button>
        </div>
      </div>
      <div id="resetBox" class="auth-form" style="margin-top: 10px; display: none;">
        <input id="resetEmail" type="email" placeholder="Email for reset link" />
        <button id="sendResetBtn" type="button">Send Reset Link</button>
        <input id="resetToken" type="text" placeholder="Reset token" />
        <input id="resetNewPassword" type="password" placeholder="New password (min 6 chars)" />
        <button id="confirmResetBtn" class="primary" type="button">Update Password</button>
      </div>
      <div id="watcherBox" class="muted" style="margin-top: 10px; display: none;">
        Upload token for HS-Coach-Uploader.exe:
        <button id="copyTokenBtn" type="button" class="primary" style="margin-left: 8px; padding: 4px 9px; font-size: 0.8rem;">Copy Upload Token</button>
        <button id="rotateTokenBtn" type="button" style="margin-left: 8px; padding: 4px 9px; font-size: 0.8rem;">Rotate Token</button>
        <div style="margin-top: 6px; font-size: 0.8rem;">Advanced (CLI instead of the .exe): <code id="watchCmd"></code></div>
      </div>
      <div id="authStatus" class="status-msg" style="margin-top: 6px;"></div>
    </section>

    <section class="card">
      <h2 id="uploadHeading">Upload Power.log</h2>
      <div id="dropZone" class="drop-zone">
        <div id="dropTitle" style="font-weight: 600; font-size: 1.02rem;">Drag &amp; drop <code>Power.log</code> here, or click to select</div>
        <div id="dropSub" class="muted" style="margin-top: 4px;">Typical Windows path: <code>D:\\Hearthstone\\Logs\\Hearthstone_*\\Power.log</code> (max 50 MB)</div>
        <input id="fileInput" type="file" accept=".log,.txt" style="display: none;" />
      </div>
      <div class="controls">
        <label class="muted">
          Coaching Model:
          <select id="modelSelect">
            <option value="">Fast (default)</option>
            <option value="google/gemini-3.1-pro-preview">Deep review · Gemini 3.1 Pro (Pro, uses 1 of 30/mo)</option>
            <option value="anthropic/claude-sonnet-4.5">Deep review · Claude Sonnet 4.5 (Pro, uses 1 of 30/mo)</option>
          </select>
        </label>
        <button id="uploadBtn" class="primary" type="button">Choose Power.log</button>
      </div>
      <div id="uploadStatus" class="status-msg"></div>
    </section>

    <section class="kpis" id="kpiStrip">
      <div class="kpi"><div class="kpi-label">Games Stored</div><div class="kpi-value" id="kpiGames">0</div></div>
      <div class="kpi"><div class="kpi-label">Win Rate</div><div class="kpi-value" id="kpiWr">0%</div></div>
      <div class="kpi"><div class="kpi-label">Coached</div><div class="kpi-value" id="kpiCoached">0</div></div>
      <div class="kpi"><div class="kpi-label">Avg Mana Floated</div><div class="kpi-value" id="kpiFloat">0</div></div>
      <div class="kpi"><div class="kpi-label">Missed Lethals</div><div class="kpi-value" id="kpiLethal">0</div></div>
      <div class="kpi"><div class="kpi-label">Total API Spend</div><div class="kpi-value" id="kpiCost">$0.00</div></div>
    </section>

    <section class="card">
      <h2>Match History</h2>
      <div style="overflow-x: auto;">
        <table>
          <thead>
            <tr>
              <th>Matchup</th>
              <th>Result</th>
              <th>Turns</th>
              <th>Pre-Checks</th>
              <th>Grade / Archetypes</th>
              <th>Cost</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="gamesBody"></tbody>
        </table>
      </div>
    </section>

    <section class="grid-2">
      <div class="card">
        <h2>Recurring Mistake Habits</h2>
        <div id="mistakesBox" class="muted">Coach games to populate recurring habit tags.</div>
      </div>
      <div class="card">
        <h2>Matchup Winrates &amp; Strengths</h2>
        <div id="matchupsBox" class="muted">Upload games to see matchup stats.</div>
      </div>
    </section>
  </div>

  <script>
    const dropZone = document.getElementById("dropZone");
    const fileInput = document.getElementById("fileInput");
    const uploadBtn = document.getElementById("uploadBtn");
    const uploadStatus = document.getElementById("uploadStatus");
    const authStatus = document.getElementById("authStatus");
    const modelSelect = document.getElementById("modelSelect");

    function setStatus(msg, isError = false) {
      uploadStatus.textContent = msg;
      uploadStatus.className = isError ? "status-msg error" : "status-msg";
    }

    function setAuthStatus(msg, isError = false) {
      authStatus.textContent = msg;
      authStatus.className = isError ? "status-msg error" : "status-msg";
    }

    function el(tag, text, className) {
      const node = document.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = String(text);
      if (className) node.className = className;
      return node;
    }

    function formatHabitTag(tag) {
      const parts = String(tag || "").trim().split(/[\\s_-]+/).filter(Boolean);
      if (!parts.length) return String(tag || "");
      return parts.map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(" ");
    }

    let rawUploadEnabled = true;
    let allowHostedClientCoaching = false;
    let authState = { authenticated: false, guests_enabled: false };
    let currentUploadToken = "";
    const copyTokenBtn = document.getElementById("copyTokenBtn");

    async function refreshAccount() {
      const res = await fetch("/api/auth/me");
      const data = await res.json();
      rawUploadEnabled = Boolean(data.raw_upload_enabled);
      allowHostedClientCoaching = Boolean(data.allow_hosted_client_coaching);
      authState = { authenticated: Boolean(data.authenticated), guests_enabled: Boolean(data.guests_enabled) };

      const uploadHeading = document.getElementById("uploadHeading");
      const dropTitle = document.getElementById("dropTitle");
      const dropSub = document.getElementById("dropSub");
      if (rawUploadEnabled) {
        uploadHeading.textContent = "Upload Power.log";
        dropTitle.textContent = "Drag & drop Power.log here, or click to select";
        dropSub.textContent = "Local loopback mode: parses locally on this machine and deletes temporary log.";
        fileInput.accept = ".log,.txt";
        uploadBtn.textContent = "Choose Power.log";
      } else {
        uploadHeading.textContent = "Manual Import (Advanced)";
        dropTitle.textContent = "Most players don't need this: the Windows uploader sends finished games automatically.";
        dropSub.textContent = "Advanced: drop an anonymized .replay.json made by the hs-coach CLI. Raw Power.log files are never accepted here.";
        fileInput.accept = ".json";
        uploadBtn.textContent = "Choose .replay.json";
      }

      const info = document.getElementById("accountInfo");
      const ctrl = document.getElementById("authControls");
      const watcherBox = document.getElementById("watcherBox");
      const watchCmd = document.getElementById("watchCmd");
      const onboardingCard = document.getElementById("onboardingCard");
      if (onboardingCard) {
        onboardingCard.style.display = data.authenticated ? "none" : "block";
      }

      info.innerHTML = "";
      if (data.authenticated && data.user) {
        const u = data.user;
        const q = data.quota;
        info.appendChild(el("strong", u.email));
        const deepPart = q.deep_limit ? ` · ${q.deep_used}/${q.deep_limit} deep` : "";
        const badge = el("span", `${u.plan.toUpperCase()} · ${q.used_this_month}/${q.monthly_limit} coached this month${deepPart}`, "pill plan");
        badge.style.marginLeft = "10px";
        info.appendChild(badge);

        ctrl.innerHTML = "";
        if (u.plan !== "pro") {
          const upBtn = el("button", "Upgrade to Pro", "primary");
          upBtn.type = "button";
          upBtn.addEventListener("click", async () => {
            const r = await fetch("/api/billing/checkout", { method: "POST" });
            const d = await r.json();
            if (!r.ok) {
              setAuthStatus(d.detail || "Unable to start checkout.", true);
              return;
            }
            window.location.href = d.checkout_url;
          });
          ctrl.appendChild(upBtn);
        } else {
          const portalBtn = el("button", "Manage Billing");
          portalBtn.type = "button";
          portalBtn.addEventListener("click", async () => {
            const r = await fetch("/api/billing/portal", { method: "POST" });
            const d = await r.json();
            if (!r.ok) {
              setAuthStatus(d.detail || "Unable to open billing portal.", true);
              return;
            }
            window.location.href = d.portal_url;
          });
          ctrl.appendChild(portalBtn);
        }

        const logoutBtn = el("button", "Sign Out");
        logoutBtn.type = "button";
        logoutBtn.addEventListener("click", async () => {
          await fetch("/api/auth/logout", { method: "POST" });
          setAuthStatus("Signed out.");
          window.location.reload();
        });
        ctrl.appendChild(logoutBtn);

        watcherBox.style.display = "block";
        currentUploadToken = u.upload_token || "";
        watchCmd.textContent = `hs-coach watch --server ${window.location.origin} --token ${currentUploadToken}`;
      } else {
        info.textContent = data.mode === "hosted"
          ? (data.guests_enabled
              ? "Isolated Guest Session — Sign in or register for personal quota & auto-uploader token."
              : "Sign in or register free to upload anonymized replays and view coaching history.")
          : "Local Loopback Mode — Register free or sign in for personal history & watcher token.";
        currentUploadToken = "";
        watcherBox.style.display = "none";
      }
    }

    async function submitAuth(endpoint) {
      const emailEl = document.getElementById("authEmail");
      const passEl = document.getElementById("authPassword");
      if (!emailEl || !passEl) return;
      try {
        const res = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: emailEl.value, password: passEl.value }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Authentication failed");
        setAuthStatus(`Signed in as ${data.user.email} (${data.user.plan.toUpperCase()} plan).`);
        await refreshAll();
      } catch (err) {
        setAuthStatus(err.message || String(err), true);
      }
    }

    document.getElementById("loginBtn")?.addEventListener("click", () => submitAuth("/api/auth/login"));
    document.getElementById("registerBtn")?.addEventListener("click", () => submitAuth("/api/auth/register"));
    copyTokenBtn?.addEventListener("click", async () => {
      if (!currentUploadToken) return;
      try {
        await navigator.clipboard.writeText(currentUploadToken);
        setAuthStatus("Upload token copied! Paste it into HS-Coach-Uploader.exe.");
      } catch (_err) {
        window.prompt("Copy your Upload Token:", currentUploadToken);
      }
    });
    document.getElementById("rotateTokenBtn")?.addEventListener("click", async () => {
      const res = await fetch("/api/auth/rotate-token", { method: "POST" });
      if (res.ok) {
        setAuthStatus("Upload token rotated.");
        await refreshAccount();
      }
    });

    async function uploadFile(file) {
      if (!file) return;
      try {
        if (!rawUploadEnabled) {
          setStatus(`Importing anonymized replay ${file.name}...`);
          const text = await file.text();
          const res = await fetch("/api/replays", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: text,
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || "Replay import failed");
          setStatus(`Imported game ${data.game_id} (${data.is_new ? "new" : "updated"}).`);
          await refreshDashboard();
          return;
        }
        setStatus(`Parsing ${file.name} (${(file.size / 1048576).toFixed(2)} MB)...`);
        const fd = new FormData();
        fd.append("file", file);
        const res = await fetch("/api/upload", { method: "POST", body: fd });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Upload failed");
        setStatus(
          `Found ${data.total_games_found} game(s): imported ${data.new_games} new, ` +
          `${data.duplicate_games} duplicate, skipped ${data.skipped_incomplete} incomplete / ` +
          `${data.skipped_non_standard} non-Standard.`
        );
        await refreshDashboard();
      } catch (err) {
        setStatus(err.message || String(err), true);
      }
    }

    dropZone.addEventListener("click", () => fileInput.click());
    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => uploadFile(fileInput.files[0]));
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
    });

    async function coachGame(gameId, btn) {
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = "Queued...";
      const chosenModel = modelSelect.value;
      const body = chosenModel ? { model: chosenModel } : {};
      try {
        const res = await fetch(`/api/games/${encodeURIComponent(gameId)}/coach?async_mode=true`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        let data = await res.json();
        if (res.status === 202 && data.poll_url) {
          const pollUrl = data.poll_url;
          setStatus(`Coaching job queued for ${gameId}...`);
          while (true) {
            if (data.status === "queued") btn.textContent = "Queued...";
            else if (data.status === "running") btn.textContent = "Coaching...";
            if (data.status === "ready") {
              data = data.result || data;
              break;
            }
            if (data.status === "failed") {
              throw new Error(data.error || "Coaching failed");
            }
            await new Promise((resolve) => setTimeout(resolve, 1500));
            const pollRes = await fetch(pollUrl);
            data = await pollRes.json();
            if (!pollRes.ok) {
              throw new Error(data.detail || "Failed to poll coaching job");
            }
          }
        } else if (!res.ok) {
          throw new Error(data.detail || "Coaching failed");
        }
        setStatus(`Coached ${gameId}: Grade ${data.overall_grade}`);
        await refreshAll();
      } catch (err) {
        setStatus(`Coach failed for ${gameId}: ${err.message || err}`, true);
        btn.disabled = false;
        btn.textContent = prev;
      }
    }

    async function refreshDashboard() {
      const [gRes, hRes] = await Promise.all([fetch("/api/games"), fetch("/api/habits")]);
      if (gRes.status === 401 || hRes.status === 401) {
        const tbody = document.getElementById("gamesBody");
        tbody.innerHTML = "";
        const tr = document.createElement("tr");
        const td = el("td", "Sign in or register above to view your match history and coach replays.", "muted");
        td.colSpan = 7;
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }
      const games = await gRes.json();
      const habits = await hRes.json();

      document.getElementById("kpiGames").textContent = habits.total_games;
      document.getElementById("kpiWr").textContent = `${habits.win_rate}%`;
      document.getElementById("kpiCoached").textContent = habits.coached_games;
      document.getElementById("kpiFloat").textContent = habits.avg_mana_floated;
      document.getElementById("kpiLethal").textContent = habits.total_missed_lethal;
      document.getElementById("kpiCost").textContent = `$${(habits.total_cost_usd || 0).toFixed(4)}`;

      const tbody = document.getElementById("gamesBody");
      tbody.innerHTML = "";
      if (!games.length) {
        const tr = document.createElement("tr");
        const td = el("td", rawUploadEnabled
          ? "No games uploaded yet. Drag a Power.log above to begin."
          : "No games yet. Run the Windows uploader with your upload token, then finish a Standard game.", "muted");
        td.colSpan = 7;
        tr.appendChild(td);
        tbody.appendChild(tr);
      } else {
        for (const g of games) {
          const tr = document.createElement("tr");
          tr.appendChild(el("td", `${g.your_class} vs ${g.opponent_class}`));

          const tdRes = document.createElement("td");
          tdRes.appendChild(el("span", `${g.result.toUpperCase()} (${g.end_reason})`, `pill ${g.result}`));
          tr.appendChild(tdRes);

          tr.appendChild(el("td", `${g.total_turns}t ${g.went_first ? "(1st)" : "(Coin)"}`));

          const flags = [];
          if (g.missed_lethal_count) flags.push(`${g.missed_lethal_count} missed lethal`);
          flags.push(`${g.total_mana_floated}m floated`);
          if (g.burned_count) flags.push(`${g.burned_count} burned`);
          if (g.discarded_count) flags.push(`${g.discarded_count} disc`);
          tr.appendChild(el("td", flags.join(" · "), "muted"));

          const gradeText = g.coached
            ? `${g.overall_grade} · ${g.your_archetype || ""} vs ${g.opponent_archetype || ""}`
            : "Not coached";
          tr.appendChild(el("td", gradeText));

          tr.appendChild(el("td", g.cost_usd != null ? `$${g.cost_usd.toFixed(4)}` : "—"));

          const tdAct = document.createElement("td");
          tdAct.style.display = "flex";
          tdAct.style.gap = "8px";
          const untrustedHosted = !rawUploadEnabled && !allowHostedClientCoaching && g.completion_trusted === false;
          const btnLabel = g.coached ? "Re-Coach" : (untrustedHosted ? "Unverified" : "Coach");
          const btn = el("button", btnLabel, (g.coached || untrustedHosted) ? "" : "primary");
          btn.type = "button";
          if (untrustedHosted) {
            btn.disabled = true;
            btn.title = "Hosted coaching requires trusted post-game completion evidence. Use local mode for self-parsed Power.log coaching.";
          } else {
            btn.addEventListener("click", () => coachGame(g.game_id, btn));
          }
          tdAct.appendChild(btn);

          const link = el("a", "View Report", "btn-link");
          link.href = `/games/${encodeURIComponent(g.game_id)}/report`;
          tdAct.appendChild(link);

          const shareBtn = el("button", g.shared ? "Copy Link" : "Share");
          shareBtn.type = "button";
          shareBtn.addEventListener("click", async () => {
            try {
              const sRes = await fetch(`/api/games/${encodeURIComponent(g.game_id)}/share`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: true }),
              });
              const sData = await sRes.json();
              if (!sRes.ok) throw new Error(sData.detail || "Unable to share report");
              if (navigator.clipboard && sData.share_url) {
                navigator.clipboard.writeText(sData.share_url).catch(() => {});
              }
              setStatus(`Shareable report link ready: ${sData.share_url}`);
              await refreshDashboard();
            } catch (err) {
              setStatus(err.message || String(err), true);
            }
          });
          tdAct.appendChild(shareBtn);

          if (g.shared) {
            const unshareBtn = el("button", "Unshare");
            unshareBtn.type = "button";
            unshareBtn.addEventListener("click", async () => {
              const uRes = await fetch(`/api/games/${encodeURIComponent(g.game_id)}/share`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: false }),
              });
              if (uRes.ok) {
                setStatus(`Share link revoked for ${g.game_id}.`);
                await refreshDashboard();
              }
            });
            tdAct.appendChild(unshareBtn);
          }
          tr.appendChild(tdAct);
          tbody.appendChild(tr);
        }
      }

      const mistakesBox = document.getElementById("mistakesBox");
      mistakesBox.innerHTML = "";
      if (habits.top_mistakes.length) {
        for (const m of habits.top_mistakes) {
          const row = el("div", `${formatHabitTag(m.tag)} (x${m.count}) — ${m.latest_note}`);
          row.style.marginBottom = "8px";
          mistakesBox.appendChild(row);
        }
      } else {
        mistakesBox.appendChild(el("span", "Coach games to populate recurring habit tags.", "muted"));
      }

      const matchupsBox = document.getElementById("matchupsBox");
      matchupsBox.innerHTML = "";
      if (habits.decks && habits.decks.length) {
        const hdrD = el("strong", "Your Decks / Archetypes:");
        matchupsBox.appendChild(hdrD);
        for (const dk of habits.decks) {
          matchupsBox.appendChild(
            el("div", `${dk.deck_label}: ${dk.wins}W-${dk.losses}L (${dk.win_rate}%) · ${dk.avg_mana_floated}m avg float`)
          );
        }
      }
      if (habits.matchups.length) {
        const hdrM = el("strong", "By Opponent Class:");
        hdrM.style.display = "block";
        hdrM.style.marginTop = "10px";
        matchupsBox.appendChild(hdrM);
        for (const mu of habits.matchups) {
          matchupsBox.appendChild(
            el("div", `vs ${mu.opponent_class}: ${mu.wins}W-${mu.losses}L (${mu.win_rate}%)`)
          );
        }
      } else if (!habits.decks || !habits.decks.length) {
        matchupsBox.appendChild(el("span", "Upload games to see matchup stats.", "muted"));
      }
    }

    document.getElementById("forgotBtn")?.addEventListener("click", () => {
      const box = document.getElementById("resetBox");
      if (box) box.style.display = box.style.display === "none" ? "flex" : "none";
    });

    document.getElementById("sendResetBtn")?.addEventListener("click", async () => {
      const emailEl = document.getElementById("resetEmail");
      try {
        const res = await fetch("/api/auth/request-reset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: emailEl ? emailEl.value : "" }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Reset request failed");
        setAuthStatus(data.message);
      } catch (err) {
        setAuthStatus(err.message || String(err), true);
      }
    });

    document.getElementById("confirmResetBtn")?.addEventListener("click", async () => {
      const tokEl = document.getElementById("resetToken");
      const pwEl = document.getElementById("resetNewPassword");
      try {
        const res = await fetch("/api/auth/reset-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            token: tokEl ? tokEl.value : "",
            new_password: pwEl ? pwEl.value : "",
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Password update failed");
        setAuthStatus(data.message);
        if (tokEl) tokEl.value = "";
        if (pwEl) pwEl.value = "";
        const box = document.getElementById("resetBox");
        if (box) box.style.display = "none";
      } catch (err) {
        setAuthStatus(err.message || String(err), true);
      }
    });

    const hashParams = new URLSearchParams((window.location.hash || "").replace(/^#/, ""));
    const resetTokParam = hashParams.get("reset_token");
    if (resetTokParam) {
      const box = document.getElementById("resetBox");
      const tokEl = document.getElementById("resetToken");
      if (box) box.style.display = "flex";
      if (tokEl) tokEl.value = resetTokParam;
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      } else {
        window.location.hash = "";
      }
    }

    async function refreshAll() {
      await refreshAccount();
      await refreshDashboard();
    }

    refreshAll();
  </script>
  <footer class="container" style="margin-top: 20px; text-align: center; font-size: 0.85rem; color: var(--muted);">
    <span>HS-Coach is not affiliated with Blizzard Entertainment · 100% Post-Game Only · </span>
    <a href="/privacy" style="color: var(--info);">Privacy Policy</a>
    <span> · </span>
    <a href="/terms" style="color: var(--info);">Terms of Service</a>
    <span> · </span>
    <a href="mailto:hscoach.support@gmail.com" style="color: var(--info);">hscoach.support@gmail.com</a>
  </footer>
</body>
</html>
"""


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "plan": user["plan"],
        "upload_token": user["upload_token"],
        "created_at": user["created_at"],
    }


def _quota_info(store: GameStore, user: dict[str, Any]) -> dict[str, Any]:
    cfg = PLAN_CONFIGS.get(user["plan"], PLAN_CONFIGS["free"])
    used = store.count_coached_this_month(user["user_id"])
    deep_used = (
        store.count_deep_this_month(user["user_id"], FAST_MODELS)
        if cfg.monthly_deep_limit
        else 0
    )
    return {
        "plan": cfg.name,
        "used_this_month": used,
        "monthly_limit": cfg.monthly_coach_limit,
        "remaining": max(0, cfg.monthly_coach_limit - used),
        "deep_used": deep_used,
        "deep_limit": cfg.monthly_deep_limit,
        "deep_remaining": max(0, cfg.monthly_deep_limit - deep_used),
        "allowed_models": sorted(get_plan_allowed_models(cfg.name)),
        "default_model": cfg.default_model,
    }


def create_app(
    card_db: CardDB,
    store: GameStore,
    *,
    config: AppConfig | None = None,
    max_workers: int | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Create the FastAPI post-game coaching dashboard & API app."""
    cfg = config or load_app_config(mode="local", host="127.0.0.1")
    store.release_stale_reservations()
    eff_workers = max_workers if max_workers is not None else cfg.max_workers
    job_manager = CoachJobManager(
        max_workers=eff_workers, max_queued=cfg.max_queued_jobs
    )
    limiter = rate_limiter or RateLimiter()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        try:
            yield
        finally:
            job_manager.shutdown(wait=True)

    app = FastAPI(title="HS-Coach", version="0.1.0", lifespan=lifespan)
    app.state.config = cfg
    app.state.card_db = card_db
    app.state.store = store
    app.state.job_manager = job_manager
    app.state.rate_limiter = limiter
    session_secret = cfg.session_secret

    @app.exception_handler(BodyTooLargeError)
    async def handle_body_too_large(
        _request: Request, exc: BodyTooLargeError
    ) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=413)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        if is_secure_request(
            request, cfg.trusted_proxies, cookie_secure=cfg.cookie_secure
        ):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    app.add_middleware(
        MaxBodySizeMiddleware,
        default_limit=cfg.max_json_bytes,
        path_limits={
            "/api/upload": cfg.max_upload_bytes,
            "/api/replays": cfg.max_replay_bytes,
            "/api/billing/webhook": cfg.max_webhook_bytes,
        },
    )

    def _enforce_rate_limit(request: Request, bucket: str) -> None:
        allowed, retry_after = app.state.rate_limiter.check(
            bucket, get_client_ip(request, cfg.trusted_proxies)
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )

    def _resolve_session_user(
        session_cookie: str | None,
    ) -> tuple[dict[str, Any] | None, bool]:
        if not session_cookie:
            return None, False
        details = verify_session_token_details(session_cookie, session_secret)
        if details is None:
            return None, True
        uid, jti, _exp = details
        if not app.state.store.is_session_active(jti, uid):
            return None, True
        user = app.state.store.get_user_by_id(uid)
        if user is None:
            return None, True
        return user, False

    def _resolve_actor(
        request: Request,
        response: Response | None,
        hs_coach_session: str | None,
        hs_coach_guest: str | None,
        *,
        allow_create_guest: bool = False,
    ) -> tuple[str, dict[str, Any] | None, str]:
        user, has_invalid_session = _resolve_session_user(hs_coach_session)
        if has_invalid_session:
            raise HTTPException(status_code=401, detail="Invalid or expired session.")
        if user is not None:
            return user["user_id"], user, user["plan"]

        if cfg.mode == "hosted":
            if cfg.allow_hosted_guests:
                if hs_coach_guest:
                    guest_id = verify_guest_token(hs_coach_guest, session_secret)
                    if guest_id is not None:
                        return guest_id, None, "guest"
                    raise HTTPException(status_code=401, detail="Invalid guest session.")
                if allow_create_guest and response is not None:
                    guest_id = f"guest_{secrets.token_hex(8)}"
                    tok = create_guest_token(guest_id, session_secret)
                    response.set_cookie(
                        key=GUEST_COOKIE_NAME,
                        value=tok,
                        httponly=True,
                        samesite="lax",
                        secure=is_secure_request(
                            request, cfg.trusted_proxies, cookie_secure=cfg.cookie_secure
                        ),
                        max_age=SESSION_TTL_SECONDS,
                    )
                    return guest_id, None, "guest"
            raise HTTPException(status_code=401, detail="Authentication required.")

        peer = request.client.host if request.client and request.client.host else ""
        if not (is_loopback_host(peer) or peer == "testclient"):
            raise HTTPException(
                status_code=403, detail="Local mode only allows loopback clients."
            )
        return "local", None, "local"

    def _validate_policy_and_config(
        plan_name: str,
        requested_model: str | None,
        requested_provider: str | None,
    ) -> Any:
        if requested_provider is not None and requested_provider.strip():
            raise HTTPException(
                status_code=403, detail="Provider selection is server-controlled."
            )
        if plan_name == "local":
            target_model = requested_model
        else:
            plan_cfg = PLAN_CONFIGS.get(plan_name, PLAN_CONFIGS["free"])
            allowed = get_plan_allowed_models(plan_cfg.name)
            target_model = requested_model or plan_cfg.default_model
            if target_model not in allowed:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Model {target_model!r} is not allowed for the {plan_cfg.name} plan. "
                        f"Allowed: {', '.join(sorted(allowed))}."
                    ),
                )
        try:
            return resolve_coach_config(provider=None, model=target_model)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _reserve_for_actor(
        actor_id: str,
        plan_name: str,
        game_id: str,
        model_name: str,
    ) -> str | None:
        if plan_name == "local":
            return None
        plan_cfg = PLAN_CONFIGS.get(plan_name, PLAN_CONFIGS["free"])
        res_id, err_code = app.state.store.reserve_coaching_slot(
            actor_id,
            game_id,
            monthly_limit=plan_cfg.monthly_coach_limit,
            model=model_name,
            deep_limit=plan_cfg.monthly_deep_limit if plan_cfg.monthly_deep_limit else None,
            fast_models=FAST_MODELS,
        )
        if err_code == "in_flight":
            raise HTTPException(
                status_code=409,
                detail="Coaching is already in progress for this game.",
            )
        if err_code == "quota_exceeded":
            raise HTTPException(
                status_code=402,
                detail=(
                    f"Monthly {plan_cfg.name} coaching quota reached "
                    f"({plan_cfg.monthly_coach_limit}). Upgrade to Pro for higher limits."
                ),
            )
        if err_code == "deep_quota_exceeded":
            raise HTTPException(
                status_code=402,
                detail=(
                    f"You've used all {plan_cfg.monthly_deep_limit} deep reviews this month. "
                    "Choose Fast in Coaching Model to keep coaching; deep reviews reset next month."
                ),
            )
        return res_id

    def _issue_session(response: Response, request: Request, user_id: str) -> None:
        jti = secrets.token_hex(16)
        tok = create_session_token(
            user_id,
            session_secret,
            ttl_seconds=SESSION_TTL_SECONDS,
            session_id=jti,
        )
        details = verify_session_token_details(tok, session_secret)
        assert details is not None
        _, _, exp = details
        app.state.store.create_session(jti, user_id, exp)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=tok,
            httponly=True,
            samesite="lax",
            secure=is_secure_request(
                request, cfg.trusted_proxies, cookie_secure=cfg.cookie_secure
            ),
            max_age=SESSION_TTL_SECONDS,
        )
        response.delete_cookie(GUEST_COOKIE_NAME)

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy_policy() -> HTMLResponse:
        return HTMLResponse(_PRIVACY_HTML)

    @app.get("/terms", response_class=HTMLResponse)
    def terms_of_service() -> HTMLResponse:
        return HTMLResponse(_TERMS_HTML)

    @app.get("/api/health")
    def health() -> Any:
        db_ok = app.state.store.ping()
        if not db_ok:
            return JSONResponse({"status": "degraded", "db_ok": False}, status_code=503)
        return {
            "status": "ok",
            "db_ok": True,
            "cards_loaded": len(app.state.card_db.by_id),
        }

    # ---- Auth endpoints ----

    @app.post("/api/auth/register")
    def register(
        creds: AuthCredentials, request: Request, response: Response
    ) -> dict[str, Any]:
        _enforce_rate_limit(request, "auth")
        email = creds.email.strip().lower()
        if "@" not in email or "." not in email.split("@", 1)[-1]:
            raise HTTPException(status_code=400, detail="Valid email address is required.")
        try:
            pw_hash = hash_password(creds.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            user = app.state.store.create_user(
                email=email,
                password_hash=pw_hash,
                upload_token=generate_upload_token(),
                plan="free",
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Email is already registered.") from exc

        _issue_session(response, request, user["user_id"])
        return {
            "user": _public_user(user),
            "quota": _quota_info(app.state.store, user),
        }

    @app.post("/api/auth/login")
    def login(
        creds: AuthCredentials, request: Request, response: Response
    ) -> dict[str, Any]:
        _enforce_rate_limit(request, "auth")
        user = app.state.store.get_user_by_email(creds.email)
        if user is None or not verify_password(creds.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password.")
        _issue_session(response, request, user["user_id"])
        return {
            "user": _public_user(user),
            "quota": _quota_info(app.state.store, user),
        }

    @app.post("/api/auth/logout")
    def logout(
        response: Response,
        hs_coach_session: str | None = Cookie(default=None),
    ) -> dict[str, bool]:
        if hs_coach_session:
            details = verify_session_token_details(hs_coach_session, session_secret)
            if details is not None:
                app.state.store.revoke_session(details[1])
        response.delete_cookie(SESSION_COOKIE_NAME)
        return {"ok": True}

    @app.post("/api/auth/request-reset")
    def request_password_reset(
        req: PasswordResetRequest, request: Request
    ) -> dict[str, Any]:
        _enforce_rate_limit(request, "auth")
        email = req.email.strip().lower()
        user = app.state.store.get_user_by_email(email) if email else None
        if user is not None:
            raw_tok, tok_hash = generate_password_reset_token()
            exp = int(time.time()) + PASSWORD_RESET_TTL_SECONDS
            app.state.store.create_password_reset(user["user_id"], tok_hash, exp)
            base_url = (cfg.public_url or str(request.base_url)).rstrip("/")
            reset_url = f"{base_url}/#reset_token={raw_tok}"
            try:
                send_password_reset_email(user["email"], reset_url)
            except Exception:
                pass
        return {
            "ok": True,
            "message": "If an account exists for that email, a reset link has been sent.",
        }

    @app.post("/api/auth/reset-password")
    def confirm_password_reset(
        req: PasswordResetConfirm, request: Request
    ) -> dict[str, Any]:
        _enforce_rate_limit(request, "auth")
        raw_token = req.token.strip()
        if not raw_token:
            raise HTTPException(
                status_code=400, detail="Invalid or expired reset token."
            )
        try:
            new_pw_hash = hash_password(req.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        uid = app.state.store.consume_password_reset(
            hash_reset_token(raw_token), new_pw_hash
        )
        if uid is None:
            raise HTTPException(
                status_code=400, detail="Invalid or expired reset token."
            )
        return {
            "ok": True,
            "message": "Password updated. Please sign in with your new password.",
        }

    @app.get("/api/auth/me")
    def auth_me(
        hs_coach_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        user, has_invalid = _resolve_session_user(hs_coach_session)
        if has_invalid or user is None:
            return {
                "authenticated": False,
                "user": None,
                "quota": None,
                "mode": cfg.mode,
                "raw_upload_enabled": cfg.allow_raw_log_upload,
                "guests_enabled": cfg.allow_hosted_guests,
                "allow_hosted_client_coaching": cfg.allow_hosted_client_coaching,
            }
        return {
            "authenticated": True,
            "user": _public_user(user),
            "quota": _quota_info(app.state.store, user),
            "mode": cfg.mode,
            "raw_upload_enabled": cfg.allow_raw_log_upload,
            "guests_enabled": cfg.allow_hosted_guests,
            "allow_hosted_client_coaching": cfg.allow_hosted_client_coaching,
        }

    @app.post("/api/auth/rotate-token")
    def rotate_token(
        hs_coach_session: str | None = Cookie(default=None),
    ) -> dict[str, str]:
        user, has_invalid = _resolve_session_user(hs_coach_session)
        if has_invalid or user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        new_tok = app.state.store.rotate_upload_token(
            user["user_id"], generate_upload_token()
        )
        return {"upload_token": new_tok}

    # ---- Stripe billing endpoints ----

    @app.post("/api/billing/checkout")
    def billing_checkout(
        hs_coach_session: str | None = Cookie(default=None),
    ) -> dict[str, str]:
        user, has_invalid = _resolve_session_user(hs_coach_session)
        if has_invalid or user is None:
            raise HTTPException(status_code=401, detail="Sign in before upgrading to Pro.")
        stripe_cfg = resolve_stripe_config()
        if not stripe_cfg.secret_key or not stripe_cfg.pro_price_id:
            raise HTTPException(
                status_code=503,
                detail="Stripe billing is not configured on this server.",
            )
        try:
            sess = create_checkout_session(
                stripe_cfg,
                user_id=user["user_id"],
                email=user["email"],
                customer_id=user.get("stripe_customer_id"),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Stripe error: {exc}") from exc
        return {"checkout_url": str(sess.get("url", ""))}

    @app.post("/api/billing/portal")
    def billing_portal(
        hs_coach_session: str | None = Cookie(default=None),
    ) -> dict[str, str]:
        user, has_invalid = _resolve_session_user(hs_coach_session)
        if has_invalid or user is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        cust_id = user.get("stripe_customer_id")
        if not cust_id:
            raise HTTPException(status_code=400, detail="No Stripe customer linked to this account.")
        stripe_cfg = resolve_stripe_config()
        if not stripe_cfg.secret_key:
            raise HTTPException(
                status_code=503,
                detail="Stripe billing is not configured on this server.",
            )
        try:
            sess = create_billing_portal_session(stripe_cfg, customer_id=cust_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Stripe error: {exc}") from exc
        return {"portal_url": str(sess.get("url", ""))}

    @app.post("/api/billing/webhook")
    async def billing_webhook(
        request: Request,
        stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    ) -> dict[str, Any]:
        stripe_cfg = resolve_stripe_config()
        if not stripe_cfg.webhook_secret:
            raise HTTPException(
                status_code=503,
                detail="Stripe webhook secret is not configured.",
            )
        raw_body = await request.body()
        if not stripe_signature or not verify_stripe_signature(
            raw_body, stripe_signature, stripe_cfg.webhook_secret
        ):
            raise HTTPException(status_code=400, detail="Invalid Stripe signature.")
        try:
            event = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook JSON.") from exc
        try:
            return handle_stripe_event(event, app.state.store, config=stripe_cfg)
        except TransientStripeError as exc:
            raise HTTPException(
                status_code=503, detail="Temporary Stripe error; please retry."
            ) from exc

    # ---- Replay ingestion & upload ----

    @app.post("/api/replays")
    def ingest_replay(
        replay: GameReplay,
        request: Request,
        response: Response,
        auto_coach: bool = Query(default=False),
        model: str | None = Query(default=None),
        provider: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        _enforce_rate_limit(request, "upload")

        if provider is not None and provider.strip():
            raise HTTPException(
                status_code=403, detail="Provider selection is server-controlled."
            )

        target_user_id: str
        plan_name: str

        bearer_token = ""
        if authorization and authorization.strip().lower().startswith("bearer "):
            bearer_token = authorization.strip()[7:].strip()

        required_global_token = os.environ.get("HS_COACH_UPLOAD_TOKEN", "").strip()

        if bearer_token:
            matched_user = app.state.store.get_user_by_upload_token(bearer_token)
            if matched_user is not None:
                target_user_id = matched_user["user_id"]
                plan_name = matched_user["plan"]
            elif (
                cfg.mode == "local"
                and required_global_token
                and hmac.compare_digest(bearer_token, required_global_token)
            ):
                target_user_id = "local"
                plan_name = "local"
            else:
                raise HTTPException(status_code=401, detail="Invalid upload token.")
        elif cfg.mode == "local" and required_global_token:
            raise HTTPException(status_code=401, detail="Missing upload token.")
        else:
            target_user_id, _u, plan_name = _resolve_actor(
                request,
                response,
                hs_coach_session,
                hs_coach_guest,
                allow_create_guest=True,
            )

        try:
            validate_completed_replay(replay)
            replay = canonicalize_replay(replay, app.state.card_db)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        gid = replay.metadata.game_id
        is_new = app.state.store.upsert_replay(
            replay, user_id=target_user_id, completion_trusted=False
        )
        loaded = app.state.store.get_game(gid, user_id=target_user_id)
        existing_report = loaded[1] if loaded else None
        coached = existing_report is not None
        grade = existing_report.overall_grade if existing_report else None
        coach_error: str | None = None

        if auto_coach and not coached:
            if (
                cfg.mode == "hosted"
                and not cfg.allow_hosted_client_coaching
                and not app.state.store.is_completion_trusted(
                    gid, user_id=target_user_id
                )
            ):
                coach_error = _HOSTED_UNTRUSTED_COACH_MSG
            else:
                res_id: str | None = None
                try:
                    coach_cfg = _validate_policy_and_config(plan_name, model, provider)
                    res_id = _reserve_for_actor(
                        target_user_id, plan_name, gid, coach_cfg.model
                    )
                    report = coach_game(replay, coach_cfg)
                    app.state.store.save_coach_report(
                        gid, report, user_id=target_user_id
                    )
                    if res_id is not None:
                        app.state.store.complete_coaching_reservation(res_id)
                    coached = True
                    grade = report.overall_grade
                except HTTPException as exc:
                    if res_id is not None:
                        app.state.store.release_coaching_reservation(res_id)
                    coach_error = str(exc.detail)
                except Exception as exc:
                    if res_id is not None:
                        app.state.store.release_coaching_reservation(res_id)
                    coach_error = str(exc)

        out: dict[str, Any] = {
            "game_id": gid,
            "is_new": is_new,
            "coached": coached,
            "overall_grade": grade,
            "report_url": f"/games/{gid}/report",
        }
        if coach_error is not None:
            out["coach_error"] = coach_error
        return out

    @app.post("/api/upload")
    async def upload_power_log(
        request: Request,
        response: Response,
        file: UploadFile = File(...),
        all_modes: bool = Query(default=False),
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> JSONResponse:
        if not cfg.allow_raw_log_upload:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Raw Power.log upload is disabled in hosted mode. "
                    "Parse locally so only anonymized replays leave your device."
                ),
            )
        _enforce_rate_limit(request, "upload")
        user_id, _u, _plan = _resolve_actor(
            request, response, hs_coach_session, hs_coach_guest, allow_create_guest=True
        )
        fd, tmp_name = tempfile.mkstemp(prefix="hs_coach_upload_", suffix=".log")
        tmp_path = Path(tmp_name)
        total_bytes = 0
        try:
            with os.fdopen(fd, "wb") as out_fh:
                while chunk := await file.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > cfg.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Uploaded Power.log exceeds {cfg.max_upload_bytes} byte limit.",
                        )
                    out_fh.write(chunk)

            parsed = await asyncio.to_thread(
                parse_power_log,
                tmp_path,
                app.state.card_db,
                standard_only=not all_modes,
            )
        finally:
            await file.close()
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        new_games = 0
        duplicate_games = 0
        game_ids: list[str] = []
        for replay in parsed.replays:
            is_new = app.state.store.upsert_replay(
                replay, user_id=user_id, completion_trusted=True
            )
            if is_new:
                new_games += 1
            else:
                duplicate_games += 1
            game_ids.append(replay.metadata.game_id)

        return JSONResponse(
            {
                "total_games_found": parsed.total_games_found,
                "new_games": new_games,
                "duplicate_games": duplicate_games,
                "skipped_incomplete": parsed.skipped_incomplete,
                "skipped_non_standard": parsed.skipped_non_standard,
                "game_ids": game_ids,
            }
        )

    @app.get("/api/games")
    def list_games(
        request: Request,
        response: Response,
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> list[dict[str, Any]]:
        actor_id, _u, _plan = _resolve_actor(
            request, response, hs_coach_session, hs_coach_guest, allow_create_guest=True
        )
        return app.state.store.list_games(user_id=actor_id)

    @app.get("/api/games/{game_id}")
    def get_game(
        game_id: str,
        request: Request,
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        actor_id, _u, _plan = _resolve_actor(
            request, None, hs_coach_session, hs_coach_guest
        )
        loaded = app.state.store.get_game(game_id, user_id=actor_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Game not found")
        replay, report = loaded
        return {
            "replay": replay.model_dump(),
            "report": report.model_dump() if report else None,
        }

    @app.post("/api/games/{game_id}/coach")
    def coach_stored_game(
        game_id: str,
        request: Request,
        req: CoachRequest | None = None,
        async_mode: bool = Query(default=False),
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> Any:
        _enforce_rate_limit(request, "coach")
        actor_id, _u, plan_name = _resolve_actor(
            request, None, hs_coach_session, hs_coach_guest
        )
        loaded = app.state.store.get_game(game_id, user_id=actor_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Game not found")
        replay, _ = loaded
        if (
            cfg.mode == "hosted"
            and not cfg.allow_hosted_client_coaching
            and not app.state.store.is_completion_trusted(game_id, user_id=actor_id)
        ):
            raise HTTPException(status_code=403, detail=_HOSTED_UNTRUSTED_COACH_MSG)

        try:
            replay = canonicalize_replay(replay, app.state.card_db)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        req_model = req.model if req else None
        req_provider = req.provider if req else None
        coach_cfg = _validate_policy_and_config(plan_name, req_model, req_provider)

        if async_mode:
            existing = app.state.job_manager.get_active_job(actor_id, game_id)
            if existing is not None:
                return JSONResponse(
                    {
                        "job_id": existing["job_id"],
                        "game_id": game_id,
                        "status": existing["status"],
                        "poll_url": f"/api/jobs/{existing['job_id']}",
                    },
                    status_code=202,
                )

            res_id = _reserve_for_actor(
                actor_id, plan_name, game_id, coach_cfg.model
            )

            def _run_async_coach() -> dict[str, Any]:
                try:
                    report = coach_game(replay, coach_cfg)
                    app.state.store.save_coach_report(
                        game_id, report, user_id=actor_id
                    )
                except Exception:
                    if res_id is not None:
                        app.state.store.release_coaching_reservation(res_id)
                    raise
                else:
                    if res_id is not None:
                        app.state.store.complete_coaching_reservation(res_id)
                    return {
                        "game_id": game_id,
                        "overall_grade": report.overall_grade,
                        "your_archetype": report.your_archetype,
                        "opponent_archetype": report.opponent_archetype,
                        "usage": report.usage.model_dump() if report.usage else None,
                    }

            try:
                job = app.state.job_manager.submit(
                    actor_id, game_id, _run_async_coach
                )
            except QueueFullError as exc:
                if res_id is not None:
                    app.state.store.release_coaching_reservation(res_id)
                return JSONResponse(
                    {"detail": str(exc)},
                    status_code=503,
                    headers={"Retry-After": "10"},
                )

            return JSONResponse(
                {
                    "job_id": job["job_id"],
                    "game_id": game_id,
                    "status": job["status"],
                    "poll_url": f"/api/jobs/{job['job_id']}",
                },
                status_code=202,
            )

        res_id = _reserve_for_actor(
            actor_id, plan_name, game_id, coach_cfg.model
        )
        try:
            report = coach_game(replay, coach_cfg)
            app.state.store.save_coach_report(game_id, report, user_id=actor_id)
        except Exception as exc:
            if res_id is not None:
                app.state.store.release_coaching_reservation(res_id)
            raise HTTPException(
                status_code=502, detail=f"LLM coaching error: {exc}"
            ) from exc
        else:
            if res_id is not None:
                app.state.store.complete_coaching_reservation(res_id)
            return {
                "game_id": game_id,
                "overall_grade": report.overall_grade,
                "your_archetype": report.your_archetype,
                "opponent_archetype": report.opponent_archetype,
                "usage": report.usage.model_dump() if report.usage else None,
            }

    @app.get("/api/jobs/{job_id}")
    def get_coach_job(
        job_id: str,
        request: Request,
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        actor_id, _u, _plan = _resolve_actor(
            request, None, hs_coach_session, hs_coach_guest
        )
        job = app.state.job_manager.get(job_id, actor_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Coaching job not found")
        job["poll_url"] = f"/api/jobs/{job_id}"
        return job

    @app.get("/api/habits")
    def get_habits(
        request: Request,
        response: Response,
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        actor_id, _u, _plan = _resolve_actor(
            request, response, hs_coach_session, hs_coach_guest, allow_create_guest=True
        )
        return app.state.store.get_habit_summary(user_id=actor_id)

    @app.get("/games/{game_id}/report", response_class=HTMLResponse)
    def view_report(
        game_id: str,
        request: Request,
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        actor_id, _u, _plan = _resolve_actor(
            request, None, hs_coach_session, hs_coach_guest
        )
        loaded = app.state.store.get_game(game_id, user_id=actor_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Game not found")
        replay, report = loaded
        html = render_html_report(replay, report)
        nav = (
            '<div style="max-width:1120px;margin:0 auto 14px auto;">'
            '<a href="/" style="color:#38bdf8;font-weight:600;text-decoration:none;">'
            "&larr; Back to HS-Coach Dashboard</a></div>"
        )
        html = html.replace("<body>\n", f"<body>\n  {nav}\n", 1)
        return HTMLResponse(html)

    @app.post("/api/games/{game_id}/share")
    def toggle_share_game(
        game_id: str,
        request: Request,
        req: ShareToggleRequest | None = None,
        hs_coach_session: str | None = Cookie(default=None),
        hs_coach_guest: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        actor_id, _u, _plan = _resolve_actor(
            request, None, hs_coach_session, hs_coach_guest
        )
        loaded = app.state.store.get_game(game_id, user_id=actor_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Game not found")

        enable = True if req is None else bool(req.enabled)
        if not enable:
            app.state.store.set_game_share_token(actor_id, game_id, None)
            return {
                "game_id": game_id,
                "shared": False,
                "share_token": None,
                "share_url": None,
            }

        tok = app.state.store.get_game_share_token(actor_id, game_id)
        if not tok:
            tok = secrets.token_urlsafe(16)
            app.state.store.set_game_share_token(actor_id, game_id, tok)
        base_url = (cfg.public_url or str(request.base_url)).rstrip("/")
        return {
            "game_id": game_id,
            "shared": True,
            "share_token": tok,
            "share_url": f"{base_url}/shared/{tok}",
        }

    @app.get("/shared/{share_token}", response_class=HTMLResponse)
    def view_shared_report(share_token: str) -> HTMLResponse:
        loaded = app.state.store.get_game_by_share_token(share_token)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Shared report not found")
        replay, report = loaded
        rendered = render_html_report(replay, report)

        m = replay.metadata
        ps = replay.pre_check_summary
        grade_part = f" · Grade {report.overall_grade}" if report else ""
        og_title = f"{m.your_class} vs {m.opponent_class} ({m.result.upper()}{grade_part}) — HS-Coach Report"
        if report:
            og_desc = (
                f"{report.your_archetype} vs {report.opponent_archetype}: "
                f"{report.key_takeaway} · 100% Post-Game · Anonymized by HS-Coach"
            )
        else:
            og_desc = (
                f"{m.total_turns} turns · {ps.total_mana_floated} mana floated · "
                f"{len(ps.missed_lethal_turns)} missed lethal flags · 100% Post-Game · Anonymized by HS-Coach"
            )

        meta_tags = "\n".join(
            [
                '  <meta property="og:type" content="article" />',
                f'  <meta property="og:title" content="{html_escape(og_title, quote=True)}" />',
                f'  <meta property="og:description" content="{html_escape(og_desc, quote=True)}" />',
                '  <meta name="twitter:card" content="summary" />',
                f'  <meta name="twitter:title" content="{html_escape(og_title, quote=True)}" />',
                f'  <meta name="twitter:description" content="{html_escape(og_desc, quote=True)}" />',
            ]
        )
        rendered = rendered.replace("</head>", f"{meta_tags}\n</head>", 1)

        cta = (
            '<div style="max-width:1120px;margin:0 auto 14px auto;display:flex;justify-content:space-between;'
            'align-items:center;flex-wrap:wrap;gap:10px;background:#171f2e;border:1px solid #2c3952;'
            'border-radius:10px;padding:10px 14px;">'
            '<span style="color:#9aa7bd;font-size:0.9rem;">Analyzed with '
            '<strong style="color:#f0b949;">HS-Coach</strong> &mdash; 100% Post-Game AI Coach for Standard Hearthstone (Anonymized)</span>'
            '<a href="/" style="background:#f0b949;color:#111827;padding:6px 12px;border-radius:8px;'
            'font-weight:700;text-decoration:none;font-size:0.86rem;">Try HS-Coach Free &rarr;</a>'
            '</div>'
        )
        rendered = rendered.replace("<body>\n", f"<body>\n  {cta}\n", 1)
        return HTMLResponse(rendered)

    return app
