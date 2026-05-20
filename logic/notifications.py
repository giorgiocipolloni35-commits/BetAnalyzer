import os
import requests
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class EmailService:
    def __init__(self):
        self.resend_key = os.getenv("RESEND_API_KEY")
        self.brevo_key = os.getenv("BREVO_API_KEY")
        # Per Resend, se non hai un dominio verificato, usa alerts@resend.dev
        self.from_email = os.getenv("EMAIL_FROM", "BetAnalyzer <onboarding@resend.dev>")

    def _parse_sections(self, ai_text):
        """Parse AI markdown into structured sections."""
        sections = []
        current = {"title": "", "icon": "", "lines": []}

        for line in ai_text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## "):
                if current["title"] or current["lines"]:
                    sections.append(current)
                title = stripped.replace("## ", "").strip()
                # Extract icon if present
                icon = ""
                if title and not title[0].isalnum():
                    parts = title.split(" ", 1)
                    if len(parts) == 2:
                        icon = parts[0]
                        title = parts[1]
                current = {"title": title, "icon": icon, "lines": []}
            elif stripped == "---":
                continue  # Skip dividers, we handle spacing with sections
            elif stripped:
                current["lines"].append(stripped)

        if current["title"] or current["lines"]:
            sections.append(current)
        return sections

    def _render_player_line(self, line):
        """Render a single player line with colored badges."""
        # Remove leading bullet/dash
        text = line.lstrip("- •").strip()

        # Extract and colorize key parts
        import re

        # VOTO badge
        voto_match = re.search(r'\[VOTO:\s*([\d.]+)\]', text)
        voto_html = ""
        if voto_match:
            v = float(voto_match.group(1))
            color = "#10b981" if v >= 70 else ("#f59e0b" if v >= 55 else "#ef4444")
            voto_html = f'<span style="background:{color};color:#fff;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:bold;margin-left:4px;">{v}</span>'
            text = text.replace(voto_match.group(0), "").strip()

        # Name and position
        name_match = re.match(r'^([^(]+)\(([^)]+)\)', text)
        if name_match:
            name = name_match.group(1).strip()
            pos = name_match.group(2).strip()
            rest = text[name_match.end():].strip()

            # Colorize specific tags in rest
            rest = rest.replace("INFORTUNATO:", '<span style="color:#ef4444;font-weight:bold;">🏥 INFORTUNATO:</span>')
            rest = rest.replace("SQUALIFICATO:", '<span style="color:#ef4444;font-weight:bold;">🔴 SQUALIFICATO:</span>')
            rest = rest.replace("DIFFIDATO", '<span style="color:#f59e0b;font-weight:bold;">⚠️ DIFFIDATO</span>')
            rest = rest.replace("Prob.GOL:", '<span style="color:#10b981;">⚽</span>')
            rest = rest.replace("Prob.AMMON:", '<span style="color:#f59e0b;">🟨</span>')

            # Format segments with subtle separators
            segments = [s.strip() for s in rest.split("|") if s.strip()]
            formatted_rest = ""
            for seg in segments:
                formatted_rest += f'<span style="color:#94a3b8;margin:0 3px;">|</span><span style="font-size:11px;">{seg}</span>'

            return f'''<div style="padding:4px 0;border-bottom:1px solid #1e293b;">
                <span style="color:#e2e8f0;font-weight:600;">{name}</span>
                <span style="color:#64748b;font-size:11px;">({pos})</span>
                {voto_html}
                {formatted_rest}
            </div>'''

        return f'<div style="padding:3px 0;font-size:12px;color:#cbd5e1;">{text}</div>'

    def _render_section(self, section, match_info):
        """Render a section as professional HTML."""
        title = section["title"]
        icon = section["icon"]
        lines = section["lines"]

        # Section header color mapping
        color_map = {
            "ANALISI": "#38bdf8",
            "FORMAZIONI": "#8b5cf6",
            "FOCUS": "#f59e0b",
            "ASSENZE": "#ef4444",
            "MARCATORI": "#10b981",
            "CARTELLINI": "#f59e0b",
            "BETTING": "#38bdf8",
        }
        accent = "#38bdf8"
        for key, col in color_map.items():
            if key in title.upper():
                accent = col
                break

        # Check if this is a lineup/player section
        is_player_section = any("[VOTO:" in l for l in lines) or "FORMAZIONI" in title.upper()
        is_betting = "BETTING" in title.upper() or "CONSIGLI" in title.upper()

        # Render header
        header = f'''<div style="margin-top:20px;margin-bottom:10px;">
            <div style="display:inline-block;background:{accent};color:#0f172a;padding:3px 12px;border-radius:6px;font-size:12px;font-weight:800;letter-spacing:0.5px;">
                {icon} {title}
            </div>
        </div>'''

        # Render body
        body_parts = []
        current_team = None

        for line in lines:
            stripped = line.strip()

            # Team sub-header (### Team Name or **MEDIA VOTO**)
            if stripped.startswith("### ") or stripped.startswith("**MEDIA VOTO"):
                team_title = stripped.replace("### ", "").replace("**", "").strip()
                if "MEDIA VOTO" in team_title:
                    body_parts.append(f'<div style="text-align:right;padding:6px 8px;margin-top:4px;background:#1e293b;border-radius:4px;font-size:12px;color:#f59e0b;font-weight:bold;">{team_title}</div>')
                else:
                    body_parts.append(f'<div style="color:{accent};font-weight:700;font-size:13px;margin:12px 0 6px 0;padding-bottom:4px;border-bottom:1px solid #334155;">{team_title}</div>')
                continue

            # MEDIA VOTO line (not bold)
            if "MEDIA VOTO" in stripped:
                clean = stripped.replace("**", "").strip()
                body_parts.append(f'<div style="text-align:right;padding:6px 8px;margin-top:4px;background:#1e293b;border-radius:4px;font-size:12px;color:#f59e0b;font-weight:bold;">{clean}</div>')
                continue

            # Player lines (start with - or contain [VOTO:])
            if (stripped.startswith("- ") or stripped.startswith("  -")) and is_player_section:
                body_parts.append(self._render_player_line(stripped))
                continue

            # Numbered items (1. Player: 45% ...)
            if stripped and stripped[0].isdigit() and ". " in stripped[:4]:
                import re
                # Highlight percentage
                highlighted = re.sub(r'(\d+%)', r'<span style="color:#10b981;font-weight:bold;">\1</span>', stripped)
                highlighted = highlighted.replace("DIFFIDATO", '<span style="color:#f59e0b;font-weight:bold;">⚠️ DIFFIDATO</span>')
                body_parts.append(f'<div style="padding:3px 0 3px 8px;font-size:12px;color:#e2e8f0;border-left:2px solid {accent}22;margin:2px 0;">{highlighted}</div>')
                continue

            # Bullet points
            if stripped.startswith("- ") or stripped.startswith("• "):
                text = stripped.lstrip("-• ").strip()
                import re
                text = re.sub(r'(\d+%)', r'<span style="color:#10b981;font-weight:bold;">\1</span>', text)
                # Handle bold
                while "**" in text:
                    text = text.replace("**", "<b>", 1).replace("**", "</b>", 1)
                body_parts.append(f'<div style="padding:3px 0 3px 12px;font-size:12px;color:#cbd5e1;">▸ {text}</div>')
                continue

            # Regular text
            if stripped:
                import re
                text = stripped
                while "**" in text:
                    text = text.replace("**", "<b>", 1).replace("**", "</b>", 1)
                text = re.sub(r'(\d+%)', r'<span style="color:#10b981;font-weight:bold;">\1</span>', text)
                body_parts.append(f'<p style="margin:4px 0;font-size:12px;color:#cbd5e1;line-height:1.6;">{text}</p>')

        body_html = "\n".join(body_parts)

        return f'''{header}
        <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 14px;margin-bottom:8px;">
            {body_html}
        </div>'''

    def _render_htft_email(self, htft_data):
        """Render Doppio Tempo section as pure HTML/CSS for email (no JavaScript)."""
        if not htft_data:
            return ""

        analysis = htft_data.get("analysis", {})
        home_stats = htft_data.get("home_stats", {})
        away_stats = htft_data.get("away_stats", {})
        predictions = analysis.get("predictions", [])
        insights = analysis.get("insights", [])

        # --- HT/FT bars ---
        htft_dist = analysis.get("htft_distribution", {})
        htft_bars = ""
        for label, pct in sorted(htft_dist.items(), key=lambda x: x[1], reverse=True):
            if pct < 3:
                continue
            bar_w = min(int(pct * 2.5), 100)
            htft_bars += f'''<tr>
                <td style="width:55px;text-align:right;padding:2px 8px 2px 0;font-size:11px;color:#a5b4fc;font-weight:700;">{label}</td>
                <td style="padding:2px 0;">
                    <div style="background:#1e1e3a;border-radius:3px;height:16px;width:100%;position:relative;">
                        <div style="background:linear-gradient(90deg,#6366f1,#818cf8);height:16px;border-radius:3px;width:{bar_w}%;"></div>
                    </div>
                </td>
                <td style="width:45px;text-align:right;padding:2px 0 2px 6px;font-size:11px;color:#e2e8f0;font-weight:600;">{pct}%</td>
            </tr>'''

        # --- GG/NG bars ---
        ggng_dist = analysis.get("ggng_distribution", {})
        ggng_bars = ""
        for label, pct in sorted(ggng_dist.items(), key=lambda x: x[1], reverse=True):
            bar_w = min(int(pct * 2.5), 100)
            ggng_bars += f'''<tr>
                <td style="width:55px;text-align:right;padding:2px 8px 2px 0;font-size:11px;color:#86efac;font-weight:700;">{label}</td>
                <td style="padding:2px 0;">
                    <div style="background:#0f2a1a;border-radius:3px;height:16px;width:100%;position:relative;">
                        <div style="background:linear-gradient(90deg,#22c55e,#4ade80);height:16px;border-radius:3px;width:{bar_w}%;"></div>
                    </div>
                </td>
                <td style="width:45px;text-align:right;padding:2px 0 2px 6px;font-size:11px;color:#e2e8f0;font-weight:600;">{pct}%</td>
            </tr>'''

        # --- GG summary boxes ---
        gg_ht = analysis.get("gg_ht_pct", 0)
        gg_st = analysis.get("gg_st_pct", 0)
        gg_ft = analysis.get("gg_ft_pct", 0)

        def gg_color(v):
            if v >= 50: return "#4ade80"
            if v >= 30: return "#fbbf24"
            return "#94a3b8"

        # --- Predictions pills ---
        htft_preds = ""
        ggng_preds = ""
        for p in predictions:
            conf = p.get("confidence", "Bassa")
            conf_colors = {"Alta": "#166534;color:#4ade80", "Media": "#713f12;color:#fbbf24", "Moderata": "#7c2d12;color:#fb923c", "Bassa": "#334155;color:#94a3b8"}
            conf_bg = conf_colors.get(conf, conf_colors["Bassa"])
            pill = f'''<span style="display:inline-block;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:700;margin:2px;background:{'#1e1e3a;color:#a5b4fc' if p['type']=='htft' else '#0f2a1a;color:#86efac'};">
                {p.get("emoji","")} {p["market"].replace("HT/FT ","").replace("GG/NG ","")} {p["probability"]}%
                <span style="background:{conf_bg};padding:1px 4px;border-radius:3px;font-size:9px;margin-left:3px;">{conf}</span>
            </span>'''
            if p["type"] == "htft":
                htft_preds += pill
            else:
                ggng_preds += pill

        # --- Insights ---
        insights_html = ""
        for i in insights:
            insights_html += f'<div style="padding:2px 0;font-size:11px;color:#cbd5e1;">{i}</div>'

        return f'''
        <div style="margin-top:20px;margin-bottom:10px;">
            <div style="display:inline-block;background:#8b5cf6;color:#fff;padding:3px 12px;border-radius:6px;font-size:12px;font-weight:800;letter-spacing:0.5px;">
                &#9200; DOPPIO TEMPO — HT/FT
            </div>
        </div>
        <div style="background:#1e293b;border:1px solid #334155;border-radius:8px;padding:14px;margin-bottom:8px;">

            <!-- Two columns via table -->
            <table width="100%" cellpadding="0" cellspacing="0"><tr>
                <!-- LEFT: HT/FT -->
                <td style="width:48%;vertical-align:top;padding-right:10px;">
                    <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:8px;">Esito 1&deg;T / Finale</div>
                    {htft_preds}
                    <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;">
                        {htft_bars}
                    </table>
                </td>
                <td style="width:4%;"></td>
                <!-- RIGHT: GG/NG -->
                <td style="width:48%;vertical-align:top;">
                    <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px;font-weight:700;margin-bottom:8px;">GG / NG per Tempo</div>

                    <!-- GG boxes -->
                    <table width="100%" cellpadding="0" cellspacing="4" style="margin-bottom:8px;"><tr>
                        <td style="background:#0f172a;border:1px solid #334155;border-radius:6px;text-align:center;padding:6px;">
                            <div style="font-size:9px;color:#64748b;">GG 1&deg;T</div>
                            <div style="font-size:16px;font-weight:800;color:{gg_color(gg_ht)};">{gg_ht:.0f}%</div>
                        </td>
                        <td style="background:#0f172a;border:1px solid #334155;border-radius:6px;text-align:center;padding:6px;">
                            <div style="font-size:9px;color:#64748b;">GG 2&deg;T</div>
                            <div style="font-size:16px;font-weight:800;color:{gg_color(gg_st)};">{gg_st:.0f}%</div>
                        </td>
                        <td style="background:#0f172a;border:1px solid #334155;border-radius:6px;text-align:center;padding:6px;">
                            <div style="font-size:9px;color:#64748b;">GG FIN</div>
                            <div style="font-size:16px;font-weight:800;color:{gg_color(gg_ft)};">{gg_ft:.0f}%</div>
                        </td>
                    </tr></table>

                    {ggng_preds}
                    <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;">
                        {ggng_bars}
                    </table>
                </td>
            </tr></table>

            <!-- Insights -->
            {"<div style='margin-top:10px;padding-top:8px;border-top:1px solid #334155;'>" + insights_html + "</div>" if insights_html else ""}
        </div>
        '''

    def _render_line_movement(self, lm):
        """Render Line Movement section as HTML for email."""
        if not lm or lm.get("snapshots", 0) < 2:
            return ""

        op = lm.get("opening", {})
        cur = lm.get("current", {})
        mv = lm.get("movement", {})

        def _arrow(val):
            if val <= -0.10:
                return f'<span style="color:#22c55e;">▼ {val:+.2f}</span>'  # Green = money coming in
            elif val >= 0.10:
                return f'<span style="color:#ef4444;">▲ {val:+.2f}</span>'  # Red = drifting out
            else:
                return f'<span style="color:#94a3b8;">= {val:+.2f}</span>'

        # Determine main signal
        signal_html = ""
        if lm.get("signals"):
            signal_html = f'''
            <div style="background:#1a2e1a;border:1px solid #22c55e;border-radius:8px;padding:10px 14px;margin-top:10px;">
                <span style="font-size:13px;">💰 <b style="color:#22c55e;">Soldi professionali su:</b> {', '.join(lm['signals'])}</span>
                <br><span style="font-size:11px;color:#94a3b8;">Quota in calo = flusso di denaro da scommettitori professionisti (sharps)</span>
            </div>'''

        steam_html = ""
        if lm.get("steam_move"):
            sm = lm["steam_move"]
            steam_html = f'''
            <div style="background:#2d1a1a;border:1px solid #ef4444;border-radius:8px;padding:10px 14px;margin-top:8px;">
                <span style="font-size:13px;">🚨 <b style="color:#ef4444;">STEAM MOVE</b> — Quota {sm['side'].upper()} {sm['direction']}{sm['delta']:.3f} in un singolo aggiornamento</span>
                <br><span style="font-size:11px;color:#94a3b8;">Movimento brusco = informazione privilegiata o grosso volume di denaro</span>
            </div>'''

        return f'''
        <div style="background:linear-gradient(135deg,#1e293b 0%,#172033 100%);border:1px solid #334155;border-radius:12px;padding:16px 20px;margin-top:16px;">
            <h3 style="color:#a78bfa;margin:0 0 12px 0;font-size:14px;">📈 LINE MOVEMENT
                <span style="color:#64748b;font-weight:normal;font-size:11px;">({lm.get('bookmaker','?')}, {lm['snapshots']} rilevazioni)</span>
            </h3>
            <table width="100%" cellpadding="6" cellspacing="0" style="font-size:13px;">
                <tr style="color:#64748b;font-size:11px;text-transform:uppercase;">
                    <td></td><td style="text-align:center;">1 (Casa)</td><td style="text-align:center;">X (Pareggio)</td><td style="text-align:center;">2 (Ospite)</td>
                </tr>
                <tr style="border-bottom:1px solid #334155;">
                    <td style="color:#94a3b8;">Apertura</td>
                    <td style="text-align:center;">{op.get('home','-'):.2f}</td>
                    <td style="text-align:center;">{op.get('draw','-'):.2f}</td>
                    <td style="text-align:center;">{op.get('away','-'):.2f}</td>
                </tr>
                <tr>
                    <td style="color:#f8fafc;font-weight:bold;">Attuale</td>
                    <td style="text-align:center;font-weight:bold;">{cur.get('home','-'):.2f}</td>
                    <td style="text-align:center;font-weight:bold;">{cur.get('draw','-'):.2f}</td>
                    <td style="text-align:center;font-weight:bold;">{cur.get('away','-'):.2f}</td>
                </tr>
                <tr style="border-top:1px solid #334155;">
                    <td style="color:#94a3b8;">Movimento</td>
                    <td style="text-align:center;">{_arrow(mv.get('home',0))}</td>
                    <td style="text-align:center;">{_arrow(mv.get('draw',0))}</td>
                    <td style="text-align:center;">{_arrow(mv.get('away',0))}</td>
                </tr>
            </table>
            {signal_html}
            {steam_html}
        </div>'''

    def send_bet_alert(self, to_email, match_info, ai_analysis, htft_data=None, line_movement=None):
        """Invia un alert scommessa formattato in HTML professionale"""
        subject = f"⚽ FORMAZIONI UFFICIALI: {match_info['home']} vs {match_info['away']}"

        # Parse AI output into structured sections
        sections = self._parse_sections(ai_analysis)

        # Render each section
        sections_html = "\n".join([self._render_section(s, match_info) for s in sections])

        # Render HT/FT section
        htft_html = self._render_htft_email(htft_data)

        # Render Line Movement section
        line_movement_html = self._render_line_movement(line_movement)

        html = f"""
        <html>
        <body style="margin: 0; padding: 0; background-color: #0f172a;">
            <div style="font-family: 'Segoe UI', Helvetica, Arial, sans-serif; max-width: 700px; margin: 0 auto; background-color: #0f172a; color: #f8fafc; padding: 30px 20px;">

                <div style="text-align: center; margin-bottom: 24px;">
                    <h1 style="color: #38bdf8; margin: 0; font-size: 22px; letter-spacing: 1px;">BetAnalyzer <span style="color: #f59e0b;">PRO</span></h1>
                    <p style="color: #94a3b8; font-size: 11px; margin: 4px 0 0 0;">Live Match Intelligence</p>
                </div>

                <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); border: 1px solid #334155; padding: 16px 20px; border-radius: 12px; margin-bottom: 20px;">
                    <table width="100%" cellpadding="0" cellspacing="0"><tr>
                        <td><span style="background-color: #38bdf8; color: #0f172a; padding: 3px 10px; border-radius: 99px; font-size: 10px; font-weight: bold; text-transform: uppercase;">{match_info['league']}</span></td>
                        <td style="text-align:right;"><span style="color: #94a3b8; font-size: 11px;">{match_info['date']}</span></td>
                    </tr></table>
                    <h2 style="margin: 12px 0 0 0; font-size: 20px; text-align: center; color: #ffffff;">
                        {match_info['home']} <span style="color: #64748b; font-weight: normal;">vs</span> {match_info['away']}
                    </h2>
                </div>

                {sections_html}

                {htft_html}

                {line_movement_html}

                <div style="text-align: center; margin-top: 24px;">
                    <a href="http://localhost:5001" style="background: linear-gradient(135deg, #38bdf8 0%, #818cf8 100%); color: #0f172a; padding: 12px 28px; text-decoration: none; font-weight: 800; border-radius: 8px; font-size: 13px; display: inline-block; box-shadow: 0 4px 12px rgba(56, 189, 248, 0.3);">
                        VAI ALLA DASHBOARD LIVE ⚡
                    </a>
                </div>

                <div style="margin-top: 32px; text-align: center; border-top: 1px solid #334155; padding-top: 16px;">
                    <p style="font-size: 11px; color: #64748b; line-height: 1.5;">
                        Ricevi questo alert perche il monitoraggio Live e <b>ATTIVO</b>.<br>
                        Monitoriamo i principali campionati per garantirti il massimo vantaggio statistico.
                    </p>
                    <p style="font-size: 10px; color: #475569; margin-top: 10px;">
                        &copy; 2026 BetAnalyzer Intelligence Pro. Tutti i diritti riservati.
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        
        if self.resend_key:
            return self._send_via_resend(to_email, subject, html)
        elif self.brevo_key:
            return self._send_via_brevo(to_email, subject, html)
        else:
            logger.error("Email fallita: Nessuna API KEY configurata nel file .env")
            return False

    def _send_via_resend(self, to, subject, html):
        url = "https://api.resend.com/emails"
        headers = {
            "Authorization": f"Bearer {self.resend_key}",
            "Content-Type": "application/json"
        }
        data = {
            "from": self.from_email,
            "to": [to],
            "subject": subject,
            "html": html
        }
        try:
            resp = requests.post(url, json=data, headers=headers)
            if resp.status_code in [200, 201, 202]:
                logger.info(f"Email Resend inviata con successo a {to}")
                return True
            else:
                logger.error(f"Errore Resend API ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Eccezione invio Resend: {e}")
            return False

    def _send_via_brevo(self, to, subject, html):
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "api-key": self.brevo_key,
            "Content-Type": "application/json"
        }
        data = {
            "sender": {"name": "BetAnalyzer", "email": "alerts@betanalyzer.pro"},
            "to": [{"email": to}],
            "subject": subject,
            "htmlContent": html
        }
        try:
            resp = requests.post(url, json=data, headers=headers)
            if resp.status_code in [200, 201, 202]:
                logger.info(f"Email Brevo inviata con successo a {to}")
                return True
            else:
                logger.error(f"Errore Brevo API ({resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Eccezione invio Brevo: {e}")
            return False
