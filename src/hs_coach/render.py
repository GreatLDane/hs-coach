"""Self-contained HTML report renderer for GameReplay and CoachReport."""

from __future__ import annotations

import re

from jinja2 import Environment, select_autoescape

from hs_coach.cards import clean_hs_display_text, partition_glossary
from hs_coach.schemas import CoachReport, GameReplay


def format_habit_tag(tag: str) -> str:
    """Normalize snake_case, kebab-case, or mixed-case habit tags to Title Case."""
    parts = [p for p in re.split(r"[\s_-]+", (tag or "").strip()) if p]
    if not parts:
        return tag or ""
    return " ".join(p[:1].upper() + p[1:] for p in parts)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>HS-Coach Report — {{ replay.metadata.your_class }} vs {{ replay.metadata.opponent_class }} ({{ replay.metadata.game_id }})</title>
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
      --tie: #9aa7bd;
      --warn: #f59e0b;
      --blunder: #fb923c;
      --lethal: #ef4444;
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
    .container { max-width: 1120px; margin: 0 auto; display: grid; gap: 20px; }
    a { color: var(--info); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
    }
    .header {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      border-left: 4px solid var(--accent);
    }
    .match-title { font-size: 1.35rem; font-weight: 700; margin: 0 0 6px 0; }
    .sub-meta { color: var(--muted); font-size: 0.9rem; display: flex; flex-wrap: wrap; gap: 10px; }
    .pill {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
      background: var(--surface-alt);
      border: 1px solid var(--border);
    }
    .pill.win { color: var(--win); border-color: rgba(39, 192, 125, 0.4); }
    .pill.loss { color: var(--loss); border-color: rgba(242, 95, 92, 0.4); }
    .pill.tie { color: var(--tie); }
    .pill.lethal { color: #fff; background: rgba(239, 68, 68, 0.25); border-color: var(--lethal); }
    .pill.blunder { color: #ffd7b5; background: rgba(251, 146, 60, 0.2); border-color: var(--blunder); }
    .pill.inaccuracy { color: #fde68a; background: rgba(245, 158, 11, 0.18); border-color: var(--warn); }
    .pill.good { color: var(--win); border-color: rgba(39, 192, 125, 0.4); }
    .pill.questionable { color: var(--warn); border-color: rgba(245, 158, 11, 0.4); }
    .pill.mistake { color: var(--loss); border-color: rgba(242, 95, 92, 0.4); }
    .grade-box {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .grade-badge {
      width: 60px;
      height: 60px;
      border-radius: 12px;
      background: linear-gradient(135deg, #27344d, #1b2436);
      border: 2px solid var(--accent);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.5rem;
      font-weight: 800;
      color: var(--accent);
    }
    .kpi-strip {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
    }
    .kpi {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
    }
    .kpi-label { font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
    .kpi-value { font-size: 1.25rem; font-weight: 700; margin-top: 4px; }
    .kpi-value.alert { color: var(--loss); }
    h2 { margin: 0 0 12px 0; font-size: 1.1rem; color: var(--accent); }
    h3 { margin: 0 0 8px 0; font-size: 0.98rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
    th { color: var(--muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase; }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
    .tp-card {
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-left: 4px solid var(--warn);
      border-radius: 10px;
      padding: 14px 16px;
      margin-bottom: 12px;
    }
    .tp-card.sev-missed-lethal { border-left-color: var(--lethal); }
    .tp-card.sev-blunder { border-left-color: var(--blunder); }
    .tp-meta { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
    .tp-row { margin: 5px 0; font-size: 0.92rem; }
    .tp-label { font-weight: 600; color: var(--muted); }
    .turn-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 16px;
      margin-bottom: 12px;
    }
    .turn-card.you { border-left: 4px solid var(--info); }
    .turn-card.opponent { border-left: 4px solid #64748b; }
    .turn-card.flagged { border-left: 4px solid var(--lethal); background: #1c1b29; }
    .turn-head {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border);
    }
    .turn-title { font-weight: 700; font-size: 1rem; }
    .board-box {
      background: var(--surface-alt);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 0.87rem;
    }
    .chip-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
    .chip {
      background: #25324a;
      border: 1px solid #354769;
      border-radius: 6px;
      padding: 3px 8px;
      font-size: 0.82rem;
    }
    .chip.dormant { border-color: #a855f7; color: #e9d5ff; }
    .chip.ready { border-color: var(--win); }
    .actions-list { margin: 8px 0 0 18px; padding: 0; font-size: 0.9rem; }
    .precheck-alert {
      margin-top: 10px;
      padding: 8px 12px;
      border-radius: 8px;
      background: rgba(239, 68, 68, 0.14);
      border: 1px solid rgba(239, 68, 68, 0.45);
      font-size: 0.87rem;
    }
    .precheck-note {
      margin-top: 10px;
      padding: 8px 12px;
      border-radius: 8px;
      background: rgba(56, 189, 248, 0.08);
      border: 1px solid rgba(56, 189, 248, 0.25);
      font-size: 0.85rem;
      color: var(--muted);
    }
    .preview-banner {
      border-left: 4px solid var(--info);
      background: rgba(56, 189, 248, 0.08);
    }
    details summary { cursor: pointer; font-weight: 600; color: var(--accent); }
    code { background: var(--surface-alt); padding: 1px 5px; border-radius: 4px; font-size: 0.85em; }
  </style>
</head>
<body>
  <div class="container">
    <section class="card header">
      <div>
        <h1 class="match-title">
          You ({{ replay.metadata.your_class }} — {{ replay.metadata.your_hero }})
          vs
          Opponent ({{ replay.metadata.opponent_class }} — {{ replay.metadata.opponent_hero }})
        </h1>
        <div class="sub-meta">
          <span class="pill {{ replay.metadata.result }}">{{ replay.metadata.result | upper }} ({{ replay.metadata.end_reason }})</span>
          <span class="pill">{{ "Went First" if replay.metadata.went_first else "Second (The Coin)" }}</span>
          <span class="pill">{{ replay.metadata.total_turns }} turns ({{ replay.metadata.your_turns_count }} yours)</span>
          <span class="pill">{{ replay.metadata.format_type }} / {{ replay.metadata.game_type }}</span>
          {% if report %}
            <span class="pill">{{ report.your_archetype }} vs {{ report.opponent_archetype }}</span>
          {% endif %}
          <span>ID: <code>{{ replay.metadata.game_id }}</code></span>
        </div>
      </div>
      {% if report %}
      <div class="grade-box">
        <div class="grade-badge">{{ report.overall_grade }}</div>
        <div style="font-size: 0.82rem; color: var(--muted);">
          <div><strong>Overall Grade</strong></div>
          {% if report.usage %}
            <div>{{ report.usage.provider }} / {{ report.usage.model }}</div>
            <div>{{ report.usage.input_tokens }} in + {{ report.usage.output_tokens }} out tokens
              {% if report.usage.estimated_cost_usd is not none %}
                · ${{ "%.4f" | format(report.usage.estimated_cost_usd) }}
              {% endif %}
            </div>
          {% endif %}
        </div>
      </div>
      {% endif %}
    </section>

    <section class="kpi-strip">
      <div class="kpi">
        <div class="kpi-label">Missed Lethal Turns</div>
        <div class="kpi-value {% if replay.pre_check_summary.missed_lethal_turns %}alert{% endif %}">
          {{ replay.pre_check_summary.missed_lethal_turns | join(", ") if replay.pre_check_summary.missed_lethal_turns else "0" }}
        </div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Total Mana Floated</div>
        <div class="kpi-value">{{ replay.pre_check_summary.total_mana_floated }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Cards Burned / Discarded</div>
        <div class="kpi-value {% if replay.pre_check_summary.your_cards_burned or replay.pre_check_summary.your_cards_discarded %}alert{% endif %}">
          {{ replay.pre_check_summary.your_cards_burned | length }} / {{ replay.pre_check_summary.your_cards_discarded | length }}
        </div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Unused Attack Turns</div>
        <div class="kpi-value">
          {{ replay.pre_check_summary.turns_with_unused_attacks | length }}
        </div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Unused Ready Locations</div>
        <div class="kpi-value">
          {{ replay.pre_check_summary.turns_with_unused_ready_locations | length }}
        </div>
      </div>
    </section>

    {% if report %}
    <section class="card">
      <h2>Match Summary & Key Takeaway</h2>
      <p style="margin-top: 0;">{{ report.match_summary }}</p>
      <div class="precheck-note" style="color: var(--text);">
        <strong>Key Takeaway:</strong> {{ report.key_takeaway }}
      </div>
    </section>

    <section class="card">
      <h2>Mulligan Review (Grade: {{ report.mulligan_review.overall_grade }})</h2>
      <p style="margin-top: 0; color: var(--muted);">{{ report.mulligan_review.summary }}</p>
      <table>
        <thead>
          <tr>
            <th>Card</th>
            <th>Action</th>
            <th>Verdict</th>
            <th>Recommended</th>
            <th>Reasoning</th>
          </tr>
        </thead>
        <tbody>
          {% for v in report.mulligan_review.card_verdicts %}
          <tr>
            <td><strong>{{ v.card_name }}</strong></td>
            <td>{{ v.action_taken }}</td>
            <td><span class="pill {{ v.verdict }}">{{ v.verdict }}</span></td>
            <td>{{ v.recommended_action }}</td>
            <td>{{ v.reasoning }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% if replay.mulligan.final_opening_hand %}
      <div class="chip-list" style="margin-top: 12px;">
        <span style="color: var(--muted); font-size: 0.85rem;">Final opening hand:</span>
        {% for hc in replay.mulligan.final_opening_hand %}
          <span class="chip">({{ hc.cost }}) {{ hc.name }}</span>
        {% endfor %}
      </div>
      {% endif %}
    </section>

    <section class="card">
      <h2>Top Turning Points & Mistakes</h2>
      {% if report.turning_points %}
        {% for tp in report.turning_points %}
        <div class="tp-card {% if tp.severity == 'Missed Lethal' %}sev-missed-lethal{% elif tp.severity == 'Blunder' %}sev-blunder{% endif %}">
          <div class="tp-meta">
            <div>
              <span class="pill {% if tp.severity == 'Missed Lethal' %}lethal{% elif tp.severity == 'Blunder' %}blunder{% else %}inaccuracy{% endif %}">
                #{{ tp.rank }} · {{ tp.severity }}
              </span>
              <strong style="margin-left: 8px;">{{ tp.title }}</strong>
            </div>
            <div>
              <a href="#turn-{{ tp.turn_number }}">Jump to Turn {{ tp.turn_number }}</a>
              <span class="pill" style="margin-left: 6px;">{{ tp.habit_tag | format_habit_tag }}</span>
            </div>
          </div>
          <div class="tp-row"><span class="tp-label">Context:</span> {{ tp.turn_context }}</div>
          <div class="tp-row"><span class="tp-label">Played:</span> {{ tp.what_was_played }}</div>
          <div class="tp-row"><span class="tp-label">Better line:</span> {{ tp.better_line }}</div>
          <div class="tp-row"><span class="tp-label">Why it mattered:</span> {{ tp.why_it_mattered }}</div>
        </div>
        {% endfor %}
      {% else %}
        <p style="color: var(--muted); margin: 0;">No major turning point mistakes flagged.</p>
      {% endif %}
    </section>

    <section class="grid-2">
      <div class="card">
        <h2>Best Plays & Positive Habits</h2>
        {% for bp in report.best_plays %}
        <div style="margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid var(--border);">
          <div>
            <a href="#turn-{{ bp.turn_number }}"><strong>Turn {{ bp.turn_number }}</strong></a> — <strong>{{ bp.title }}</strong>
          </div>
          <div style="font-size: 0.9rem; margin-top: 4px;"><span class="tp-label">Play:</span> {{ bp.what_was_played }}</div>
          <div style="font-size: 0.9rem; color: var(--muted);">{{ bp.why_it_was_good }}</div>
        </div>
        {% else %}
          <p style="color: var(--muted); margin: 0;">No standout plays highlighted.</p>
        {% endfor %}
      </div>

      <div class="card">
        <h2>Habit Tags</h2>
        {% for ht in report.habit_tags %}
        <div style="margin-bottom: 10px;">
          <span class="pill {{ 'good' if ht.polarity == 'strength' else 'mistake' }}">{{ ht.polarity }}</span>
          <strong style="margin-left: 6px;">{{ ht.tag | format_habit_tag }}</strong>
          {% if ht.turns %}
            <span style="color: var(--muted); font-size: 0.82rem;">(Turns: {{ ht.turns | join(", ") }})</span>
          {% endif %}
          <div style="font-size: 0.88rem; color: var(--muted); margin-top: 3px;">{{ ht.note }}</div>
        </div>
        {% else %}
          <p style="color: var(--muted); margin: 0;">No habit tags recorded.</p>
        {% endfor %}
      </div>
    </section>
    {% else %}
    <section class="card preview-banner">
      <h2>Replay-Only Preview</h2>
      <p style="margin: 0 0 10px 0;">
        This report was generated directly from deterministic <code>Power.log</code> parsing and Python pre-checks.
        Run <code>hs-coach coach &lt;replay.json&gt;</code> with an LLM provider configured to add AI coaching grades, mulligan review, and turning points.
      </p>
      {% if replay.mulligan.offered %}
      <div><strong>Mulligan Offered:</strong>
        <div class="chip-list">
          {% for mc in replay.mulligan.offered %}
            <span class="chip {{ 'ready' if mc.kept else '' }}">
              {{ 'KEPT' if mc.kept else 'TOSSED' }} · {% if mc.cost is not none %}({{ mc.cost }}) {% endif %}{{ mc.name }}
            </span>
          {% endfor %}
        </div>
      </div>
      {% endif %}
    </section>
    {% endif %}

    {% if replay.your_deck_observed or replay.opponent_deck_observed %}
    <section class="card">
      <h2>Observed Decklists (Original Cards Seen)</h2>
      <div class="grid-2">
        <div class="board-box">
          <strong>Your Observed Deck ({{ replay.your_deck_observed | sum(attribute='count') }} cards seen)</strong>
          {% if replay.your_deck_observed %}
          <div class="chip-list">
            {% for dc in replay.your_deck_observed %}
              <span class="chip">{% if dc.cost is not none %}({{ dc.cost }}) {% endif %}{{ dc.name }}{% if dc.count > 1 %} x{{ dc.count }}{% endif %}</span>
            {% endfor %}
          </div>
          {% else %}
            <div style="color: var(--muted); margin-top: 6px;">None observed</div>
          {% endif %}
        </div>
        <div class="board-box">
          <strong>Opponent Observed Deck ({{ replay.opponent_deck_observed | sum(attribute='count') }} cards seen)</strong>
          {% if replay.opponent_deck_observed %}
          <div class="chip-list">
            {% for dc in replay.opponent_deck_observed %}
              <span class="chip">{% if dc.cost is not none %}({{ dc.cost }}) {% endif %}{{ dc.name }}{% if dc.count > 1 %} x{{ dc.count }}{% endif %}</span>
            {% endfor %}
          </div>
          {% else %}
            <div style="color: var(--muted); margin-top: 6px;">None observed</div>
          {% endif %}
        </div>
      </div>
    </section>
    {% endif %}

    <section class="card">
      <h2>Turn-by-Turn Decision Timeline</h2>
      {% for t in replay.turns %}
      {% set is_flagged = t.pre_checks.missed_lethal or t.pre_checks.cards_burned or t.pre_checks.cards_discarded or (t.turn_number in turning_point_turns) %}
      <div id="turn-{{ t.turn_number }}" class="turn-card {{ t.active_player }} {% if is_flagged %}flagged{% endif %}">
        <div class="turn-head">
          <div class="turn-title">
            Turn {{ t.turn_number }} —
            {{ "Your" if t.active_player == "you" else "Opponent's" }} Turn #{{ t.player_turn_number }}
          </div>
          <div class="sub-meta">
            <span>You: {{ t.start_state.your_hero.hp }}{% if t.start_state.your_hero.armor %}+{{ t.start_state.your_hero.armor }}{% endif %} HP · {{ t.start_state.your_mana_available }}/{{ t.start_state.your_mana_max }} Mana</span>
            <span>Opp: {{ t.start_state.opponent_hero.hp }}{% if t.start_state.opponent_hero.armor %}+{{ t.start_state.opponent_hero.armor }}{% endif %} HP · {{ t.start_state.opponent_hand_size }} cards</span>
          </div>
        </div>

        <div class="grid-2">
          <div class="board-box">
            <strong>Opponent Board (start):</strong>
            {% if t.start_state.opponent_board %}
            <div class="chip-list">
              {% for e in t.start_state.opponent_board %}
              <span class="chip {{ 'dormant' if e.dormant else '' }}">
                {% if e.card_type == 'LOCATION' %}
                  Loc: {{ e.name }} ({{ e.health }}u, {{ 'READY' if e.location_ready else 'CD' }})
                {% else %}
                  {{ e.name }} {{ e.attack }}/{{ e.health }}{% if e.keywords %} [{{ e.keywords | join(', ') }}]{% endif %}
                {% endif %}
              </span>
              {% endfor %}
            </div>
            {% else %}
              <span style="color: var(--muted);"> (empty)</span>
            {% endif %}
          </div>

          <div class="board-box">
            <strong>Your Board (start):</strong>
            {% if t.start_state.your_board %}
            <div class="chip-list">
              {% for e in t.start_state.your_board %}
              <span class="chip {{ 'dormant' if e.dormant else ('ready' if e.location_ready else '') }}">
                {% if e.card_type == 'LOCATION' %}
                  Loc: {{ e.name }} ({{ e.health }}u, {{ 'READY' if e.location_ready else 'CD' }})
                {% else %}
                  {{ e.name }} {{ e.attack }}/{{ e.health }}{% if e.keywords %} [{{ e.keywords | join(', ') }}]{% endif %}
                {% endif %}
              </span>
              {% endfor %}
            </div>
            {% else %}
              <span style="color: var(--muted);"> (empty)</span>
            {% endif %}
          </div>
        </div>

        {% if t.active_player == 'you' %}
        <div style="margin-top: 8px; font-size: 0.87rem;">
          <strong>Your Hand ({{ t.start_state.your_hand | length }}/10):</strong>
          <div class="chip-list">
            {% for hc in t.start_state.your_hand %}
            <span class="chip">
              ({{ hc.cost }}) {{ hc.name }}{% if hc.attack is not none and hc.health is not none %} {{ hc.attack }}/{{ hc.health }}{% endif %}
              {% if hc.countdown and hc.countdown.discards_this_turn %} [DISCARDS!]{% endif %}
              {% if hc.prepare and hc.prepare.available %} [PREPARE]{% endif %}
            </span>
            {% endfor %}
          </div>
        </div>
        {% endif %}

        {% if t.choices %}
        <div style="margin-top: 8px; font-size: 0.87rem;">
          <strong>Choices:</strong>
          <ul class="actions-list">
            {% for ch in t.choices %}
            <li>
              [{{ ch.actor }}] {{ ch.kind }} from <strong>{{ ch.source_name or '?' }}</strong>:
              {% if ch.options_visible %}
                offered [{{ ch.options | map(attribute='name') | join(', ') }}]
                &rarr; picked <strong>{{ ch.chosen_names | join(', ') or 'none' }}</strong>
              {% else %}
                (hidden opponent options)
              {% endif %}
            </li>
            {% endfor %}
          </ul>
        </div>
        {% endif %}

        <div style="margin-top: 8px; font-size: 0.89rem;">
          <strong>Actions:</strong>
          {% if t.actions %}
          <ol class="actions-list">
            {% for act in t.actions %}
            <li>{{ act.summary }}{% if act.action_type in ('PLAY', 'HERO_POWER') and act.mana_cost is not none %} <span style="color: var(--muted);">({{ act.mana_cost }}m)</span>{% endif %}</li>
            {% endfor %}
          </ol>
          {% else %}
            <span style="color: var(--muted);"> (no actions)</span>
          {% endif %}
        </div>

        {% if t.pre_checks.missed_lethal %}
        <div class="precheck-alert">
          <strong>MISSED LETHAL FLAG:</strong> {{ t.pre_checks.missed_lethal_detail }}
        </div>
        {% endif %}
        {% if t.active_player == 'you' and (t.pre_checks.mana_floated > 0 or t.pre_checks.unused_attacks or t.pre_checks.unused_ready_locations or t.pre_checks.cards_burned or t.pre_checks.cards_discarded) %}
        <div class="precheck-note">
          {% if t.pre_checks.mana_floated > 0 %}Mana floated: <strong>{{ t.pre_checks.mana_floated }}</strong> · {% endif %}
          {% if t.pre_checks.unused_attacks %}Unused attacks: <strong>{{ t.pre_checks.unused_attacks | join(', ') }}</strong> · {% endif %}
          {% if t.pre_checks.unused_ready_locations %}Unused READY locations: <strong>{{ t.pre_checks.unused_ready_locations | join(', ') }}</strong> · {% endif %}
          {% if t.pre_checks.cards_burned %}Burned: <strong>{{ t.pre_checks.cards_burned | map(attribute='name') | join(', ') }}</strong> · {% endif %}
          {% if t.pre_checks.cards_discarded %}Discarded: <strong>{{ t.pre_checks.cards_discarded | map(attribute='name') | join(', ') }}</strong>{% endif %}
        </div>
        {% endif %}
      </div>
      {% endfor %}
    </section>

    <section class="card">
      <details>
        <summary>Card Glossary ({{ playable_glossary | length }} playable cards seen in match)</summary>
        <table style="margin-top: 12px;">
          <thead>
            <tr>
              <th>Card</th>
              <th>Cost</th>
              <th>Type / Traits</th>
              <th>Text</th>
            </tr>
          </thead>
          <tbody>
            {% for ref in playable_glossary %}
            <tr>
              <td><strong>{{ ref.name }}</strong><br /><code>{{ ref.card_id }}</code></td>
              <td>{{ ref.base_cost if ref.base_cost is not none else '-' }}</td>
              <td>{{ ref.card_type }}{% if ref.spell_school %} · {{ ref.spell_school }}{% endif %}{% if ref.races %} · {{ ref.races | join('/') }}{% endif %}</td>
              <td>{{ (ref.text | clean_card_text) or '-' }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </details>
      {% if enchantment_glossary %}
      <details style="margin-top: 12px;">
        <summary>Enchantments &amp; Effects ({{ enchantment_glossary | length }})</summary>
        <table style="margin-top: 12px;">
          <thead>
            <tr>
              <th>Effect</th>
              <th>Card ID</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {% for ref in enchantment_glossary %}
            <tr>
              <td><strong>{{ ref.name }}</strong></td>
              <td><code>{{ ref.card_id }}</code></td>
              <td>{{ (ref.text | clean_card_text) or '-' }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </details>
      {% endif %}
    </section>
  </div>
</body>
</html>
"""

_ENV = Environment(autoescape=select_autoescape(default_for_string=True, default=True))
_ENV.filters["format_habit_tag"] = format_habit_tag
_ENV.filters["clean_card_text"] = clean_hs_display_text
_COMPILED_TEMPLATE = _ENV.from_string(_HTML_TEMPLATE)


def render_html_report(
    replay: GameReplay, report: CoachReport | None = None
) -> str:
    """Render an anonymized GameReplay (plus optional CoachReport) into self-contained HTML."""
    tp_turns = (
        {tp.turn_number for tp in report.turning_points} if report is not None else set()
    )
    playable_glossary, enchantment_glossary = partition_glossary(replay.cards_seen)
    return _COMPILED_TEMPLATE.render(
        replay=replay,
        report=report,
        turning_point_turns=tp_turns,
        playable_glossary=playable_glossary,
        enchantment_glossary=enchantment_glossary,
    )
